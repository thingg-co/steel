"""Coinbase Advanced Trade spot: `POST /api/v3/brokerage/orders`, `batch_cancel`, the open-order
list and the fills, behind the execution gate. The CDP key signs an ES256 JWT per request exactly
as the read-only reader does (`uri` = "METHOD api.coinbase.com/path"), from a second key file,
`coinbase-trade.json`, that carries the Trade permission."""
from __future__ import annotations

import secrets
import time
import uuid
from decimal import ROUND_DOWN, Decimal

from ..config import canonical
from ..sources.base import _iso, _iso_ts, base_of
from .base import Executor, Order, OrderResult

HOST = "api.coinbase.com"
ORDERS = "/api/v3/brokerage/orders"
CANCEL = "/api/v3/brokerage/orders/batch_cancel"
OPEN = "/api/v3/brokerage/orders/historical/batch"
FILLS = "/api/v3/brokerage/orders/historical/fills"


def _num(x: float, places: int = 8) -> str:
    """A quantity or price as the venue wants it: a plain decimal string, rounded DOWN (never
    upsized) to `places`, no exponent."""
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class CoinbaseExecutor(Executor):
    """Spot only, USD-quoted, the product id is the canonical symbol itself. A market order is
    `market_market_ioc` with `base_size`; a limit is `limit_limit_gtc` with `post_only: false`.
    `client_order_id` is required by the venue, so one is minted when the ticket has none.
    Lot sizes (`base_increment`) are per product and not known here: quantities go rounded down
    to 8 decimals and the venue rejects a size under its increment. Spot has no reduce-only
    (the flag exists only for perpetuals), so the ticket's flag is ignored. The "testnet" is
    Coinbase's Advanced Trade sandbox: the same paths on api-sandbox.coinbase.com, no key
    checked, every answer static and canned (nothing is ever matched); the Exchange API's
    sandbox is a different host and a different API."""

    id = "coinbase"
    label = "Coinbase"
    venue = "spot"
    fields = [{"id": "name", "label": "Key name (organizations/.../apiKeys/...)"}, {"id": "privateKey", "label": "Private key (PEM)", "multiline": True}]
    endpoints = (ORDERS, CANCEL, OPEN, FILLS)
    HOST = f"https://{HOST}"
    TESTNET = "https://api-sandbox.coinbase.com"

    def __init__(self, gate, *, dry_run: bool = True, testnet: bool = False, **creds):
        super().__init__(gate, dry_run=dry_run, testnet=testnet, **creds)
        self.privateKey = self.privateKey.replace("\\n", "\n")       # a one-line field carries the PEM with escaped newlines

    @staticmethod
    def product(sym: str) -> str:
        return f"{base_of(sym)}-USD"

    def jwt(self, method: str, path: str, now: float | None = None, nonce: str | None = None) -> str:
        """ES256, `uri` = "METHOD host/path" (the production host even on the sandbox, which
        checks nothing), 120 s life, the key name as `kid` and `sub`."""
        import jwt as pyjwt  # noqa: PLC0415 -- optional at import time; needed only with a key
        now = int(now if now is not None else time.time())
        payload = {"sub": self.name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"{method} {HOST}{path}"}
        return pyjwt.encode(payload, self.privateKey, algorithm="ES256", headers={"kid": self.name, "nonce": nonce or secrets.token_hex(16)})

    def headers(self, method: str, path: str, now: float | None = None) -> dict:
        return {"Authorization": f"Bearer {self.jwt(method, path, now)}", "Content-Type": "application/json"}

    def body(self, order: Order) -> dict:
        size = _num(order.qty)
        if order.price is None:
            cfg = {"market_market_ioc": {"base_size": size}}
        else:
            cfg = {"limit_limit_gtc": {"base_size": size, "limit_price": _num(order.price), "post_only": False}}
        return {"client_order_id": order.client_id or str(uuid.uuid4()), "product_id": self.product(order.symbol),
                "side": "BUY" if order.side == "buy" else "SELL", "order_configuration": cfg}

    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._send("POST", f"{self.host}{ORDERS}", json_body=self.body(order), headers=self.headers("POST", ORDERS))
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        if ans.get("success"):
            return OrderResult(True, self.id, str((ans.get("success_response") or {}).get("order_id") or ""), "accepted", raw=ans, request=req)
        err = ans.get("error_response") or {}
        why = err.get("error_details") or err.get("message") or err.get("new_order_failure_reason") or ans.get("message") or f"HTTP {ans.get('_status')}"
        return OrderResult(False, self.id, status="rejected", error=str(why)[:200], raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._send("POST", f"{self.host}{CANCEL}", json_body={"order_ids": [order_id]}, headers=self.headers("POST", CANCEL))
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id, "dry-run", request=req)
        res = ((ans.get("results") or [{}])[0]) if isinstance(ans.get("results"), list) else {}
        if res.get("success"):
            return OrderResult(True, self.id, str(res.get("order_id") or order_id), "accepted", raw=ans, request=req)
        why = res.get("failure_reason") or ans.get("message") or f"HTTP {ans.get('_status')}"
        return OrderResult(False, self.id, order_id, "rejected", error=str(why)[:200], raw=ans, request=req)

    @staticmethod
    def _norm_order(o: dict) -> dict:
        cfg = o.get("order_configuration") or {}
        inner = next(iter(cfg.values()), {}) if cfg else {}
        price = inner.get("limit_price") if isinstance(inner, dict) else None
        return {"order_id": str(o.get("order_id") or ""), "symbol": canonical(str(o.get("product_id") or "")),
                "side": str(o.get("side") or "").lower(), "qty": float((inner.get("base_size") if isinstance(inner, dict) else None) or 0.0),
                "price": float(price) if price else None, "filled": float(o.get("filled_size") or 0.0), "status": str(o.get("status") or "").lower()}

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict = {"order_status": "OPEN", "limit": 250}
        if symbol:
            params["product_ids"] = self.product(symbol)
        _, ans = await self._send("GET", f"{self.host}{OPEN}", params=params, headers=self.headers("GET", OPEN))
        return [self._norm_order(o) for o in (ans.get("orders") or []) if isinstance(o, dict)]

    async def fills(self, since: float) -> list[dict]:
        params = {"start_sequence_timestamp": _iso(since), "limit": 250}
        _, ans = await self._send("GET", f"{self.host}{FILLS}", params=params, headers=self.headers("GET", FILLS))
        out = []
        for f in ans.get("fills") or []:
            if not isinstance(f, dict):
                continue
            try:
                t = _iso_ts(f.get("trade_time"))
            except (TypeError, ValueError):
                t = 0.0
            out.append({"order_id": str(f.get("order_id") or ""), "symbol": canonical(str(f.get("product_id") or "")), "side": str(f.get("side") or "").lower(),
                        "qty": float(f.get("size") or 0.0), "price": float(f.get("price") or 0.0), "fee": float(f.get("commission") or 0.0), "t": t})
        return out
