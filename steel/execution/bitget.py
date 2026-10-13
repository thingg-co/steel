"""Bitget USDT-margined futures: `POST /api/v2/mix/order/place-order`, `POST /api/v2/mix/order/cancel-order`,
`GET /api/v2/mix/order/orders-pending` and `GET /api/v2/mix/order/fills` on api.bitget.com
(`productType: USDT-FUTURES`), in isolated margin and one-way position mode, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse

from ..sources import base_of, unscale
from .base import Executor, Order, OrderResult

PLACE = "/api/v2/mix/order/place-order"
CANCEL = "/api/v2/mix/order/cancel-order"
PENDING = "/api/v2/mix/order/orders-pending"
FILLS = "/api/v2/mix/order/fills"
PRODUCT = "USDT-FUTURES"
# Bitget's scaled USDT perps (GET /api/v2/mix/market/contracts, 2026-10-11): PEPE, SHIB, FLOKI and
# LUNC trade unscaled here (PEPEUSDT with a 1000-token lot), unlike Binance and Bybit.
SCALED = {"BONK": "1000", "XEC": "1000", "SATS": "1000", "RATS": "1000", "CAT": "1000", "MOG": "1000000", "BABYDOGE": "1M", "CHEEMS": "1M"}


def _num(x: float) -> str:
    return f"{float(x):.10f}".rstrip("0").rstrip(".") or "0"


class BitgetExecutor(Executor):
    """Bitget trade key + secret + passphrase: the reader's signer, ACCESS-SIGN =
    base64(HMAC-SHA256(secret, timestamp + METHOD + path + "?query" + body)) with a millisecond
    ACCESS-TIMESTAMP, ACCESS-KEY, ACCESS-PASSPHRASE and `locale`; the JSON body is signed and
    sent as the same string. `paptrading: 1` on every call when `testnet`: Bitget's demo is the
    same host with that header and a demo key made in the account's demo mode, so `TESTNET` is
    the production host. One-way position mode is assumed: `tradeSide` (open/close, hedge mode
    only) is omitted and `reduceOnly` "YES"/"NO" is sent, which Bitget honours in one-way mode
    only. `size` is in base coin (over the factor for its 1000x names), unrounded: the step
    (`volumePlace`, `minTradeNum`) is per contract and only in the contracts endpoint, so an
    off-step size is the venue's rejection. A limit order is `force: gtc`; a market order sends
    no `force` (the docs require it for limit orders only). `clientOid` is `order.client_id` cut
    to 40 of [A-Za-z0-9_-]. A reduce-only order that trims older reduce-only orders may answer
    without an `orderId`; the result then carries the clientOid."""

    id = "bitget"
    label = "Bitget futures"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (PLACE, CANCEL, PENDING, FILLS)
    HOST = "https://api.bitget.com"
    TESTNET = HOST
    SCALED = SCALED

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests

    def stamp(self) -> int:
        return int((time.time() if self.now is None else self.now) * 1000)

    @classmethod
    def contract(cls, sym: str) -> tuple[str, float]:
        """BTC-USD -> ("BTCUSDT", 1.0); BONK-USD -> ("1000BONKUSDT", 1000.0); PEPE-USD -> ("PEPEUSDT", 1.0)."""
        base = base_of(sym)
        prefix = cls.SCALED.get(base, "")
        return f"{prefix}{base}USDT", unscale(prefix + base)[1]

    @staticmethod
    def canonical(symbol: str) -> tuple[str, float]:
        base, factor = unscale(str(symbol or "").upper().removesuffix("USDT"))
        return f"{base}-USD", factor

    # Signing ------------------------------------------------------------------------------
    def sign(self, ts, method: str, path: str, query: str = "", body: str = "") -> str:
        msg = f"{ts}{method.upper()}{path}{'?' + query if query else ''}{body}"
        return base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    def headers(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> dict:
        ts = self.stamp() if ts is None else int(ts)
        h = {"ACCESS-KEY": self.key, "ACCESS-SIGN": self.sign(ts, method, path, query, body), "ACCESS-TIMESTAMP": str(ts),
             "ACCESS-PASSPHRASE": self.passphrase, "locale": "en-US", "Content-Type": "application/json"}
        if self.testnet:
            h["paptrading"] = "1"
        return h

    def order_body(self, order: Order) -> dict:
        symbol, factor = self.contract(order.symbol)
        b = {"symbol": symbol, "productType": PRODUCT, "marginMode": "isolated", "marginCoin": "USDT", "size": _num(order.qty / factor),
             "side": order.side, "orderType": "market" if order.price is None else "limit", "reduceOnly": "YES" if order.reduce_only else "NO"}
        if order.price is not None:
            b["price"], b["force"] = _num(order.price * factor), "gtc"
        cid = re.sub(r"[^A-Za-z0-9_-]", "", order.client_id or "")[:40]
        if cid:
            b["clientOid"] = cid
        return b

    async def _post(self, path: str, body: dict) -> tuple[dict, dict]:
        payload = json.dumps(body, separators=(",", ":"))
        return await self._send("POST", f"{self.host}{path}", content=payload, headers=self.headers("POST", path, body=payload))

    async def _get(self, path: str, params: dict) -> dict:
        q = urllib.parse.urlencode(params)
        _, ans = await self._send("GET", f"{self.host}{path}?{q}", headers=self.headers("GET", path, q))
        return (ans.get("data") or {}) if str(ans.get("code")) == "00000" else {}

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        if str(ans.get("code")) == "00000":
            d = ans.get("data") or {}
            return OrderResult(True, self.id, order_id=str(d.get("orderId") or d.get("clientOid") or order_id), status="accepted", raw=ans, request=req)
        err = f"{ans.get('msg')} ({ans.get('code')})" if "code" in ans else f"HTTP {ans.get('_status')}: {ans.get('text', '')}"
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=err[:160], raw=ans, request=req)

    # The contract -------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._post(PLACE, self.order_body(order))
        return self._result(req, ans)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._post(CANCEL, {"symbol": self.contract(symbol)[0], "productType": PRODUCT, "marginCoin": "USDT", "orderId": str(order_id)})
        return self._result(req, ans, order_id=order_id)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"productType": PRODUCT, "limit": 100} | ({"symbol": self.contract(symbol)[0]} if symbol else {})
        out = []
        for o in (await self._get(PENDING, params)).get("entrustedList") or []:
            sym, factor = self.canonical(o.get("symbol", ""))
            out.append({"order_id": str(o.get("orderId")), "symbol": sym, "side": str(o.get("side", "")).lower(), "qty": float(o.get("size") or 0) * factor,
                        "price": float(o.get("price") or 0) / factor, "filled": float(o.get("baseVolume") or 0) * factor, "status": str(o.get("status", "")).lower()})
        return out

    async def fills(self, since: float) -> list[dict]:
        out = []
        for f in (await self._get(FILLS, {"productType": PRODUCT, "startTime": int(since * 1000), "limit": 100})).get("fillList") or []:
            sym, factor = self.canonical(f.get("symbol", ""))
            fee = -sum(float(x.get("totalFee") or 0) for x in f.get("feeDetail") or [])      # Bitget's fee is negative when charged
            out.append({"order_id": str(f.get("orderId")), "symbol": sym, "side": str(f.get("side", "")).lower(), "qty": float(f.get("baseVolume") or 0) * factor,
                        "price": float(f.get("price") or 0) / factor, "fee": fee, "t": float(f.get("cTime") or 0) / 1000.0})
        return out
