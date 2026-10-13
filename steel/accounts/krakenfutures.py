"""Kraken Futures: `GET /derivatives/api/v3/accounts` (the flex wallet's collateral) and
`GET /derivatives/api/v3/openpositions`, nothing else. The "Read only" key permission."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .base import Account, fnum

HOST = "https://futures.kraken.com"
ACCOUNTS = "/derivatives/api/v3/accounts"
POSITIONS = "/derivatives/api/v3/openpositions"


class KrakenFuturesAccount(Account):
    """Kraken Futures API key + secret. Authent = base64(HMAC-SHA512(base64decode(secret),
    SHA-256(postData + nonce + endpointPath))) where endpointPath drops the /derivatives prefix
    (`/api/v3/accounts`) and postData is the query string (empty here); headers APIKey, Authent,
    Nonce. Holdings are the multi-collateral ("flex") wallet's currencies plus any old-style
    cash balances; positions are the PF_ (flex) contracts, sized in the coin (the inverse PI_/FI_
    ones are sized in USD and skipped). The response has no mark price: `fetch_balances` marks
    a position at the live price."""

    id = "krakenfutures"
    label = "Kraken Futures"
    URL = "https://futures.kraken.com/trade/settings/api"     # Settings > API > Create Key, "General API: Read Only"
    note = 'Optional. A read-only ("General API: Read Only", no trading) Kraken Futures API key shows your flex-wallet collateral and open perpetual positions on the Portfolio tab.'
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (ACCOUNTS, POSITIONS)
    HOST = HOST

    def sign(self, path: str, post_data: str = "", nonce: str = "") -> str:
        endpoint = path[len("/derivatives"):] if path.startswith("/derivatives") else path
        digest = hashlib.sha256((post_data + nonce + endpoint).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def headers(self, path: str, post_data: str = "", nonce: str | None = None) -> dict:
        nonce = str(int(time.time() * 1000)) if nonce is None else nonce
        return {"APIKey": self.key, "Authent": self.sign(path, post_data, nonce), "Nonce": nonce}

    @staticmethod
    def asset(code: str) -> str:
        """Kraken's spellings to bases: XBT -> BTC, XDG -> DOGE."""
        c = str(code or "").upper()
        return {"XBT": "BTC", "XDG": "DOGE"}.get(c, c)

    async def _get(self, path: str) -> dict:
        d = await self._json("GET", f"{HOST}{path}", headers=self.headers(path))
        if d.get("result") != "success":
            raise RuntimeError(str(d.get("error") or d.get("errors") or "error")[:120])
        return d

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        accounts = (await self._get(ACCOUNTS)).get("accounts") or {}
        for code, cur in ((accounts.get("flex") or {}).get("currencies") or {}).items():
            q = fnum(cur.get("quantity"))
            if q > 0:
                out.append({"asset": self.asset(code), "qty": q, "hold": 0.0, "id": "flex"})
        for code, q in ((accounts.get("cash") or {}).get("balances") or {}).items():
            if fnum(q) > 0:
                out.append({"asset": self.asset(code), "qty": fnum(q), "hold": 0.0, "id": "cash"})
        for pos in (await self._get(POSITIONS)).get("openPositions") or []:
            name = str(pos.get("symbol") or "").upper()
            if not (name.startswith("PF_") and name.endswith("USD")):
                continue
            size = fnum(pos.get("size")) * (-1.0 if pos.get("side") == "short" else 1.0)
            if size:
                out.append({"asset": self.asset(name[3:-3]), "kind": "perp", "qty": size, "entry": fnum(pos.get("price")),
                            "leverage": fnum(pos.get("maxFixedLeverage")), "upnl": fnum(pos.get("unrealizedPnl"))})
        return out
