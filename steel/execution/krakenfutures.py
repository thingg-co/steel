"""Kraken Futures orders on the PF_ (flex) perpetuals: `POST /derivatives/api/v3/sendorder` and
`cancelorder`, `GET openorders` and `fills`. The "Full access" key permission; the demo host is
a separate self-service platform with its own keys (demo-futures.kraken.com)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from ..sources.krakenfutures import KrakenFuturesSource
from .base import Executor, Order, OrderResult

HOST = "https://futures.kraken.com"
SEND = "/derivatives/api/v3/sendorder"
CANCEL = "/derivatives/api/v3/cancelorder"
OPEN = "/derivatives/api/v3/openorders"
FILLS = "/derivatives/api/v3/fills"
LIVE = ("placed", "partiallyFilled", "filled")   # sendStatus.status values that mean the venue took the order


def _dec(x: float, places: int) -> str:
    """`x` as a plain decimal string cut (never rounded up) to `places` decimals."""
    s = format(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _iso(s: str) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class KrakenFuturesExecutor(Executor):
    """Kraken Futures API key + secret, signed as the account reader does: Authent =
    base64(HMAC-SHA512(b64decode(secret), SHA-256(postData + nonce + endpointPath))) with the
    endpoint path stripped of `/derivatives`, headers APIKey / Authent / Nonce. Every parameter
    travels in the query string, on POSTs too (the API spec marks them `in: query`, and
    postData is that URL-encoded string as sent), so a POST here carries no body. The PF_
    contracts are linear, USD-margined and sized in the coin (`contractSize` 1), so `size` is
    the base quantity: its precision (`contractValueTradePrecision`, 4 for BTC, -3 for PEPE)
    is per instrument and not known here, so the quantity is passed to 8 decimals and the venue
    rejects an off-step size. `orderType` is `mkt` or `lmt`; `reduceOnly` rejects an order that
    would grow the position; `cliOrdId` is up to 100 characters and must be globally unique.
    A market order that executed comes back as `placed` with EXECUTION events, so the fill is
    read from the events, not the status. `/fills` has no "since": without `lastFillTime` it
    returns the newest 100, which `fills` filters by time; a fill carries no fee field."""

    id = "krakenfutures"
    label = "Kraken Futures"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (SEND, CANCEL, OPEN, FILLS)
    HOST = HOST
    TESTNET = "https://demo-futures.kraken.com"
    PLACES = 8

    pair = staticmethod(KrakenFuturesSource.pair)       # BTC-USD -> PF_XBTUSD
    unpair = staticmethod(KrakenFuturesSource.unpair)   # PF_XBTUSD -> BTC-USD

    def sign(self, path: str, post_data: str = "", nonce: str = "") -> str:
        endpoint = path[len("/derivatives"):] if path.startswith("/derivatives") else path
        digest = hashlib.sha256((post_data + nonce + endpoint).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def headers(self, path: str, post_data: str = "", nonce: str | None = None) -> dict:
        nonce = str(int(time.time() * 1000)) if nonce is None else nonce
        return {"APIKey": self.key, "Authent": self.sign(path, post_data, nonce), "Nonce": nonce}

    async def _call(self, method: str, path: str, params: dict | None = None) -> tuple[dict, dict]:
        post_data = urllib.parse.urlencode(params or {})
        url = f"{self.host}{path}" + (f"?{post_data}" if post_data else "")
        return await self._send(method, url, headers=self.headers(path, post_data))

    @staticmethod
    def error_of(ans: dict) -> str:
        return str(ans.get("error") or ans.get("errors") or f"HTTP {ans.get('_status')}")[:160]

    def request(self, order: Order) -> dict:
        p = {"orderType": "mkt" if order.price is None else "lmt", "symbol": self.pair(order.symbol), "side": order.side,
             "size": _dec(order.qty, self.PLACES)}
        if order.price is not None:
            p["limitPrice"] = _dec(order.price, 10)
        if order.reduce_only:
            p["reduceOnly"] = "true"
        if order.client_id:
            p["cliOrdId"] = order.client_id[:100]
        return p

    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._call("POST", SEND, self.request(order))
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        st = ans.get("sendStatus") or {}
        if ans.get("result") != "success" or not st:
            return OrderResult(False, self.id, status="rejected", error=self.error_of(ans), raw=ans, request=req)
        status, oid = str(st.get("status") or ""), str(st.get("order_id") or "")
        execs = [e for e in st.get("orderEvents") or [] if e.get("type") == "EXECUTION"]
        qty = sum(float(e.get("amount") or 0.0) for e in execs)
        avg = sum(float(e.get("amount") or 0.0) * float(e.get("price") or 0.0) for e in execs) / qty if qty > 0 else None
        if status not in LIVE:
            return OrderResult(False, self.id, order_id=oid, status="rejected", error=status, raw=ans, request=req)
        done = status == "filled" or qty >= order.qty * (1 - 1e-9)
        return OrderResult(True, self.id, order_id=oid, status="filled" if done else "accepted", filled_qty=qty, avg_price=avg,
                           raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("POST", CANCEL, {"order_id": order_id})
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        st = ans.get("cancelStatus") or {}
        ok = ans.get("result") == "success" and st.get("status") == "cancelled"
        err = "" if ok else (str(st.get("status")) if st else self.error_of(ans))
        return OrderResult(ok, self.id, order_id=order_id, status="cancelled" if ok else "rejected", error=err, raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._call("GET", OPEN)
        out = []
        for o in ans.get("openOrders") or []:
            sym = self.unpair(o.get("symbol"))
            if sym is None or (symbol and sym != symbol):
                continue
            filled, left = float(o.get("filledSize") or 0.0), float(o.get("unfilledSize") or 0.0)
            out.append({"order_id": str(o.get("order_id") or ""), "symbol": sym, "side": o.get("side"), "qty": filled + left,
                        "price": o.get("limitPrice"), "filled": filled, "status": o.get("status"), "client_id": o.get("cliOrdId") or ""})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._call("GET", FILLS)
        out = []
        for f in ans.get("fills") or []:
            sym, t = self.unpair(f.get("symbol")), _iso(f.get("fillTime"))
            if sym is None or t < since:
                continue
            out.append({"order_id": str(f.get("order_id") or ""), "symbol": sym, "side": f.get("side"), "qty": float(f.get("size") or 0.0),
                        "price": float(f.get("price") or 0.0), "fee": None, "t": t})
        return out
