"""MEXC spot: `GET /api/v3/account` and the symbol's commission from `GET /api/v3/tradeFee` (the
"Account: read" permission), nothing else."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.mexc.com"
ACCOUNT = "/api/v3/account"
TRADE_FEE = "/api/v3/tradeFee"                        # ?symbol=BTCUSDT -> {data: {makerCommission, takerCommission}}
FEE_SYMBOL = "BTCUSDT"


class MEXCAccount(Account):
    """MEXC API key and secret, Binance-shaped: the hex HMAC-SHA256 of the query string
    (`timestamp` in milliseconds and `recvWindow`) goes on the query as `signature`, the key in
    the X-MEXC-APIKEY header. Spot balances only; mexc.com does not serve US residents."""

    id = "mexc"
    label = "MEXC"
    URL = "https://www.mexc.com/user/openapi"
    note = 'Optional. A read-only ("Account: read" permission only) API key shows your MEXC spot balances on the Portfolio tab.'
    venue = "spot"
    fields = [{"id": "key", "label": "Access key"}, {"id": "secret", "label": "Secret key"}]
    endpoints = (ACCOUNT, TRADE_FEE)
    HOST = HOST

    def sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def query(self, ts: int | None = None, recv_window: int = 5000, params: dict | None = None) -> str:
        """The signed query: any request parameters first, then timestamp and recvWindow, the signature last."""
        ts = int(time.time() * 1000) if ts is None else int(ts)
        q = (urllib.parse.urlencode(params) + "&" if params else "") + f"timestamp={ts}&recvWindow={recv_window}"
        return f"{q}&signature={self.sign(q)}"

    async def balances(self) -> list[dict]:
        d = await self._json("GET", f"{HOST}{ACCOUNT}?{self.query()}", headers={"X-MEXC-APIKEY": self.key})
        if d.get("code") not in (None, 0, 200):
            raise RuntimeError(f"{d.get('code')}: {str(d.get('msg'))[:80]}")
        out: list[dict] = []
        for b in d.get("balances") or []:                   # [{asset, free, locked}]
            free, locked = fnum(b.get("free")), fnum(b.get("locked"))
            if free + locked > 0:
                out.append({"asset": self.asset(b.get("asset")), "qty": free + locked, "hold": locked, "id": "mexc"})
        return out

    async def fees(self) -> dict | None:
        """The account's commission on BTCUSDT: `data.makerCommission` / `takerCommission` are
        decimals (0.003 is 30 bps). Spot only."""
        if not self.configured:
            return None
        d = await self._json("GET", f"{HOST}{TRADE_FEE}?{self.query(params={'symbol': FEE_SYMBOL})}", headers={"X-MEXC-APIKEY": self.key})
        if d.get("code") not in (None, 0, 200):
            raise RuntimeError(f"{d.get('code')}: {str(d.get('msg'))[:80]}")
        data = d.get("data") or {}
        return fee_rates(spot_taker=fee_bps(data.get("takerCommission")), spot_maker=fee_bps(data.get("makerCommission")))
