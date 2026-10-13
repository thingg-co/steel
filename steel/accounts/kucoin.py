"""KuCoin: spot accounts, the futures account overview, and the account's fee rates
(`GET /api/v1/base-fee` on spot, `GET /api/v1/trade-fees` on futures). Four GETs, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .base import Account, fee_bps, fee_rates, fnum, mask

HOST = "https://api.kucoin.com"
FUTURES = "https://api-futures.kucoin.com"
ACCOUNTS = "/api/v1/accounts"
OVERVIEW = "/api/v1/account-overview"
BASE_FEE = "/api/v1/base-fee"                         # spot: {data: {takerFeeRate, makerFeeRate}}
TRADE_FEES = "/api/v1/trade-fees"                     # futures host: ?symbol=XBTUSDTM -> {data: {symbol, takerFeeRate, makerFeeRate}}
FEE_CONTRACT = "XBTUSDTM"


class KuCoinAccount(Account):
    """KuCoin API key, secret and passphrase, key version 2: KC-API-SIGN is base64(HMAC-SHA256(
    timestamp + METHOD + endpoint-with-query + body, secret)), KC-API-PASSPHRASE the passphrase
    HMAC-signed the same way, KC-API-TIMESTAMP in milliseconds. One key serves both hosts. The
    spot accounts (main, trade, margin... summed per currency), then the USDT futures account's
    equity as USDT collateral; a key without futures still shows spot."""

    id = "kucoin"
    label = "KuCoin"
    URL = "https://www.kucoin.com/account/api"
    note = 'Optional. A read-only ("General" permission only) API key with its passphrase shows your KuCoin balances on the Portfolio tab.'
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (ACCOUNTS, OVERVIEW, BASE_FEE, TRADE_FEES)
    HOST = HOST

    def sign(self, ts, method: str, endpoint: str, body: str = "") -> str:
        msg = f"{ts}{method.upper()}{endpoint}{body}"
        return base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    def headers(self, method: str, endpoint: str, ts: int | None = None) -> dict:
        ts = int(time.time() * 1000) if ts is None else int(ts)
        passphrase = base64.b64encode(hmac.new(self.secret.encode(), self.passphrase.encode(), hashlib.sha256).digest()).decode()
        return {"KC-API-KEY": self.key, "KC-API-SIGN": self.sign(ts, method, endpoint), "KC-API-TIMESTAMP": str(ts),
                "KC-API-PASSPHRASE": passphrase, "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"}

    @staticmethod
    def asset(code: str) -> str:
        """KuCoin's codes to bases: the futures side still says XBT for bitcoin."""
        c = str(code or "").upper()
        return {"XBT": "BTC"}.get(c, c)

    async def _get(self, host: str, endpoint: str):
        d = await self._json("GET", host + endpoint, headers=self.headers("GET", endpoint))
        if str(d.get("code")) != "200000":
            raise RuntimeError(f"{d.get('code')}: {str(d.get('msg'))[:80]}")
        return d.get("data")

    async def balances(self) -> list[dict]:
        totals: dict[str, list] = {}
        for a in await self._get(HOST, ACCOUNTS) or []:       # one row per currency and account type: {id, currency, type, balance, available, holds}
            qty = fnum(a.get("balance"))
            if qty > 0:
                t = totals.setdefault(self.asset(a.get("currency")), [0.0, 0.0, mask(a.get("id"))])
                t[0] += qty
                t[1] += fnum(a.get("holds"))
        out = [{"asset": code, "qty": q, "hold": h, "id": i} for code, (q, h, i) in totals.items()]
        try:
            d = await self._get(FUTURES, f"{OVERVIEW}?currency=USDT") or {}   # {accountEquity, unrealisedPNL, availableBalance, currency, ...}
            equity = fnum(d.get("accountEquity"))
            if equity > 0:
                out.append({"asset": self.asset(d.get("currency") or "USDT"), "qty": equity,
                            "hold": max(0.0, equity - fnum(d.get("availableBalance"))), "id": "kucoin-futures"})
        except RuntimeError:
            pass                                            # no futures permission: spot still shows
        return out

    async def fees(self) -> dict | None:
        """The account's base spot fee (`takerFeeRate` / `makerFeeRate`, decimal strings: "0.001"
        is 10 bps) and its rate on the XBTUSDTM perp from the futures host (the same decimals);
        a key without futures keeps the spot rates."""
        if not self.configured:
            return None
        d = await self._get(HOST, BASE_FEE) or {}
        perp_t = perp_m = None
        try:
            f = await self._get(FUTURES, f"{TRADE_FEES}?symbol={FEE_CONTRACT}") or {}
            perp_t, perp_m = fee_bps(f.get("takerFeeRate")), fee_bps(f.get("makerFeeRate"))
        except RuntimeError:
            pass                                            # no futures permission: spot still shows
        return fee_rates(spot_taker=fee_bps(d.get("takerFeeRate")), spot_maker=fee_bps(d.get("makerFeeRate")), perp_taker=perp_t, perp_maker=perp_m)
