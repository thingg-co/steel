"""Gate.io spot: `POST /api/v4/spot/orders`, the single-order DELETE, the open-order list and
`my_trades`, behind the execution gate. Signed as the read-only reader signs its reads (SIGN is
the hex HMAC-SHA512 of "METHOD\\npath\\nquery\\nsha512hex(body)\\ntimestamp", seconds), from a
key of its own, `gateio-trade.json`, with the Spot Trade permission. Gate.io has no spot testnet
(its testnet covers futures only)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from decimal import ROUND_DOWN, Decimal

from ..config import canonical
from ..sources.base import base_of
from .base import Executor, Order, OrderResult

HOST = "https://api.gateio.ws"
ORDERS = "/api/v4/spot/orders"
OPEN = "/api/v4/spot/open_orders"
TRADES = "/api/v4/spot/my_trades"
_TEXT = re.compile(r"[^0-9A-Za-z_.-]")


def _num(x: float, places: int = 8) -> str:
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class GateIOExecutor(Executor):
    """USDT-quoted: BASE_USDT, as the market-data source maps it. A limit order is GTC with
    `amount` in base units. A market order is `type: "market"` with `time_in_force: "ioc"`
    (the venue allows only ioc or fok) and `amount` in the QUOTE currency for a buy and the
    base for a sell: a market buy therefore spends the ticket's `notional_usd` of USDT (the
    gate requires it to be set) and the filled base quantity comes back as `filled_total` over
    `avg_deal_price`. The client id goes in `text` as "t-" plus up to 28 bytes of
    [0-9A-Za-z_.-] (the ticket id is squeezed to that; a hex one is minted otherwise).
    Precision (`amount_precision`, `precision`) is per pair and not known here: 8 decimals
    rounded down, the venue rejects what it will not take. Spot has no reduce-only; the flag
    is ignored. An error answer is `{label, message}`; a success carries `id` and `status`
    (open, closed, cancelled)."""

    id = "gateio"
    label = "Gate.io"
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (ORDERS, ORDERS + "/*", OPEN, TRADES)
    HOST = HOST

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "_USDT"

    @staticmethod
    def text(client_id: str) -> str:
        cid = _TEXT.sub("", str(client_id or ""))[:28]
        return "t-" + (cid or secrets.token_hex(12))

    def sign(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> str:
        ts = int(time.time()) if ts is None else int(ts)
        msg = "\n".join([method.upper(), path, query, hashlib.sha512(body.encode()).hexdigest(), str(ts)])
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha512).hexdigest()

    def headers(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> dict:
        ts = int(time.time()) if ts is None else int(ts)
        return {"KEY": self.key, "Timestamp": str(ts), "SIGN": self.sign(method, path, query, body, ts), "Content-Type": "application/json"}

    def body(self, order: Order) -> dict:
        d = {"text": self.text(order.client_id), "currency_pair": self.pair(order.symbol), "account": "spot", "side": order.side}
        if order.price is None:
            d |= {"type": "market", "time_in_force": "ioc",
                  "amount": _num(order.notional_usd if order.side == "buy" else order.qty)}
        else:
            d |= {"type": "limit", "time_in_force": "gtc", "amount": _num(order.qty), "price": _num(order.price)}
        return d

    async def _call(self, method: str, path: str, query: str = "", body: dict | None = None) -> tuple[dict, dict]:
        content = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.host}{path}" + (f"?{query}" if query else "")
        return await self._send(method, url, content=content or None, headers=self.headers(method, path, query, content))

    @staticmethod
    def _why(ans: dict) -> str:
        return f"{ans.get('label') or ans.get('_status')}: {str(ans.get('message') or '')[:120]}".strip(": ")

    async def _place(self, order: Order) -> OrderResult:
        body = self.body(order)
        req, ans = await self._call("POST", ORDERS, body=body)
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        if ans.get("label") or not ans.get("id"):
            return OrderResult(False, self.id, status="rejected", error=self._why(ans), raw=ans, request=req)
        amount, left, avg = float(ans.get("amount") or 0.0), float(ans.get("left") or 0.0), float(ans.get("avg_deal_price") or 0.0)
        if body["type"] == "market" and order.side == "buy":
            filled = float(ans.get("filled_total") or 0.0) / avg if avg > 0 else 0.0      # amount and left are in quote
        else:
            filled = max(0.0, amount - left)
        status = "filled" if ans.get("status") == "closed" else "accepted"
        return OrderResult(True, self.id, str(ans.get("id")), status, filled_qty=filled, avg_price=avg or None, raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("DELETE", f"{ORDERS}/{order_id}", f"currency_pair={self.pair(symbol)}")
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id, "dry-run", request=req)
        if ans.get("label") or not ans.get("id"):
            return OrderResult(False, self.id, order_id, "rejected", error=self._why(ans), raw=ans, request=req)
        return OrderResult(True, self.id, str(ans.get("id")), "accepted", raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._call("GET", OPEN, "account=spot")
        out = []
        for group in ans.get("data") or []:
            if not isinstance(group, dict):
                continue
            sym = canonical(str(group.get("currency_pair") or "").replace("_", "-"))
            if symbol and sym != symbol:
                continue
            for o in group.get("orders") or []:
                amount, left = float(o.get("amount") or 0.0), float(o.get("left") or 0.0)
                out.append({"order_id": str(o.get("id") or ""), "symbol": sym, "side": str(o.get("side") or ""), "qty": amount,
                            "price": float(o["price"]) if o.get("price") not in (None, "", "0") else None, "filled": max(0.0, amount - left),
                            "status": str(o.get("status") or "")})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._call("GET", TRADES, f"account=spot&from={int(since)}&limit=1000")
        out = []
        for t in ans.get("data") or []:
            if not isinstance(t, dict):
                continue
            ms = t.get("create_time_ms")
            out.append({"order_id": str(t.get("order_id") or ""), "symbol": canonical(str(t.get("currency_pair") or "").replace("_", "-")),
                        "side": str(t.get("side") or ""), "qty": float(t.get("amount") or 0.0), "price": float(t.get("price") or 0.0),
                        "fee": float(t.get("fee") or 0.0), "t": float(ms) / 1000.0 if ms else float(t.get("create_time") or 0.0)})
        return out
