"""Kraken spot: `POST /0/private/Balance` and the fee tier from `POST /0/private/TradeVolume`
(both under the "Query Funds" permission), nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

from .base import Account, fee_bps, fee_rates, fnum

KRAKEN = "https://api.kraken.com"
BALANCE = "/0/private/Balance"
TRADE_VOLUME = "/0/private/TradeVolume"               # a read: the 30-day volume and the fee tier it puts the account in
FEE_PAIR = "XBTUSD"


class KrakenAccount(Account):
    """Kraken API key + secret (HMAC-SHA512 over the nonce and the POST body)."""

    id = "kraken"
    label = "Kraken"
    URL = "https://www.kraken.com/u/security/api"
    note = "Optional. A read-only (Query Funds only) API key shows your Kraken balances on the Portfolio tab."
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Private key"}]
    endpoints = (BALANCE, TRADE_VOLUME)
    HOST = KRAKEN

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    def sign(self, path: str, data: dict) -> str:
        post = urllib.parse.urlencode(data)
        sha = hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), path.encode() + sha, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    @staticmethod
    def asset(code: str) -> str:
        """Kraken's asset codes to bases: XXBT -> BTC, XETH -> ETH, ZUSD -> USD, XDG -> DOGE."""
        c = str(code or "").upper()
        c = c.split(".")[0]                                  # staked variants like ETH2.S, DOT.S
        if len(c) == 4 and c[0] in ("X", "Z") and c not in ("XRP", "XLM", "XTZ", "XCN"):
            c = c[1:]
        return {"XBT": "BTC", "XDG": "DOGE"}.get(c, c)

    async def balances(self) -> list[dict]:
        data = {"nonce": str(int(time.time() * 1000))}
        d = await self._json("POST", f"{KRAKEN}{BALANCE}", data=data, headers={"API-Key": self.key, "API-Sign": self.sign(BALANCE, data)})
        if d.get("error"):
            raise RuntimeError("; ".join(str(e) for e in d["error"])[:120])
        out = []
        for code, qty in (d.get("result") or {}).items():
            q = fnum(qty)
            if q > 0:
                out.append({"asset": self.asset(code), "qty": q, "hold": 0.0, "id": "kraken"})
        return out

    async def _post(self, path: str, data: dict) -> dict:
        data = {"nonce": str(int(time.time() * 1000))} | data
        d = await self._json("POST", f"{KRAKEN}{path}", data=data, headers={"API-Key": self.key, "API-Sign": self.sign(path, data)})
        if d.get("error"):
            raise RuntimeError("; ".join(str(e) for e in d["error"])[:120])
        return d.get("result") or {}

    async def fees(self) -> dict | None:
        """TradeVolume for XBTUSD: `fees` (taker) and `fees_maker` per pair, each `fee` a percent
        ("0.2600" is 26 bps). The pair comes back under Kraken's own name (XXBTZUSD), so the
        first entry is taken. Spot only."""
        if not self.configured:
            return None
        res = await self._post(TRADE_VOLUME, {"pair": FEE_PAIR, "fee-info": "true"})
        taker = next(iter((res.get("fees") or {}).values()), None) or {}
        maker = next(iter((res.get("fees_maker") or {}).values()), None) or {}
        return fee_rates(spot_taker=fee_bps(taker.get("fee"), 100.0), spot_maker=fee_bps(maker.get("fee"), 100.0))
