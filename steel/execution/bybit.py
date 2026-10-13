"""Bybit: `POST /v5/order/create`, `POST /v5/order/cancel`, `GET /v5/order/realtime` and
`GET /v5/execution/list` on api.bybit.com (api-testnet.bybit.com when `testnet`), the
USDT-settled linear perps (`category: linear`) in one-way mode, nothing else."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.parse

from ..sources import base_of, unscale
from .base import Executor, Order, OrderResult

CREATE = "/v5/order/create"
CANCEL = "/v5/order/cancel"
OPEN_ORDERS = "/v5/order/realtime"
EXECUTIONS = "/v5/execution/list"
# Bybit's scaled linear perps (instruments-info, 2026-10-11) that `unscale` can invert; its
# 10000SATSUSDT and 10000NEXUSDT use a prefix `unscale` does not know and are left unmapped.
SCALED = {"PEPE": "1000", "FLOKI": "1000", "BONK": "1000", "LUNC": "1000", "XEC": "1000", "RATS": "1000", "CAT": "1000", "TOSHI": "1000",
          "TURBO": "1000", "NEIROCTO": "1000", "BTT": "1000", "TAG": "1000", "BABYDOGE": "1000000", "MOG": "1000000"}
WEEK_MS = 7 * 86_400_000


def _num(x: float) -> str:
    return f"{float(x):.10f}".rstrip("0").rstrip(".") or "0"


class BybitExecutor(Executor):
    """Bybit trade key + secret: the reader's signer, X-BAPI-SIGN = hex(HMAC-SHA256(secret,
    timestamp + key + recv_window + payload)) where the payload is the query string of a GET or
    the exact JSON body of a POST (sent as-is, so the bytes signed are the bytes sent), beside
    X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-RECV-WINDOW and X-BAPI-SIGN-TYPE 2. One-way mode
    is assumed and declared with `positionIdx: 0` (hedge mode wants 1 for the long leg and 2 for
    the short). A market order is IOC on Bybit whatever is sent; a limit order is GTC. `qty` is
    the caller's base units over the contract factor, as a string, unrounded: the lot step
    (`qtyStep`, 100 for 1000PEPEUSDT) lives in instruments-info, so an off-step quantity is the
    venue's rejection. `orderLinkId` is `order.client_id` cut to 36 of [A-Za-z0-9_-]. The
    create answer is an ack (orderId); fills show up in `fills`, whose window Bybit caps at 7
    days (`since` older than that is clamped). An unknown key gets a bodiless 401 from the edge."""

    id = "bybit"
    label = "Bybit"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (CREATE, CANCEL, OPEN_ORDERS, EXECUTIONS)
    HOST = "https://api.bybit.com"
    TESTNET = "https://api-testnet.bybit.com"
    RECV_WINDOW = 5000
    SCALED = SCALED

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests

    def stamp(self) -> int:
        return int((time.time() if self.now is None else self.now) * 1000)

    @classmethod
    def contract(cls, sym: str) -> tuple[str, float]:
        """BTC-USD -> ("BTCUSDT", 1.0); PEPE-USD -> ("1000PEPEUSDT", 1000.0)."""
        base = base_of(sym)
        prefix = cls.SCALED.get(base, "")
        return f"{prefix}{base}USDT", unscale(prefix + base)[1]

    @staticmethod
    def canonical(symbol: str) -> tuple[str, float]:
        base, factor = unscale(str(symbol or "").upper().removesuffix("USDT"))
        return f"{base}-USD", factor

    # Signing ------------------------------------------------------------------------------
    def sign(self, ts, payload: str) -> str:
        return hmac.new(self.secret.encode(), f"{ts}{self.key}{self.RECV_WINDOW}{payload}".encode(), hashlib.sha256).hexdigest()

    def headers(self, payload: str, ts=None) -> dict:
        ts = str(self.stamp() if ts is None else ts)
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(self.RECV_WINDOW), "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-SIGN": self.sign(ts, payload), "Content-Type": "application/json"}

    def order_body(self, order: Order) -> dict:
        symbol, factor = self.contract(order.symbol)
        market = order.price is None
        b = {"category": "linear", "symbol": symbol, "side": "Buy" if order.side == "buy" else "Sell", "orderType": "Market" if market else "Limit",
             "qty": _num(order.qty / factor), "timeInForce": "IOC" if market else "GTC", "reduceOnly": bool(order.reduce_only), "positionIdx": 0}
        if not market:
            b["price"] = _num(order.price * factor)
        cid = re.sub(r"[^A-Za-z0-9_-]", "", order.client_id or "")[:36]
        if cid:
            b["orderLinkId"] = cid
        return b

    async def _post(self, path: str, body: dict) -> tuple[dict, dict]:
        payload = json.dumps(body, separators=(",", ":"))
        return await self._send("POST", f"{self.host}{path}", content=payload, headers=self.headers(payload))

    async def _get(self, path: str, params: dict) -> dict:
        q = urllib.parse.urlencode(params)
        _, ans = await self._send("GET", f"{self.host}{path}?{q}", headers=self.headers(q))
        return (ans.get("result") or {}) if ans.get("retCode") == 0 else {}

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        if ans.get("retCode") == 0:
            res = ans.get("result") or {}
            return OrderResult(True, self.id, order_id=str(res.get("orderId") or order_id), status="accepted", raw=ans, request=req)
        msg = f"{ans.get('retMsg')} ({ans.get('retCode')})" if "retCode" in ans else f"HTTP {ans.get('_status')}: {ans.get('text', '')}"
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=msg[:160], raw=ans, request=req)

    # The contract -------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._post(CREATE, self.order_body(order))
        return self._result(req, ans)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._post(CANCEL, {"category": "linear", "symbol": self.contract(symbol)[0], "orderId": str(order_id)})
        return self._result(req, ans, order_id=order_id)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"category": "linear", "limit": 50}
        params.update({"symbol": self.contract(symbol)[0]} if symbol else {"settleCoin": "USDT"})
        out = []
        for o in (await self._get(OPEN_ORDERS, params)).get("list") or []:
            sym, factor = self.canonical(o.get("symbol", ""))
            out.append({"order_id": str(o.get("orderId")), "symbol": sym, "side": str(o.get("side", "")).lower(), "qty": float(o.get("qty") or 0) * factor,
                        "price": float(o.get("price") or 0) / factor, "filled": float(o.get("cumExecQty") or 0) * factor,
                        "status": str(o.get("orderStatus", "")).lower()})
        return out

    async def fills(self, since: float) -> list[dict]:
        start = max(int(since * 1000), self.stamp() - WEEK_MS)
        out = []
        for f in (await self._get(EXECUTIONS, {"category": "linear", "startTime": start, "limit": 100})).get("list") or []:
            if f.get("execType") not in (None, "Trade"):
                continue
            sym, factor = self.canonical(f.get("symbol", ""))
            out.append({"order_id": str(f.get("orderId")), "symbol": sym, "side": str(f.get("side", "")).lower(), "qty": float(f.get("execQty") or 0) * factor,
                        "price": float(f.get("execPrice") or 0) / factor, "fee": float(f.get("execFee") or 0), "t": float(f.get("execTime") or 0) / 1000.0})
        return out
