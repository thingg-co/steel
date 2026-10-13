"""OKX: `GET /api/v5/account/balance` (the trading account), `GET /api/v5/asset/balances` (the
funding account), `GET /api/v5/account/positions` and the account's fee level from
`GET /api/v5/account/trade-fee`, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://www.okx.com"
BALANCE = "/api/v5/account/balance"
FUNDING = "/api/v5/asset/balances"
POSITIONS = "/api/v5/account/positions"
TRADE_FEE = "/api/v5/account/trade-fee"               # ?instType=SPOT|SWAP: [{level, taker, maker, takerU, makerU, feeGroup: [...]}]


class OKXAccount(Account):
    """OKX API key + secret + passphrase: OK-ACCESS-SIGN = base64(HMAC-SHA256(secret, timestamp
    + METHOD + requestPath-with-query + body)) with the ISO-8601 millisecond timestamp
    (2020-12-08T09:08:57.715Z) in OK-ACCESS-TIMESTAMP and the passphrase in OK-ACCESS-PASSPHRASE.
    Positions come in contracts (`pos`); the base size is notionalUsd / markPx with the sign of
    posSide (of pos itself in net mode), so no instrument table is needed."""

    id = "okx"
    label = "OKX"
    URL = "https://www.okx.com/account/my-api"
    note = ('Optional. A read-only ("Read" permission only, no trading or withdrawals) OKX API key, secret and passphrase show your '
            'trading and funding balances and open swap positions on the Portfolio tab.')
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (BALANCE, FUNDING, POSITIONS, TRADE_FEE)
    HOST = HOST

    @staticmethod
    def stamp(now: float | None = None) -> str:
        """OKX's timestamp: ISO 8601 UTC with milliseconds, 2020-12-08T09:08:57.715Z."""
        now = time.time() if now is None else float(now)
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"

    def sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        mac = hmac.new(self.secret.encode(), f"{ts}{method.upper()}{path}{body}".encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def headers(self, method: str, path: str, body: str = "", ts: str | None = None) -> dict:
        ts = self.stamp() if ts is None else ts
        return {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": self.sign(ts, method, path, body), "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}

    async def _get(self, path: str) -> list:
        d = await self._json("GET", f"{HOST}{path}", headers=self.headers("GET", path))
        if str(d.get("code")) != "0":
            raise RuntimeError(f"{d.get('msg')} ({d.get('code')})"[:120])
        return d.get("data") or []

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for acct in await self._get(BALANCE):
            for det in acct.get("details") or []:
                q = fnum(det.get("cashBal"))
                if q > 0:
                    out.append({"asset": self.asset(det.get("ccy")), "qty": q, "hold": fnum(det.get("frozenBal")), "id": "trading"})
        for b in await self._get(FUNDING):
            q = fnum(b.get("bal"))
            if q > 0:
                out.append({"asset": self.asset(b.get("ccy")), "qty": q, "hold": fnum(b.get("frozenBal")), "id": "funding"})
        for p in await self._get(POSITIONS):
            mark, notional, pos = fnum(p.get("markPx")), fnum(p.get("notionalUsd")), fnum(p.get("pos"))
            if not (mark > 0 and notional > 0 and pos):
                continue
            side = p.get("posSide")
            sign = -1.0 if side == "short" or (side == "net" and pos < 0) else 1.0
            out.append({"asset": str(p.get("instId") or "").split("-")[0].upper(), "kind": "perp", "qty": sign * notional / mark, "price": mark,
                        "entry": fnum(p.get("avgPx")), "leverage": fnum(p.get("lever")), "upnl": fnum(p.get("upl"))})
        return out

    @staticmethod
    def _okx_bps(value) -> float | None:
        # OKX signs a fee rate the other way round: a negative decimal is a commission the
        # platform charges ("-0.001" is a 10 bps fee), a positive one a rebate. Flipped here so a
        # fee is positive like every other venue's.
        v = fee_bps(value)
        return None if v is None else -v

    async def fees(self) -> dict | None:
        """The account's fee level for SPOT (`taker` / `maker`: the USDT pairs' rates) and SWAP
        (`takerU` / `makerU`: the USDT-margined contracts' rates, labelled deprecated in the docs
        but still served; the first `feeGroup` entry, then `taker` / `maker`, stand in when they
        are empty)."""
        if not self.configured:
            return None
        spot = (await self._get(f"{TRADE_FEE}?instType=SPOT") or [{}])[0]
        swap = (await self._get(f"{TRADE_FEE}?instType=SWAP") or [{}])[0]
        group = ((swap.get("feeGroup") or [{}])[0]) or {}
        perp_t = self._okx_bps(swap.get("takerU") or group.get("taker") or swap.get("taker"))
        perp_m = self._okx_bps(swap.get("makerU") or group.get("maker") or swap.get("maker"))
        return fee_rates(spot_taker=self._okx_bps(spot.get("taker")), spot_maker=self._okx_bps(spot.get("maker")), perp_taker=perp_t, perp_maker=perp_m)
