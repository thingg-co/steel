"""Deribit: `GET /api/v2/private/get_account_summaries` (equity per currency) and
`GET /api/v2/private/get_positions`, nothing else, each signed per request (no token)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from ..sources import unscale
from .base import Account, fnum

HOST = "https://www.deribit.com"
SUMMARIES = "/api/v2/private/get_account_summaries"
POSITIONS = "/api/v2/private/get_positions"


class DeribitAccount(Account):
    """Deribit API key: client id + client secret with read scopes. Every request carries
    `Authorization: deri-hmac-sha256 id=..,ts=..,nonce=..,sig=..` where sig =
    hex(HMAC-SHA256(secret, "ts\\nnonce\\nMETHOD\\nuri-with-query\\nbody\\n")), so no access
    token is ever fetched or stored. Positions: BTC/ETH-PERPETUAL are inverse (`size` in USD,
    `size_currency` and P&L in the coin, converted to USD at the mark); the linear BASE_USDC
    ones are sized in the coin, 1000PEPE-style names scaling through `unscale`."""

    id = "deribit"
    label = "Deribit"
    URL = "https://www.deribit.com/account/BTC/api"
    note = ("Optional. A read-only Deribit API key (read scopes only: account:read and trade:read, no wallet or block scopes) "
            "shows your per-currency equity and open futures positions on the Portfolio tab.")
    venue = "perp"
    fields = [{"id": "key", "label": "Client ID"}, {"id": "secret", "label": "Client secret"}]
    endpoints = (SUMMARIES, POSITIONS)
    HOST = HOST

    def sign(self, method: str, uri: str, body: str = "", ts=None, nonce: str | None = None) -> tuple[str, str, str]:
        """(ts, nonce, sig) for one request; `uri` is the path with its query string."""
        ts = str(int(time.time() * 1000) if ts is None else ts)
        nonce = nonce or secrets.token_hex(8)
        data = f"{ts}\n{nonce}\n{method.upper()}\n{uri}\n{body}\n"
        return ts, nonce, hmac.new(self.secret.encode(), data.encode(), hashlib.sha256).hexdigest()

    def headers(self, method: str, uri: str, body: str = "", ts=None, nonce: str | None = None) -> dict:
        ts, nonce, sig = self.sign(method, uri, body, ts, nonce)
        return {"Authorization": f"deri-hmac-sha256 id={self.key},ts={ts},nonce={nonce},sig={sig}"}

    async def _get(self, uri: str):
        d = await self._json("GET", f"{HOST}{uri}", headers=self.headers("GET", uri))
        if d.get("error"):
            e = d["error"] if isinstance(d["error"], dict) else {"message": d["error"]}
            raise RuntimeError(f"{e.get('message')} ({e.get('code')})"[:120])
        return d.get("result")

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for s in ((await self._get(f"{SUMMARIES}?extended=true")) or {}).get("summaries") or []:
            q = fnum(s.get("equity"))
            if q > 0:
                out.append({"asset": self.asset(s.get("currency")), "qty": q, "hold": max(0.0, q - fnum(s.get("available_funds"))), "id": "deribit"})
        for p in (await self._get(f"{POSITIONS}?currency=any&kind=future")) or []:
            inst = str(p.get("instrument_name") or "")
            base, factor = unscale(inst.split("_")[0].split("-")[0])
            mark = fnum(p.get("mark_price")) / factor
            sign = -1.0 if p.get("direction") == "sell" else 1.0
            if "_" in inst:                                                 # linear: size in the coin
                qty, upnl = abs(fnum(p.get("size"))) * factor, fnum(p.get("floating_profit_loss"))
            else:                                                           # inverse: size in USD, size_currency and P&L in the coin
                qty = abs(fnum(p.get("size_currency"))) or (abs(fnum(p.get("size"))) / mark if mark else 0.0)
                upnl = fnum(p.get("floating_profit_loss")) * mark
            if qty:
                out.append({"asset": base, "kind": "perp", "qty": sign * qty, "price": mark, "entry": fnum(p.get("average_price")) / factor,
                            "leverage": fnum(p.get("leverage")), "upnl": upnl})
        return out
