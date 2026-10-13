"""Bitget: the spot wallet, the USDT-futures account and its positions. Three GETs, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from ..sources import unscale
from .base import Account, fnum

HOST = "https://api.bitget.com"
ASSETS = "/api/v2/spot/account/assets"
MIX_ACCOUNTS = "/api/v2/mix/account/accounts"
POSITIONS = "/api/v2/mix/position/all-position"
PRODUCT = "productType=USDT-FUTURES"


class BitgetAccount(Account):
    """Bitget API key, secret and passphrase: ACCESS-SIGN is base64(HMAC-SHA256(timestamp +
    METHOD + path + "?query" + body, secret)) with a millisecond ACCESS-TIMESTAMP. Reads the
    spot wallet, then the USDT-margined futures account (its equity as USDT collateral) and its
    open positions; a key without the futures permission still shows its spot wallet."""

    id = "bitget"
    label = "Bitget"
    URL = "https://www.bitget.com/account/newapi"
    note = 'Optional. A read-only ("Read" only) API key with its passphrase shows your Bitget spot and futures balances on the Portfolio tab.'
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (ASSETS, MIX_ACCOUNTS, POSITIONS)
    HOST = HOST

    def sign(self, ts, method: str, path: str, query: str = "", body: str = "") -> str:
        msg = f"{ts}{method.upper()}{path}{'?' + query if query else ''}{body}"
        return base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    def headers(self, method: str, path: str, query: str = "", ts: int | None = None) -> dict:
        ts = int(time.time() * 1000) if ts is None else int(ts)
        return {"ACCESS-KEY": self.key, "ACCESS-SIGN": self.sign(ts, method, path, query), "ACCESS-TIMESTAMP": str(ts),
                "ACCESS-PASSPHRASE": self.passphrase, "locale": "en-US", "Content-Type": "application/json"}

    async def _get(self, path: str, query: str = ""):
        url = f"{HOST}{path}" + (f"?{query}" if query else "")
        d = await self._json("GET", url, headers=self.headers("GET", path, query))
        if str(d.get("code")) != "00000":
            raise RuntimeError(f"{d.get('code')}: {str(d.get('msg'))[:80]}")
        return d.get("data") or []

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for a in await self._get(ASSETS):                   # [{coin, available, frozen, locked, ...}]
            avail, frozen, locked = fnum(a.get("available")), fnum(a.get("frozen")), fnum(a.get("locked"))
            if avail + frozen + locked > 0:
                out.append({"asset": self.asset(a.get("coin")), "qty": avail + frozen + locked, "hold": frozen + locked, "id": "bitget"})
        try:
            for a in await self._get(MIX_ACCOUNTS, PRODUCT):  # [{marginCoin, available, locked, accountEquity, unrealizedPL, ...}]
                equity = fnum(a.get("accountEquity")) or fnum(a.get("available")) + fnum(a.get("locked"))
                if equity > 0:
                    out.append({"asset": self.asset(a.get("marginCoin")), "qty": equity, "hold": fnum(a.get("locked")), "id": "bitget-futures"})
            for p in await self._get(POSITIONS, PRODUCT):     # [{symbol, holdSide, total, openPriceAvg, markPrice, unrealizedPL, leverage, ...}]
                base, factor = unscale(str(p.get("symbol") or "").removesuffix("USDT"))
                qty = fnum(p.get("total")) * factor
                if qty <= 0:
                    continue
                out.append({"asset": self.asset(base), "kind": "perp", "qty": -qty if p.get("holdSide") == "short" else qty,
                            "price": fnum(p.get("markPrice")) / factor, "entry": fnum(p.get("openPriceAvg")) / factor,
                            "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unrealizedPL"))})
        except RuntimeError:
            pass                                            # no futures permission: the spot wallet still shows
        return out
