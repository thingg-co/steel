"""Gemini: `POST /v1/balances` and the fee tier from `POST /v1/notionalvolume` (the Auditor
role), nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.gemini.com"
BALANCES = "/v1/balances"
NOTIONAL = "/v1/notionalvolume"                        # the 30-day volume and the tier's api_taker_fee_bps / api_maker_fee_bps


class GeminiAccount(Account):
    """Gemini API key + secret. The request travels in headers, not the body: X-GEMINI-PAYLOAD
    is the base64 JSON {request, nonce} and X-GEMINI-SIGNATURE the hex HMAC-SHA384 of that
    base64 text; the body is empty (Content-Length 0, Content-Type text/plain). The nonce is
    epoch seconds: the time-based mode Gemini recommends wants seconds within 30 s of its clock,
    and the incremental mode only needs it to rise, which one read a minute does."""

    id = "gemini"
    label = "Gemini"
    URL = "https://exchange.gemini.com/settings/api"
    note = "Optional. A read-only (Auditor role) API key shows your Gemini balances on the Portfolio tab."
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (BALANCES, NOTIONAL)
    HOST = HOST

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    @staticmethod
    def payload(path: str, nonce: int | None = None) -> str:
        nonce = int(nonce if nonce is not None else time.time())
        return base64.b64encode(json.dumps({"request": path, "nonce": nonce}, separators=(",", ":")).encode()).decode()

    def sign(self, payload: str) -> str:
        return hmac.new(self.secret.encode(), payload.encode(), hashlib.sha384).hexdigest()

    def headers(self, path: str, nonce: int | None = None) -> dict:
        p = self.payload(path, nonce)
        return {"Content-Type": "text/plain", "Content-Length": "0", "Cache-Control": "no-cache",
                "X-GEMINI-APIKEY": self.key, "X-GEMINI-PAYLOAD": p, "X-GEMINI-SIGNATURE": self.sign(p)}

    async def balances(self) -> list[dict]:
        """[{type, currency, amount, available, availableForWithdrawal}]: amount is the total,
        the hold is what is not available."""
        rows = await self._json("POST", f"{HOST}{BALANCES}", headers=self.headers(BALANCES))
        if isinstance(rows, dict):
            raise RuntimeError(str(rows.get("message") or rows.get("reason") or rows)[:120])
        out = []
        for r in rows or []:
            q = fnum(r.get("amount"))
            if q > 0:
                out.append({"asset": self.asset(r.get("currency")), "qty": q, "hold": max(0.0, q - fnum(r.get("available"), q)), "id": "gemini"})
        return out

    async def fees(self) -> dict | None:
        """The tier's API rates, `api_taker_fee_bps` / `api_maker_fee_bps`: integers already in
        bps (35 stays 35.0). Spot only."""
        if not self.configured:
            return None
        d = await self._json("POST", f"{HOST}{NOTIONAL}", headers=self.headers(NOTIONAL))
        if not isinstance(d, dict) or d.get("result") == "error":
            raise RuntimeError(str((d or {}).get("message") if isinstance(d, dict) else d)[:120])
        return fee_rates(spot_taker=fee_bps(d.get("api_taker_fee_bps"), 1.0), spot_maker=fee_bps(d.get("api_maker_fee_bps"), 1.0))
