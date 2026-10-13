"""Kraken spot: `AddOrder`, `CancelOrder`, `OpenOrders` and `TradesHistory` on the private REST
API, behind the execution gate. Signed as the read-only reader signs `Balance` (HMAC-SHA512 over
path + sha256(nonce + form body), base64), with a key of its own, `kraken-trade.json`, that
carries "Create & modify orders". There is no Kraken testnet; the venue's own `validate: true`
is the only rehearsal and needs a real trade key, so it is not used here."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal

from ..sources.base import base_of
from .base import Executor, Order, OrderResult

KRAKEN = "https://api.kraken.com"
ADD = "/0/private/AddOrder"
CANCEL = "/0/private/CancelOrder"
OPEN = "/0/private/OpenOrders"
TRADES = "/0/private/TradesHistory"
ALT = {"BTC": "XBT", "DOGE": "XDG"}                 # the two altnames that are not the base itself
_UUID = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def _num(x: float, places: int = 8) -> str:
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class KrakenExecutor(Executor):
    """The pair is the REST altname the market-data source maps from `AssetPairs`: the base plus
    USD, with bitcoin and doge under their Kraken names (XBTUSD, XDGUSD). Form-encoded bodies,
    a millisecond nonce, a string volume rounded down to 8 decimals (`lot_decimals` is 8 on the
    USD pairs; `ordermin` is per pair and the venue rejects a dust order). `cl_ord_id` takes a
    UUID or free text up to 18 characters: a longer ticket id is dropped rather than truncated
    (the venue's txid is the handle). Spot has no reduce-only (the flag is for margin), so the
    ticket's flag is ignored. Answers carry `error` (a list, empty on success) and `result`."""

    id = "kraken"
    label = "Kraken"
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Private key"}]
    endpoints = (ADD, CANCEL, OPEN, TRADES)
    HOST = KRAKEN

    @staticmethod
    def pair(sym: str) -> str:
        base = base_of(sym)
        return ALT.get(base, base) + "USD"

    @staticmethod
    def symbol_of(pair: str) -> str:
        """An altname (XBTUSD) or a pair key (XXBTZUSD) back to the canonical symbol."""
        p = str(pair or "").upper().replace("/", "")
        base = p[:-4] if p.endswith("ZUSD") and len(p) > 6 else p[:-3] if p.endswith("USD") else p
        if len(base) == 4 and base[0] in ("X", "Z") and base not in ("XRP", "XLM", "XTZ", "XCN"):
            base = base[1:]
        return {"XBT": "BTC", "XDG": "DOGE"}.get(base, base) + "-USD"

    @staticmethod
    def nonce() -> str:
        return str(int(time.time() * 1000))

    def sign(self, path: str, data: dict) -> str:
        post = urllib.parse.urlencode(data)
        sha = hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), path.encode() + sha, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def headers(self, path: str, data: dict) -> dict:
        return {"API-Key": self.key, "API-Sign": self.sign(path, data)}

    def body(self, order: Order, nonce: str | None = None) -> dict:
        d = {"nonce": nonce or self.nonce(), "ordertype": "market" if order.price is None else "limit",
             "type": order.side, "volume": _num(order.qty), "pair": self.pair(order.symbol)}
        if order.price is not None:
            d["price"] = _num(order.price)
        cid = str(order.client_id or "")
        if cid and (_UUID.match(cid) or (len(cid) <= 18 and cid.isascii())):
            d["cl_ord_id"] = cid
        return d

    async def _post(self, path: str, data: dict) -> tuple[dict, dict]:
        return await self._send("POST", f"{self.host}{path}", data=data, headers=self.headers(path, data))

    async def _place(self, order: Order) -> OrderResult:
        req, ans = await self._post(ADD, self.body(order))
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        errs = ans.get("error") or []
        if errs or ans.get("_status", 200) >= 400:
            why = "; ".join(str(e) for e in errs) or f"HTTP {ans.get('_status')}"
            return OrderResult(False, self.id, status="rejected", error=why[:200], raw=ans, request=req)
        txids = (ans.get("result") or {}).get("txid") or []
        return OrderResult(True, self.id, str(txids[0]) if txids else "", "accepted", raw=ans, request=req)

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._post(CANCEL, {"nonce": self.nonce(), "txid": order_id})
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id, "dry-run", request=req)
        errs = ans.get("error") or []
        if errs or not (ans.get("result") or {}).get("count"):
            return OrderResult(False, self.id, order_id, "rejected", error=("; ".join(str(e) for e in errs) or "nothing cancelled")[:200], raw=ans, request=req)
        return OrderResult(True, self.id, order_id, "accepted", raw=ans, request=req)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._post(OPEN, {"nonce": self.nonce()})
        out = []
        for txid, o in ((ans.get("result") or {}).get("open") or {}).items():
            d = o.get("descr") or {}
            sym = self.symbol_of(d.get("pair"))
            if symbol and sym != symbol:
                continue
            price = d.get("price")
            out.append({"order_id": str(txid), "symbol": sym, "side": str(d.get("type") or ""), "qty": float(o.get("vol") or 0.0),
                        "price": float(price) if price not in (None, "", "0") else None, "filled": float(o.get("vol_exec") or 0.0), "status": str(o.get("status") or "")})
        return out

    async def fills(self, since: float) -> list[dict]:
        _, ans = await self._post(TRADES, {"nonce": self.nonce(), "start": int(since)})
        out = []
        for _, t in ((ans.get("result") or {}).get("trades") or {}).items():
            out.append({"order_id": str(t.get("ordertxid") or ""), "symbol": self.symbol_of(t.get("pair")), "side": str(t.get("type") or ""),
                        "qty": float(t.get("vol") or 0.0), "price": float(t.get("price") or 0.0), "fee": float(t.get("fee") or 0.0), "t": float(t.get("time") or 0.0)})
        return out
