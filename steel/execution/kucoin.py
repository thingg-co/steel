"""KuCoin spot: the high-frequency order endpoints (`/api/v1/hf/orders`, the only spot order
API the current docs list; the old `/api/v1/orders` family is "abandoned"), behind the execution
gate. KC-API v2 signing as the read-only reader: KC-API-SIGN is base64(HMAC-SHA256(ts + METHOD +
endpoint-with-query + body)), the passphrase HMAC-signed the same way, from a key of its own,
`kucoin-trade.json`, with the Spot Trading permission. The sandbox host is the one the docs
used to publish (openapi-sandbox.kucoin.com, spot only); it did not answer on 2026-10-11."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import ROUND_DOWN, Decimal

from ..config import canonical
from ..sources.base import base_of
from .base import Executor, Order, OrderResult

HOST = "https://api.kucoin.com"
ORDERS = "/api/v1/hf/orders"
ACTIVE = "/api/v1/hf/orders/active"
ACTIVE_SYMBOLS = "/api/v1/hf/orders/active/symbols"
FILLS = "/api/v1/hf/fills"


def _num(x: float, places: int = 8) -> str:
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class KuCoinExecutor(Executor):
    """USDT-quoted: BASE-USDT, as the market-data source maps it. The body is sent as the exact
    compact JSON string that was signed (the signature covers the bytes). A market order carries
    `size` in base units (`funds` is the quote-sized alternative, unused); a limit is GTC.
    `clientOid` is required-unique in practice (a UUID is recommended; none is given a hex one).
    Lot sizes (`baseIncrement`) are per symbol and not known here: 8 decimals rounded down, the
    venue rejects a size under its increment. Spot has no reduce-only; the flag is ignored.
    `symbol` is required on the cancel, the active list and the fills: with no symbol the
    active list walks `active/symbols`, and the fills walk the names this client has traded
    (`traded`), so a fresh process lists nothing until something is sent. The answer is
    `{code, data}`: "200000" is success, anything else carries `msg`."""

    id = "kucoin"
    label = "KuCoin"
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (ORDERS, ORDERS + "/*", ACTIVE, ACTIVE_SYMBOLS, FILLS)
    HOST = HOST
    TESTNET = "https://openapi-sandbox.kucoin.com"

    def __init__(self, gate, *, dry_run: bool = True, testnet: bool = False, **creds):
        super().__init__(gate, dry_run=dry_run, testnet=testnet, **creds)
        self.traded: set[str] = set()

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "-USDT"

    def sign(self, ts, method: str, endpoint: str, body: str = "") -> str:
        msg = f"{ts}{method.upper()}{endpoint}{body}"
        return base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    def headers(self, method: str, endpoint: str, body: str = "", ts: int | None = None) -> dict:
        ts = int(time.time() * 1000) if ts is None else int(ts)
        passphrase = base64.b64encode(hmac.new(self.secret.encode(), self.passphrase.encode(), hashlib.sha256).digest()).decode()
        return {"KC-API-KEY": self.key, "KC-API-SIGN": self.sign(ts, method, endpoint, body), "KC-API-TIMESTAMP": str(ts),
                "KC-API-PASSPHRASE": passphrase, "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"}

    def body(self, order: Order) -> dict:
        d = {"clientOid": order.client_id or uuid.uuid4().hex, "side": order.side, "symbol": self.pair(order.symbol),
             "type": "market" if order.price is None else "limit", "size": _num(order.qty)}
        if order.price is not None:
            d["price"] = _num(order.price)
            d["timeInForce"] = "GTC"
        return d

    async def _call(self, method: str, endpoint: str, body: dict | None = None) -> tuple[dict, dict]:
        content = json.dumps(body, separators=(",", ":")) if body is not None else ""
        return await self._send(method, f"{self.host}{endpoint}", content=content or None, headers=self.headers(method, endpoint, content))

    @staticmethod
    def _ok(ans: dict) -> bool:
        return str(ans.get("code")) == "200000"

    @staticmethod
    def _why(ans: dict) -> str:
        return f"{ans.get('code') or ans.get('_status')}: {str(ans.get('msg') or '')[:120]}".strip(": ")

    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._call("POST", ORDERS, self.body(order))
        self.traded.add(order.symbol)
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        if self._ok(ans):
            return OrderResult(True, self.id, str((ans.get("data") or {}).get("orderId") or ""), "accepted", raw=ans, request=req)
        return OrderResult(False, self.id, status="rejected", error=self._why(ans), raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("DELETE", f"{ORDERS}/{order_id}?symbol={self.pair(symbol)}")
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id, "dry-run", request=req)
        if self._ok(ans):
            return OrderResult(True, self.id, str((ans.get("data") or {}).get("orderId") or order_id), "accepted", raw=ans, request=req)
        return OrderResult(False, self.id, order_id, "rejected", error=self._why(ans), raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        pairs = [self.pair(symbol)] if symbol else []
        if not pairs:
            _, ans = await self._call("GET", ACTIVE_SYMBOLS)
            pairs = list(((ans.get("data") or {}).get("symbols") or [])) if self._ok(ans) else []
        out = []
        for p in pairs:
            _, ans = await self._call("GET", f"{ACTIVE}?symbol={p}")
            rows = ans.get("data") if self._ok(ans) else []
            for o in rows if isinstance(rows, list) else []:
                out.append({"order_id": str(o.get("id") or ""), "symbol": canonical(str(o.get("symbol") or p)), "side": str(o.get("side") or ""),
                            "qty": float(o.get("size") or 0.0), "price": float(o["price"]) if o.get("price") not in (None, "", "0") else None,
                            "filled": float(o.get("dealSize") or 0.0), "status": "open" if o.get("active", True) else "done"})
        return out

    async def fills(self, since: float, symbol: str | None = None) -> list[dict]:
        out = []
        for sym in ([symbol] if symbol else sorted(self.traded)):
            _, ans = await self._call("GET", f"{FILLS}?symbol={self.pair(sym)}&startAt={int(since * 1000)}&limit=100")
            items = (ans.get("data") or {}).get("items") if self._ok(ans) else []
            for f in items or []:
                out.append({"order_id": str(f.get("orderId") or ""), "symbol": canonical(str(f.get("symbol") or "")), "side": str(f.get("side") or ""),
                            "qty": float(f.get("size") or 0.0), "price": float(f.get("price") or 0.0), "fee": float(f.get("fee") or 0.0),
                            "t": float(f.get("createdAt") or 0.0) / 1000.0})
        return out
