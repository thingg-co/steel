"""Gate.io USDT-settled perpetual orders: `POST /api/v4/futures/usdt/orders`, `DELETE
.../orders/{id}`, `GET .../orders?status=open` and `GET .../my_trades_timerange`, sized in contracts
of `quanto_multiplier` from `GET .../contracts/{contract}`. The Gate.io venue's second client
(the spot one is `gateio`): its trade key is `gateio_perp-trade.json`, or the venue's
`gateio-trade.json` when that is the only one saved. A key with the Perpetual Futures permission."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal

from ..config import read_creds
from ..sources import unscale
from ..sources.gateio import GateIOSource
from .base import Executor, Order, OrderResult

HOST = "https://api.gateio.ws"
ORDERS = "/api/v4/futures/usdt/orders"
CONTRACTS = "/api/v4/futures/usdt/contracts/"
TRADES = "/api/v4/futures/usdt/my_trades_timerange"


def _dec(x: float, places: int) -> str:
    s = format(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class GateIOPerpExecutor(Executor):
    """Gate.io API key + secret, signed as the account reader does: SIGN = hex HMAC-SHA512 of
    "METHOD\\npath\\nquery\\nsha512hex(body)\\ntimestamp" with a second-resolution Timestamp,
    headers KEY / Timestamp / SIGN (the docs' worked examples reproduce). The body is signed as
    the exact compact JSON string sent. `size` is a SIGNED whole number of contracts (positive
    buys, negative sells) of `quanto_multiplier` base units (0.0001 BTC; 10,000,000 PEPE), cut
    down and refused under `order_size_min`, so the contract must be in `contracts` first:
    `contract_of()` fetches it once through the gate (nothing is fetched in dry-run: seed
    `contracts` from the public contract endpoint). A market order is `price` "0" with `tif`
    `ioc`; a limit is `gtc`. `reduce_only` is a boolean; `text` is "t-" plus up to 28 bytes of
    [0-9A-Za-z_.-], the client id sanitized. An order answers as a FuturesOrder: `status`
    `open` is accepted; `finished` with `finish_as` `filled` (or `ioc` with something filled) is
    filled, `left` being the unfilled contracts; any other `finish_as` is a rejection, as is
    a `{label, message}` error body. `my_trades` has no `from`, `my_trades_timerange` does (seconds)."""

    id = "gateio_perp"
    label = "Gate.io USDT perps"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (ORDERS, ORDERS + "/*", CONTRACTS + "*", TRADES)
    HOST = HOST
    TESTNET = "https://api-testnet.gateapi.io"

    contract = staticmethod(GateIOSource.pair)      # BTC-USD -> BTC_USDT

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.contracts: dict[str, dict] = {}        # contract -> {mult (base per contract), min, max}

    @classmethod
    def load(cls, gate, *, dry_run: bool = True, testnet: bool = False) -> "GateIOPerpExecutor":
        """`gateio_perp-trade.json`, else the venue's shared `gateio-trade.json`."""
        names = [f["id"] for f in cls.fields]
        creds = read_creds("gateio_perp-trade", names)
        if not all(creds.values()):
            creds = read_creds("gateio-trade", names)
        return cls(gate, dry_run=dry_run, testnet=testnet, **creds)

    def sign(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> str:
        ts = int(time.time()) if ts is None else int(ts)
        msg = "\n".join([method.upper(), path, query, hashlib.sha512(body.encode()).hexdigest(), str(ts)])
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha512).hexdigest()

    def headers(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> dict:
        ts = int(time.time()) if ts is None else int(ts)
        return {"KEY": self.key, "Timestamp": str(ts), "SIGN": self.sign(method, path, query, body, ts), "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, query: str = "", body: dict | None = None) -> tuple[dict, dict]:
        content = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.host}{path}" + (f"?{query}" if query else "")
        return await self._send(method, url, content=content or None, headers=self.headers(method, path, query, content))

    @staticmethod
    def symbol(contract: str) -> tuple[str, float]:
        base, factor = unscale(str(contract or "").split("_")[0])
        return f"{base.upper()}-USD", factor

    async def contract_of(self, symbol: str) -> dict | None:
        name = self.contract(symbol)
        if name not in self.contracts:
            _, ans = await self._call("GET", CONTRACTS + name)
            if ans.get("name") == name and ans.get("quanto_multiplier"):
                self.contracts[name] = {"mult": float(ans["quanto_multiplier"]) * self.symbol(name)[1], "min": int(ans.get("order_size_min") or 1),
                                        "max": int(ans.get("order_size_max") or 0)}
        return self.contracts.get(name)

    def size(self, symbol: str, qty: float, side: str) -> int:
        c = self.contracts.get(self.contract(symbol))
        if not c:
            raise LookupError(f"{symbol}: contract size unknown ({self.contract(symbol)} has not been fetched)")
        n = int(Decimal(str(qty)) / Decimal(str(c["mult"])))                         # toward zero: never upsize
        if n < max(1, c["min"]):
            raise ValueError(f"{symbol}: {qty} is under the minimum order of {max(1, c['min'])} contracts of {c['mult']:g}")
        if c["max"] and n > c["max"]:
            raise ValueError(f"{symbol}: {n} contracts is over the venue's maximum {c['max']}")
        return n if side == "buy" else -n

    async def _place(self, order: Order) -> OrderResult:
        await self.contract_of(order.symbol)
        try:
            n = self.size(order.symbol, order.qty, order.side)
        except (LookupError, ValueError) as e:
            return OrderResult(False, self.id, status="refused", error=str(e))
        name = self.contract(order.symbol)
        factor = self.symbol(name)[1]
        body = {"contract": name, "size": n, "price": "0" if order.price is None else _dec(order.price * factor, 12),
                "tif": "ioc" if order.price is None else "gtc", "reduce_only": bool(order.reduce_only)}
        if order.client_id:
            body["text"] = "t-" + re.sub(r"[^0-9A-Za-z_.-]", "", order.client_id)[:28]
        req, ans = await self._call("POST", ORDERS, body=body)
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        return self._result(req, ans, name)

    def _result(self, req: dict, ans: dict, name: str) -> OrderResult:
        if ans.get("label") or "id" not in ans:
            err = f"{ans.get('label') or 'HTTP ' + str(ans.get('_status'))}: {ans.get('message') or ans.get('detail') or ''}"[:160]
            return OrderResult(False, self.id, status="rejected", error=err, raw=ans, request=req)
        c = self.contracts.get(name) or {"mult": 1.0}
        oid, status, how = str(ans.get("id") or ""), str(ans.get("status") or ""), str(ans.get("finish_as") or "")
        size, left = abs(float(ans.get("size") or 0.0)), abs(float(ans.get("left") or 0.0))
        filled = (size - left) * c["mult"]
        px = float(ans.get("fill_price") or 0.0) / self.symbol(name)[1]
        if status == "open":
            return OrderResult(True, self.id, order_id=oid, status="accepted", filled_qty=filled, avg_price=px or None, raw=ans, request=req)
        if status == "finished" and (how == "filled" or (how == "ioc" and filled > 0)):
            return OrderResult(True, self.id, order_id=oid, status="filled", filled_qty=filled, avg_price=px or None, raw=ans, request=req)
        return OrderResult(False, self.id, order_id=oid, status="rejected", error=f"{status}: {how}", raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("DELETE", f"{ORDERS}/{order_id}")
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        ok = not ans.get("label") and ans.get("status") == "finished" and ans.get("finish_as") == "cancelled"
        err = "" if ok else f"{ans.get('label') or ans.get('status')}: {ans.get('message') or ans.get('finish_as') or ''}"[:160]
        return OrderResult(ok, self.id, order_id=order_id, status="cancelled" if ok else "rejected", error=err, raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        query = "status=open" + (f"&contract={self.contract(symbol)}" if symbol else "")
        _, ans = await self._call("GET", ORDERS, query)
        out = []
        for o in ans.get("data") if isinstance(ans.get("data"), list) else []:
            name = str(o.get("contract") or "")
            sym, factor = self.symbol(name)
            mult = (self.contracts.get(name) or {"mult": 1.0})["mult"]
            size, left = float(o.get("size") or 0.0), float(o.get("left") or 0.0)
            out.append({"order_id": str(o.get("id") or ""), "symbol": sym, "side": "buy" if size > 0 else "sell", "qty": abs(size) * mult,
                        "price": float(o.get("price") or 0.0) / factor, "filled": (abs(size) - abs(left)) * mult, "status": o.get("status"),
                        "client_id": str(o.get("text") or "")})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._call("GET", TRADES, urllib.parse.urlencode({"from": int(since), "limit": 100}))
        out = []
        for t in ans.get("data") if isinstance(ans.get("data"), list) else []:
            name = str(t.get("contract") or "")
            sym, factor = self.symbol(name)
            mult, size = (self.contracts.get(name) or {"mult": 1.0})["mult"], float(t.get("size") or 0.0)
            out.append({"order_id": str(t.get("order_id") or ""), "symbol": sym, "side": "buy" if size > 0 else "sell", "qty": abs(size) * mult,
                        "price": float(t.get("price") or 0.0) / factor, "fee": float(t.get("fee") or 0.0), "t": float(t.get("create_time") or 0.0)})
        return out
