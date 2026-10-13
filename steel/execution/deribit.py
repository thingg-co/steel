"""Deribit perpetual orders over JSON-RPC-on-HTTPS: `GET /api/v2/private/buy` and `sell`,
`cancel`, `get_open_orders_by_currency` and `get_user_trades_by_currency`, each signed per
request. A key with the trade:read_write scope; test.deribit.com has its own accounts and keys."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal

from ..sources import unscale
from ..sources.deribit import DeribitSource
from .base import Executor, Order, OrderResult

HOST = "https://www.deribit.com"
BUY = "/api/v2/private/buy"
SELL = "/api/v2/private/sell"
CANCEL = "/api/v2/private/cancel"
OPEN = "/api/v2/private/get_open_orders_by_currency"
TRADES = "/api/v2/private/get_user_trades_by_currency"
INSTRUMENTS = "/api/v2/public/get_instruments"
INVERSE_USD = {"BTC-PERPETUAL": 10.0, "ETH-PERPETUAL": 1.0}     # contract size in USD (public/get_instrument, 2026-10)


def _dec(x: float, places: int) -> str:
    s = format(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class DeribitExecutor(Executor):
    """Deribit client id + secret, signed per request as the account reader does:
    `Authorization: deri-hmac-sha256 id=..,ts=..,nonce=..,sig=..`, sig = hex(HMAC-SHA256(secret,
    "ts\\nnonce\\nGET\\nuri-with-query\\n\\n")); no token is ever fetched. The query is built
    once and the same string is signed and sent. Sizing: BTC-PERPETUAL and ETH-PERPETUAL are
    inverse, `amount` in USD in multiples of the contract size (10 USD for BTC, 1 USD for ETH:
    `INVERSE_USD`, from the venue's instrument data), so the base quantity is turned into USD at
    the order's reference price (its limit price, else `notional_usd / qty`) and cut down to a
    whole number of contracts, never up; the linear BASE_USDC-PERPETUAL contracts take `amount`
    in the coin, a 1000PEPE-style name in thousands of tokens (`unscale`), cut to the contract
    size when `products` has it (fetched once from public/get_instruments through the gate; in
    dry-run nothing is fetched, so seed `products` as the source's `list_products` would, or
    the static spelling is used and the float passed for the venue to check). A limit order
    sends `post_only=false` explicitly. `reduce_only` is a plain boolean; `label` is up to 64
    characters. A filled market order comes back `order_state: filled` with `filled_amount` and
    `average_price` in the instrument's units, converted back to base here."""

    id = "deribit"
    label = "Deribit"
    venue = "perp"
    fields = [{"id": "key", "label": "Client ID"}, {"id": "secret", "label": "Client secret"}]
    endpoints = (BUY, SELL, CANCEL, OPEN, TRADES, INSTRUMENTS)
    HOST = HOST
    TESTNET = "https://test.deribit.com"

    inst = staticmethod(DeribitSource.inst)             # BTC-USD -> BTC-PERPETUAL, SOL-USD -> SOL_USDC-PERPETUAL
    is_inverse = staticmethod(DeribitSource.is_inverse)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.products: dict[str, tuple[str, float, float]] = {}   # canonical -> (instrument, token factor, contract size)

    def sign(self, method: str, uri: str, body: str = "", ts=None, nonce: str | None = None) -> tuple[str, str, str]:
        ts = str(int(time.time() * 1000) if ts is None else ts)
        nonce = nonce or secrets.token_hex(8)
        data = f"{ts}\n{nonce}\n{method.upper()}\n{uri}\n{body}\n"
        return ts, nonce, hmac.new(self.secret.encode(), data.encode(), hashlib.sha256).hexdigest()

    def headers(self, method: str, uri: str, body: str = "", ts=None, nonce: str | None = None) -> dict:
        ts, nonce, sig = self.sign(method, uri, body, ts, nonce)
        return {"Authorization": f"deri-hmac-sha256 id={self.key},ts={ts},nonce={nonce},sig={sig}"}

    async def _call(self, path: str, params: dict | None = None) -> tuple[dict, dict]:
        query = urllib.parse.urlencode(params or {})
        uri = path + (f"?{query}" if query else "")
        return await self._send("GET", f"{self.host}{uri}", headers=self.headers("GET", uri))

    @staticmethod
    def symbol(name: str) -> tuple[str, float]:
        base, factor = unscale(str(name or "").split("_")[0].split("-")[0])
        return f"{base}-USD", factor

    @staticmethod
    def error_of(ans: dict) -> str:
        e = ans.get("error") if isinstance(ans.get("error"), dict) else {"message": ans.get("error") or f"HTTP {ans.get('_status')}"}
        reason = (e.get("data") or {}).get("reason") if isinstance(e.get("data"), dict) else ""
        return f"{e.get('message')} ({e.get('code')}{', ' + reason if reason else ''})"[:160]

    async def instruments(self) -> dict:
        if not self.products:
            _, ans = await self._call(INSTRUMENTS, {"currency": "any", "kind": "future"})
            for i in ans.get("result") if isinstance(ans.get("result"), list) else []:
                name = str(i.get("instrument_name") or "")
                if i.get("settlement_period") != "perpetual" or not i.get("is_active", True):
                    continue
                if not self.is_inverse(name) and "_USDC-" not in name and "_USDT-" not in name:
                    continue
                sym, factor = self.symbol(name)
                if self.is_inverse(name) or sym not in self.products:        # the inverse wins when both exist
                    self.products[sym] = (name, factor, float(i.get("contract_size") or 0.0))
        return self.products

    def amount(self, order: Order) -> tuple[str, str]:
        """(instrument, `amount`): USD for an inverse contract, the coin for a linear one."""
        name, factor, size = self.products.get(order.symbol) or (self.inst(order.symbol), 1.0, 0.0)
        if self.is_inverse(name):
            step = size or INVERSE_USD.get(name, 10.0)
            ref = order.price or (order.notional_usd / order.qty if order.qty > 0 else 0.0)
            if ref <= 0:
                raise ValueError(f"{order.symbol}: an inverse contract is sized in USD and needs a reference price")
            usd = math.floor(order.qty * ref / step + 1e-9) * step
            if usd < step:
                raise ValueError(f"{order.symbol}: {order.qty} x {ref:g} USD is under one {step:g} USD contract")
            return name, _dec(usd, 2)
        amt = Decimal(str(order.qty)) / Decimal(str(factor))
        if size > 0:
            amt = (amt / Decimal(str(size))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(size))
        if amt <= 0:
            raise ValueError(f"{order.symbol}: {order.qty} is under one contract of {size * factor:g}")
        return name, format(amt.normalize(), "f")

    def _base(self, name: str, amount, price) -> float:
        """A venue amount back to base units: USD / price for an inverse contract, coin x factor for a linear one."""
        amount, factor = float(amount or 0.0), self.symbol(name)[1]
        if self.is_inverse(name):
            return amount / float(price) if isinstance(price, (int, float)) and float(price) > 0 else 0.0
        return amount * factor

    async def _place(self, order: Order) -> OrderResult:
        await self.instruments()
        try:
            name, amt = self.amount(order)
        except ValueError as e:
            return OrderResult(False, self.id, status="refused", error=str(e))
        factor = self.symbol(name)[1]
        p = {"instrument_name": name, "amount": amt, "type": "market" if order.price is None else "limit"}
        if order.price is not None:
            p["price"] = _dec(order.price * factor, 10)
            p["post_only"] = "false"
        if order.reduce_only:
            p["reduce_only"] = "true"
        if order.client_id:
            p["label"] = order.client_id[:64]
        req, ans = await self._call(BUY if order.side == "buy" else SELL, p)
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        if ans.get("error") or not isinstance(ans.get("result"), dict):
            return OrderResult(False, self.id, status="rejected", error=self.error_of(ans), raw=ans, request=req)
        o = ans["result"].get("order") or {}
        state, oid = str(o.get("order_state") or ""), str(o.get("order_id") or "")
        if state not in ("open", "filled"):
            return OrderResult(False, self.id, order_id=oid, status="rejected", error=f"{state}: {o.get('cancel_reason') or ''}"[:160], raw=ans, request=req)
        avg = o.get("average_price")
        filled = self._base(name, o.get("filled_amount"), avg)
        avg_base = float(avg) / factor if isinstance(avg, (int, float)) and avg > 0 else None
        return OrderResult(True, self.id, order_id=oid, status=state if state == "filled" else "accepted", filled_qty=filled, avg_price=avg_base,
                           raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call(CANCEL, {"order_id": order_id})
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        o = ans.get("result") if isinstance(ans.get("result"), dict) else {}
        ok = not ans.get("error") and o.get("order_state") == "cancelled"
        err = "" if ok else (self.error_of(ans) if ans.get("error") else str(o.get("order_state")))
        return OrderResult(ok, self.id, order_id=order_id, status="cancelled" if ok else "rejected", error=err, raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._call(OPEN, {"currency": "any", "kind": "future"})
        out = []
        for o in ans.get("result") or [] if isinstance(ans.get("result"), list) else []:
            name = str(o.get("instrument_name") or "")
            sym, factor = self.symbol(name)
            if symbol and sym != symbol:
                continue
            px = o.get("price")
            out.append({"order_id": str(o.get("order_id") or ""), "symbol": sym, "side": o.get("direction"), "qty": self._base(name, o.get("amount"), px),
                        "price": float(px) / factor if isinstance(px, (int, float)) else px, "filled": self._base(name, o.get("filled_amount"), px),
                        "status": o.get("order_state"), "client_id": o.get("label") or ""})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._call(TRADES, {"currency": "any", "kind": "future", "start_timestamp": int(since * 1000), "count": 1000})
        out = []
        for t in ((ans.get("result") or {}).get("trades") if isinstance(ans.get("result"), dict) else None) or []:
            name = str(t.get("instrument_name") or "")
            sym, factor = self.symbol(name)
            out.append({"order_id": str(t.get("order_id") or ""), "symbol": sym, "side": t.get("direction"), "qty": self._base(name, t.get("amount"), t.get("price")),
                        "price": float(t.get("price") or 0.0) / factor, "fee": float(t.get("fee") or 0.0), "t": int(t.get("timestamp") or 0) / 1000.0})
        return out
