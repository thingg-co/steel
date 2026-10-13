"""Aster: `GET /fapi/v2/balance`, `GET /fapi/v2/positionRisk` and the account's commission from
`GET /fapi/v1/commissionRate` with the V1 (Pro) HMAC API key, nothing else. Aster's V3 API signs
with an EVM wallet instead and is not used here."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

from ..sources import unscale
from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://fapi.asterdex.com"
BALANCE = "/fapi/v2/balance"
POSITIONS = "/fapi/v2/positionRisk"
COMMISSION = "/fapi/v1/commissionRate"                 # ?symbol=BTCUSDT -> {symbol, makerCommissionRate, takerCommissionRate}
FEE_SYMBOL = "BTCUSDT"
QUOTES = ("USDT", "USDC")


class AsterAccount(Account):
    """Aster Pro API key + secret, the Binance shape: HMAC-SHA256 hex over the query string
    (`timestamp` and `recvWindow` included, `signature` last) with the key in X-MBX-APIKEY.
    Aster's recommended V3 API authenticates with an EVM wallet signature (user, signer, nonce)
    instead; only the API-key version is implemented. Collateral is the futures wallet's
    assets; positions are signed `positionAmt` in the coin (1000PEPEUSDT scales through
    `unscale`)."""

    id = "aster"
    label = "Aster"
    URL = "https://www.asterdex.com/en/api-wallet"
    note = 'Optional. A read-only ("Read" only, no trading) Aster Pro API key shows your futures-wallet collateral and open perpetual positions on the Portfolio tab.'
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (BALANCE, POSITIONS, COMMISSION)
    HOST = HOST
    RECV_WINDOW = 5000

    def sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def query(self, params: dict | None = None, ts=None) -> str:
        """The signed query string: the parameters, then timestamp and recvWindow, then the signature."""
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) if ts is None else int(ts)
        p["recvWindow"] = self.RECV_WINDOW
        q = urllib.parse.urlencode(p)
        return f"{q}&signature={self.sign(q)}"

    @staticmethod
    def contract(symbol: str) -> tuple[str, float]:
        """BTCUSDT -> ("BTC", 1.0); 1000PEPEUSDT -> ("PEPE", 1000.0)."""
        s = str(symbol or "").upper()
        for q in QUOTES:
            if s.endswith(q):
                s = s[:-len(q)]
                break
        return unscale(s)

    async def _get(self, path: str, params: dict | None = None):
        return await self._json("GET", f"{HOST}{path}?{self.query(params)}", headers={"X-MBX-APIKEY": self.key})

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for b in (await self._get(BALANCE)) or []:
            q = fnum(b.get("balance"))
            if q > 0:
                out.append({"asset": self.asset(b.get("asset")), "qty": q, "hold": max(0.0, q - fnum(b.get("availableBalance", q))), "id": "futures"})
        for p in (await self._get(POSITIONS)) or []:
            amt = fnum(p.get("positionAmt"))
            if not amt:
                continue
            base, factor = self.contract(p.get("symbol"))
            out.append({"asset": base, "kind": "perp", "qty": amt * factor, "price": fnum(p.get("markPrice")) / factor,
                        "entry": fnum(p.get("entryPrice")) / factor, "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unRealizedProfit"))})
        return out

    async def fees(self) -> dict | None:
        """The account's commission on BTCUSDT, the Binance-futures shape: `takerCommissionRate` /
        `makerCommissionRate` are decimal strings ("0.0004" is 4 bps). Perps only."""
        if not self.configured:
            return None
        d = await self._get(COMMISSION, {"symbol": FEE_SYMBOL}) or {}
        return fee_rates(perp_taker=fee_bps(d.get("takerCommissionRate")), perp_maker=fee_bps(d.get("makerCommissionRate")))
