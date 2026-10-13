"""OKX: `POST /api/v5/trade/order`, `POST /api/v5/trade/cancel-order`, `GET /api/v5/trade/orders-pending`
and `GET /api/v5/trade/fills` on www.okx.com, the USDT-margined perpetual swaps (`BTC-USDT-SWAP`)
in isolated margin and net position mode, plus the public instrument table for contract sizes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import time
import json
import urllib.parse

from ..sources import base_of
from .base import Executor, Order, OrderResult

PLACE = "/api/v5/trade/order"
CANCEL = "/api/v5/trade/cancel-order"
PENDING = "/api/v5/trade/orders-pending"
FILLS = "/api/v5/trade/fills"
INSTRUMENTS = "/api/v5/public/instruments"
# (ctVal in base units, lotSz in contracts) for the majors, from GET /api/v5/public/instruments
# on 2026-10-11: the seed that lets a dry run size an order before the live table is fetched.
SEED = {"BTC-USDT-SWAP": (0.01, 0.01), "ETH-USDT-SWAP": (0.1, 0.01), "SOL-USDT-SWAP": (1.0, 0.01), "DOGE-USDT-SWAP": (1000.0, 0.01),
        "PEPE-USDT-SWAP": (10_000_000.0, 0.1)}


def _num(x: float) -> str:
    return f"{float(x):.10f}".rstrip("0").rstrip(".") or "0"


class OKXExecutor(Executor):
    """OKX trade key + secret + passphrase: the reader's signer, OK-ACCESS-SIGN =
    base64(HMAC-SHA256(secret, timestamp + METHOD + path-with-query + body)) with the ISO
    millisecond timestamp; `x-simulated-trading: 1` on every call when `testnet` (OKX's demo is
    the same host with that header and a demo-trading key, so `TESTNET` is the production host).
    OKX sizes a swap in contracts: `sz` is the caller's base quantity over the instrument's
    `ctVal` (0.01 BTC for BTC-USDT-SWAP, 1000 DOGE, ten million PEPE), floored to `lotSz`, never
    up; a name whose contract value is unknown (not in the fetched table or `SEED`) is refused
    with a clear error rather than sized blind. The table comes from the public instruments
    endpoint (through `_send`, once an hour; nothing arrives in dry-run, so the seed stands).
    Net position mode is assumed: `posSide` is omitted and `reduceOnly` applies (long/short
    mode wants posSide and rejects these with 51000). `tdMode` is isolated. `clOrdId` is
    `order.client_id` cut to 32 alphanumerics. A placed order answers in `data[0]` with
    `sCode` "0" or the per-order rejection (`sCode`/`sMsg`) under a top-level code "1"."""

    id = "okx"
    label = "OKX"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (PLACE, CANCEL, PENDING, FILLS, INSTRUMENTS)
    HOST = "https://www.okx.com"
    TESTNET = HOST
    TABLE_TTL = 3600.0

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests
        self.table: dict[str, tuple[float, float]] = dict(SEED)
        self.table_t = 0.0

    def clock(self) -> float:
        return time.time() if self.now is None else self.now

    @staticmethod
    def stamp(now: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"

    @staticmethod
    def inst(sym: str) -> str:
        return base_of(sym) + "-USDT-SWAP"

    @staticmethod
    def canonical(inst_id: str) -> str:
        return str(inst_id or "").split("-")[0].upper() + "-USD"

    # Signing ------------------------------------------------------------------------------
    def sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        mac = hmac.new(self.secret.encode(), f"{ts}{method.upper()}{path}{body}".encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def headers(self, method: str, path: str, body: str = "", ts: str | None = None) -> dict:
        ts = self.stamp(self.clock()) if ts is None else ts
        h = {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": self.sign(ts, method, path, body), "OK-ACCESS-TIMESTAMP": ts,
             "OK-ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}
        if self.testnet:
            h["x-simulated-trading"] = "1"
        return h

    # Contract sizes -----------------------------------------------------------------------
    async def instruments(self) -> dict[str, tuple[float, float]]:
        """The live (ctVal, lotSz) table for every USDT swap, refreshed hourly; dry-run keeps the seed."""
        if self.clock() - self.table_t < self.TABLE_TTL:
            return self.table
        _, ans = await self._send("GET", f"{self.host}{INSTRUMENTS}?instType=SWAP")
        rows = ans.get("data") if str(ans.get("code")) == "0" else None
        if rows:
            self.table.update({r["instId"]: (float(r["ctVal"]), float(r.get("lotSz") or 1)) for r in rows
                               if r.get("ctVal") and str(r.get("instId", "")).endswith("-USDT-SWAP")})
            self.table_t = self.clock()
        return self.table

    def contracts(self, inst_id: str, qty: float) -> str:
        """Base units to a contract count floored to the lot size; raises when the value is unknown."""
        if inst_id not in self.table:
            raise ValueError(f"OKX: contract value of {inst_id} unknown (not in the instrument table); refusing to size it")
        ct_val, lot = self.table[inst_id]
        n = math.floor(qty / ct_val / lot + 1e-9) * lot
        if n <= 0:
            raise ValueError(f"OKX: {qty:g} is under one lot of {inst_id} ({lot:g} x {ct_val:g})")
        return _num(n)

    def order_body(self, order: Order) -> dict:
        inst_id = self.inst(order.symbol)
        b = {"instId": inst_id, "tdMode": "isolated", "side": order.side, "ordType": "market" if order.price is None else "limit",
             "sz": self.contracts(inst_id, order.qty), "reduceOnly": bool(order.reduce_only)}
        if order.price is not None:
            b["px"] = _num(order.price)
        cid = re.sub(r"[^A-Za-z0-9]", "", order.client_id or "")[:32]
        if cid:
            b["clOrdId"] = cid
        return b

    async def _post(self, path: str, body: dict) -> tuple[dict, dict]:
        payload = json.dumps(body, separators=(",", ":"))
        return await self._send("POST", f"{self.host}{path}", content=payload, headers=self.headers("POST", path, payload))

    async def _get(self, path: str, params: dict) -> list:
        q = "?" + urllib.parse.urlencode(params)
        _, ans = await self._send("GET", f"{self.host}{path}{q}", headers=self.headers("GET", path + q))
        return (ans.get("data") or []) if str(ans.get("code")) == "0" else []

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        rows = ans.get("data") if isinstance(ans.get("data"), list) else []
        row = rows[0] if rows else {}
        if str(ans.get("code")) == "0" and str(row.get("sCode", "0")) == "0":
            return OrderResult(True, self.id, order_id=str(row.get("ordId") or order_id), status="accepted", raw=ans, request=req)
        err = f"{row.get('sMsg')} ({row.get('sCode')})" if row.get("sCode") else f"{ans.get('msg')} ({ans.get('code', ans.get('_status'))})"
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=err[:160], raw=ans, request=req)

    # The contract -------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        await self.instruments()
        req, ans = await self._post(PLACE, self.order_body(order))
        return self._result(req, ans)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._post(CANCEL, {"instId": self.inst(symbol), "ordId": str(order_id)})
        return self._result(req, ans, order_id=order_id)

    def _row(self, inst_id: str, contracts, price) -> tuple[str, float, float]:
        ct_val = self.table.get(inst_id, (1.0, 1.0))[0]            # an unknown name stays in contracts
        return self.canonical(inst_id), float(contracts or 0) * ct_val, float(price or 0)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        await self.instruments()
        params = {"instType": "SWAP"} | ({"instId": self.inst(symbol)} if symbol else {})
        out = []
        for o in await self._get(PENDING, params):
            sym, qty, px = self._row(o.get("instId", ""), o.get("sz"), o.get("px"))
            out.append({"order_id": str(o.get("ordId")), "symbol": sym, "side": str(o.get("side", "")).lower(), "qty": qty, "price": px,
                        "filled": self._row(o.get("instId", ""), o.get("accFillSz"), 0)[1], "status": str(o.get("state", "")).lower()})
        return out

    async def fills(self, since: float) -> list[dict]:
        await self.instruments()
        out = []
        for f in await self._get(FILLS, {"instType": "SWAP", "begin": int(since * 1000), "limit": 100}):
            sym, qty, px = self._row(f.get("instId", ""), f.get("fillSz"), f.get("fillPx"))
            out.append({"order_id": str(f.get("ordId")), "symbol": sym, "side": str(f.get("side", "")).lower(), "qty": qty, "price": px,
                        "fee": -float(f.get("fee") or 0), "t": float(f.get("ts") or 0) / 1000.0})   # OKX's fee is negative when charged
        return out
