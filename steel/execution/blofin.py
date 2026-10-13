"""BloFin USDT-perpetual orders: `POST /api/v1/trade/order` and `cancel-order`, `GET
orders-pending` and `fills-history`, sized in contracts from `GET /api/v1/market/instruments`.
A key with the Trade permission; the demo host has its own keys."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import uuid
from decimal import ROUND_DOWN, Decimal

from ..sources import unscale
from ..sources.blofin import BloFinSource
from .base import Executor, Order, OrderResult

HOST = "https://openapi.blofin.com"
ORDER = "/api/v1/trade/order"
CANCEL = "/api/v1/trade/cancel-order"
PENDING = "/api/v1/trade/orders-pending"
FILLS = "/api/v1/trade/fills-history"
INSTRUMENTS = "/api/v1/market/instruments"


def _dec(x: float, places: int) -> str:
    s = format(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class BloFinExecutor(Executor):
    """BloFin API key, secret and passphrase, signed as the account reader does: the hex
    HMAC-SHA256 of `path + METHOD + timestamp + nonce + body`, base64 of the hex STRING, headers
    ACCESS-KEY / SIGN / TIMESTAMP / NONCE / PASSPHRASE. The body is signed as the exact JSON
    string sent (compact separators, sent as raw content). One-way mode: `marginMode` isolated,
    `positionSide` net; `reduceOnly` is honoured in one-way mode only (hedge mode ignores it).
    `size` is in CONTRACTS of `contractValue` base units (0.001 BTC for BTC-USDT; a
    1000PEPE-USDT contract is `contractValue` x 1000 tokens), cut down to `lotSize` and refused
    under `minSize`, so the instrument list must be in `insts` before an order can be sized:
    `instruments()` fetches it once through the gate (in dry-run nothing is fetched, so seed
    `insts` as the market-data source's `list_instruments` would). A limit price for a
    thousand-token contract is per thousand tokens. `clientOrderId` is up to 32 alphanumerics.
    The order answer carries no fill: a placed order is `accepted`, fills come from
    `fills-history` (`begin` in ms)."""

    id = "blofin"
    label = "BloFin"
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (ORDER, CANCEL, PENDING, FILLS, INSTRUMENTS)
    HOST = HOST
    TESTNET = "https://demo-trading-openapi.blofin.com"
    MARGIN_MODE = "isolated"
    POSITION_SIDE = "net"

    inst = staticmethod(BloFinSource.inst)      # BTC-USD -> BTC-USDT (the plain spelling; 1000x names come from the list)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.insts: dict[str, dict] = {}        # canonical -> {instId, factor, contract (base per contract / factor), lot, min}

    def sign(self, method: str, path: str, ts: str, nonce: str, body: str = "") -> str:
        hexed = hmac.new(self.secret.encode(), f"{path}{method.upper()}{ts}{nonce}{body}".encode(), hashlib.sha256).hexdigest()
        return base64.b64encode(hexed.encode()).decode()

    def headers(self, method: str, path: str, ts=None, nonce: str | None = None, body: str = "") -> dict:
        ts = str(ts if ts is not None else int(time.time() * 1000))
        nonce = nonce or str(uuid.uuid4())
        return {"ACCESS-KEY": self.key, "ACCESS-SIGN": self.sign(method, path, ts, nonce, body), "ACCESS-TIMESTAMP": ts,
                "ACCESS-NONCE": nonce, "ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> tuple[dict, dict]:
        query = urllib.parse.urlencode(params or {})
        uri = path + (f"?{query}" if query else "")
        content = json.dumps(body, separators=(",", ":")) if body is not None else ""
        return await self._send(method, f"{self.host}{uri}", content=content or None, headers=self.headers(method, uri, body=content))

    @staticmethod
    def symbol(inst_id: str) -> str:
        return unscale(str(inst_id or "").split("-")[0])[0].upper() + "-USD"

    async def instruments(self) -> dict:
        if not self.insts:
            _, ans = await self._call("GET", INSTRUMENTS, params={"instType": "SWAP"})
            for p in ans.get("data") or []:
                if p.get("quoteCurrency") != "USDT" or p.get("state") != "live":
                    continue
                base, factor = unscale(str(p.get("baseCurrency") or ""))
                self.insts.setdefault(f"{base}-USD", {"instId": p["instId"], "factor": factor, "contract": float(p.get("contractValue") or 1.0),
                                                       "lot": float(p.get("lotSize") or 0.0), "min": float(p.get("minSize") or 0.0)})
        return self.insts

    def contracts(self, symbol: str, qty: float) -> tuple[dict, str]:
        """(the instrument, the contract count as a string) for `qty` base units, cut down to
        the lot; LookupError when the list has not been seen, ValueError under the minimum."""
        i = self.insts.get(symbol)
        if not i:
            raise LookupError(f"{symbol}: contract value unknown ({self.inst(symbol)} is not in the instrument list)")
        n = Decimal(str(qty)) / (Decimal(str(i["contract"])) * Decimal(str(i["factor"])))
        if i["lot"] > 0:
            lot = Decimal(str(i["lot"]))
            n = (n / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        if n <= 0 or n < Decimal(str(i["min"])):
            raise ValueError(f"{symbol}: {qty} is under the minimum order of {i['min']:g} contracts of {i['contract'] * i['factor']:g}")
        return i, format(n.normalize(), "f")

    def _base(self, inst_id: str, contracts) -> float:
        i = next((v for v in self.insts.values() if v["instId"] == inst_id), None)
        return float(contracts or 0.0) * (i["contract"] * i["factor"] if i else 1.0)

    async def _place(self, order: Order) -> OrderResult:
        await self.instruments()
        try:
            inst, size = self.contracts(order.symbol, order.qty)
        except (LookupError, ValueError) as e:
            return OrderResult(False, self.id, status="refused", error=str(e))
        body = {"instId": inst["instId"], "marginMode": self.MARGIN_MODE, "positionSide": self.POSITION_SIDE, "side": order.side,
                "orderType": "market" if order.price is None else "limit"}
        if order.price is not None:
            body["price"] = _dec(order.price * inst["factor"], 10)
        body["size"] = size
        body["reduceOnly"] = "true" if order.reduce_only else "false"
        if order.client_id:
            body["clientOrderId"] = re.sub(r"[^A-Za-z0-9]", "", order.client_id)[:32]
        req, ans = await self._call("POST", ORDER, body)
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        row = (ans.get("data") or [{}])[0] if isinstance(ans.get("data"), list) else {}
        code = str(row.get("code") if row.get("code") not in (None, "") else ans.get("code"))
        if code != "0":
            err = f"{code}: {row.get('msg') or ans.get('msg') or ''}"[:160]
            return OrderResult(False, self.id, order_id=str(row.get("orderId") or ""), status="rejected", error=err, raw=ans, request=req)
        return OrderResult(True, self.id, order_id=str(row.get("orderId") or ""), status="accepted", raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        inst_id = (self.insts.get(symbol) or {}).get("instId") or self.inst(symbol)
        req, ans = await self._call("POST", CANCEL, {"instId": inst_id, "orderId": order_id})
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        row = (ans.get("data") or [{}])[0] if isinstance(ans.get("data"), list) else {}
        code = str(row.get("code") if row.get("code") not in (None, "") else ans.get("code"))
        ok = code == "0"
        return OrderResult(ok, self.id, order_id=order_id, status="cancelled" if ok else "rejected",
                           error="" if ok else f"{code}: {row.get('msg') or ans.get('msg') or ''}"[:160], raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"limit": 100}
        if symbol:
            params = {"instId": (self.insts.get(symbol) or {}).get("instId") or self.inst(symbol), **params}
        _, ans = await self._call("GET", PENDING, params=params)
        out = []
        for o in ans.get("data") or []:
            inst_id = str(o.get("instId") or "")
            factor = unscale(inst_id.split("-")[0])[1]
            out.append({"order_id": str(o.get("orderId") or ""), "symbol": self.symbol(inst_id), "side": o.get("side"),
                        "qty": self._base(inst_id, o.get("size")), "price": float(o.get("price") or 0.0) / factor,
                        "filled": self._base(inst_id, o.get("filledSize")), "status": o.get("state"), "client_id": o.get("clientOrderId") or ""})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._call("GET", FILLS, params={"begin": int(since * 1000), "limit": 100})
        out = []
        for f in ans.get("data") or []:
            inst_id = str(f.get("instId") or "")
            factor = unscale(inst_id.split("-")[0])[1]
            out.append({"order_id": str(f.get("orderId") or ""), "symbol": self.symbol(inst_id), "side": f.get("side"),
                        "qty": self._base(inst_id, f.get("fillSize")), "price": float(f.get("fillPrice") or 0.0) / factor,
                        "fee": float(f.get("fee") or 0.0), "t": int(f.get("ts") or 0) / 1000.0})
        return out
