"""Binance USD-M futures: `POST /fapi/v1/order`, `DELETE /fapi/v1/order`, `GET /fapi/v1/openOrders`
and `GET /fapi/v1/userTrades` on fapi.binance.com (testnet.binancefuture.com when `testnet`).
USDT-margined linear perps in one-way position mode, nothing else."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.parse

from ..sources import base_of, unscale
from .base import Executor, Order, OrderResult

ORDER = "/fapi/v1/order"
OPEN_ORDERS = "/fapi/v1/openOrders"
TRADES = "/fapi/v1/userTrades"
# The bases Binance lists as scaled contracts (fapi exchangeInfo, 2026-10-11): 1000PEPEUSDT is a
# thousand PEPE, 1MBABYDOGEUSDT a million. Sizes and prices scale through `unscale`'s factor.
SCALED = {"PEPE": "1000", "SHIB": "1000", "FLOKI": "1000", "BONK": "1000", "LUNC": "1000", "XEC": "1000", "SATS": "1000",
          "RATS": "1000", "CAT": "1000", "CHEEMS": "1000", "BABYDOGE": "1M", "MOG": "1000000"}
STATUS = {"NEW": "accepted", "PARTIALLY_FILLED": "accepted", "FILLED": "filled", "CANCELED": "rejected", "EXPIRED": "rejected",
          "REJECTED": "rejected", "EXPIRED_IN_MATCH": "rejected"}


def _num(x: float) -> str:
    return f"{float(x):.10f}".rstrip("0").rstrip(".") or "0"


def _scrub_sig(url: str) -> str:
    return re.sub(r"signature=[0-9a-f]+", "signature=***", url)


class BinanceExecutor(Executor):
    """Binance futures trade key + secret: the reader's signer (HMAC-SHA256 hex over the query
    string, `timestamp` and `recvWindow` included, `signature` last; the key in X-MBX-APIKEY).
    Every call, POST and DELETE included, carries its signed query in the URL, which Binance
    accepts for every method; the signature is masked in the logged request. One-way position
    mode is assumed: `reduceOnly` is sent and `positionSide` is not (hedge mode needs
    positionSide LONG/SHORT and refuses reduceOnly, so an account in hedge mode rejects these
    orders rather than mis-sizing them). Quantities are passed as the caller's base units (over
    the contract factor for 1000x names) with no rounding: the lot step is per symbol and only
    in exchangeInfo, so a quantity off the step is the venue's -1111 rejection, never an
    upsized order. `newClientOrderId` is `order.client_id` cut to Binance's
    `^[.A-Z:/a-z0-9_-]{1,36}$`. `fills` needs a symbol: without one it asks for every name this
    client has ordered in this process."""

    id = "binance"
    label = "Binance futures"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}]
    endpoints = (ORDER, OPEN_ORDERS, TRADES)
    HOST = "https://fapi.binance.com"
    TESTNET = "https://testnet.binancefuture.com"
    RECV_WINDOW = 5000
    SCALED = SCALED

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests
        self.recent: set[str] = set()            # venue symbols this client has sent orders for

    def stamp(self) -> int:
        return int((time.time() if self.now is None else self.now) * 1000)

    # Symbols ------------------------------------------------------------------------------
    @classmethod
    def contract(cls, sym: str) -> tuple[str, float]:
        """BTC-USD -> ("BTCUSDT", 1.0); PEPE-USD -> ("1000PEPEUSDT", 1000.0)."""
        base = base_of(sym)
        prefix = cls.SCALED.get(base, "")
        return f"{prefix}{base}USDT", unscale(prefix + base)[1]

    @staticmethod
    def canonical(symbol: str) -> tuple[str, float]:
        """1000PEPEUSDT -> ("PEPE-USD", 1000.0)."""
        base, factor = unscale(str(symbol or "").upper().removesuffix("USDT"))
        return f"{base}-USD", factor

    # Signing ------------------------------------------------------------------------------
    def sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def query(self, params: dict | None = None, ts: int | None = None) -> str:
        p = dict(params or {})
        p["timestamp"] = self.stamp() if ts is None else int(ts)
        p["recvWindow"] = self.RECV_WINDOW
        q = urllib.parse.urlencode(p)
        return f"{q}&signature={self.sign(q)}"

    def headers(self) -> dict:
        return {"X-MBX-APIKEY": self.key}

    def order_params(self, order: Order) -> dict:
        symbol, factor = self.contract(order.symbol)
        p = {"symbol": symbol, "side": order.side.upper(), "type": "MARKET" if order.price is None else "LIMIT",
             "quantity": _num(order.qty / factor), "reduceOnly": "true" if order.reduce_only else "false"}
        if order.price is not None:
            p["timeInForce"], p["price"] = "GTC", _num(order.price * factor)
        cid = re.sub(r"[^.A-Za-z0-9:/_-]", "", order.client_id or "")[:36]
        if cid:
            p["newClientOrderId"] = cid
        return p

    async def _call(self, method: str, path: str, params: dict) -> tuple[dict, dict]:
        req, ans = await self._send(method, f"{self.host}{path}?{self.query(params)}", headers=self.headers())
        req["url"] = _scrub_sig(req["url"])
        return req, ans

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        if "code" in ans and "orderId" not in ans:                       # {"code": -2022, "msg": "ReduceOnly Order is rejected."}
            return OrderResult(False, self.id, order_id=order_id, status="rejected", error=f"{ans.get('msg')} ({ans.get('code')})"[:160],
                               raw=ans, request=req)
        status = STATUS.get(str(ans.get("status")), "accepted")
        filled = float(ans.get("executedQty") or 0.0)
        avg = float(ans.get("avgPrice") or 0.0) or (float(ans.get("cumQuote") or 0.0) / filled if filled else 0.0)
        _, factor = self.canonical(ans.get("symbol", ""))
        return OrderResult(status != "rejected", self.id, order_id=str(ans.get("orderId") or order_id), status=status, filled_qty=filled * factor,
                           avg_price=avg / factor if avg else None, error="" if status != "rejected" else str(ans.get("status")), raw=ans, request=req)

    # The contract -------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        params = self.order_params(order)
        self.recent.add(params["symbol"])
        req, ans = await self._call("POST", ORDER, params)
        return self._result(req, ans)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("DELETE", ORDER, {"symbol": self.contract(symbol)[0], "orderId": order_id})
        return self._result(req, ans, order_id=order_id)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._call("GET", OPEN_ORDERS, {"symbol": self.contract(symbol)[0]} if symbol else {})
        out = []
        for o in ans.get("data") if isinstance(ans.get("data"), list) else []:
            sym, factor = self.canonical(o.get("symbol", ""))
            out.append({"order_id": str(o.get("orderId")), "symbol": sym, "side": str(o.get("side", "")).lower(), "qty": float(o.get("origQty") or 0) * factor,
                        "price": float(o.get("price") or 0) / factor, "filled": float(o.get("executedQty") or 0) * factor, "status": str(o.get("status", "")).lower()})
        return out

    async def fills(self, since: float, symbol: str | None = None) -> list[dict]:
        out = []
        for venue_sym in ([self.contract(symbol)[0]] if symbol else sorted(self.recent)):
            _, ans = await self._call("GET", TRADES, {"symbol": venue_sym, "startTime": int(since * 1000), "limit": 1000})
            for f in ans.get("data") if isinstance(ans.get("data"), list) else []:
                sym, factor = self.canonical(f.get("symbol", ""))
                out.append({"order_id": str(f.get("orderId")), "symbol": sym, "side": str(f.get("side", "")).lower(), "qty": float(f.get("qty") or 0) * factor,
                            "price": float(f.get("price") or 0) / factor, "fee": float(f.get("commission") or 0), "t": float(f.get("time") or 0) / 1000.0})
        return out
