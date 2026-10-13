"""Bitstamp: `POST /api/v2/account_balances/` and the account's fees from
`POST /api/v2/fees/trading/` (the "View account balance" permission), nothing else."""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from .base import Account, fee_bps, fee_rates, fnum

HOST = "www.bitstamp.net"
BALANCES = "/api/v2/account_balances/"
TRADING_FEES = "/api/v2/fees/trading/"               # [{currency_pair, market, fees: {maker, taker}}]
FEE_MARKET = "btcusd"


class BitstampAccount(Account):
    """Bitstamp API key + secret, v2 auth: X-Auth-Signature is the hex HMAC-SHA256 of
    "BITSTAMP " + key + verb + host + path + query + content-type + nonce + timestamp + "v2" + body,
    with no content type (in the string or the headers) when the body is empty, as ours is;
    X-Auth-Nonce is a uuid4 and X-Auth-Timestamp milliseconds, both within 150 s."""

    id = "bitstamp"
    label = "Bitstamp"
    URL = "https://www.bitstamp.net/account/security/api/"
    note = 'Optional. A read-only ("View account balance" permission only) API key shows your Bitstamp balances on the Portfolio tab.'
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (BALANCES, TRADING_FEES)
    HOST = f"https://{HOST}"

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    def message(self, method: str, path: str, nonce: str, ts: str, query: str = "", content_type: str = "", body: str = "") -> str:
        return f"BITSTAMP {self.key}{method.upper()}{HOST}{path}{query}{content_type}{nonce}{ts}v2{body}"

    def headers(self, method: str, path: str, nonce: str | None = None, ts: str | None = None) -> dict:
        nonce = nonce or str(uuid.uuid4())
        ts = ts or str(int(time.time() * 1000))
        sig = hmac.new(self.secret.encode(), self.message(method, path, nonce, ts).encode(), hashlib.sha256).hexdigest()
        return {"X-Auth": f"BITSTAMP {self.key}", "X-Auth-Signature": sig, "X-Auth-Nonce": nonce, "X-Auth-Timestamp": ts,
                "X-Auth-Version": "v2"}

    async def balances(self) -> list[dict]:
        """[{currency, total, available, reserved}]; `reserved` is the hold."""
        rows = await self._json("POST", f"{self.HOST}{BALANCES}", headers=self.headers("POST", BALANCES))
        if isinstance(rows, dict):
            raise RuntimeError(str(rows.get("reason") or rows.get("message") or rows)[:120])
        return [{"asset": self.asset(r.get("currency")), "qty": fnum(r.get("total")), "hold": fnum(r.get("reserved")), "id": "bitstamp"}
                for r in rows or [] if fnum(r.get("total")) > 0]

    async def fees(self) -> dict | None:
        """The account's fee per market; btcusd's `fees.taker` / `fees.maker` are percents
        ("0.40000" is 40 bps), the convention of every Bitstamp fee field. Spot only."""
        if not self.configured:
            return None
        rows = await self._json("POST", f"{self.HOST}{TRADING_FEES}", headers=self.headers("POST", TRADING_FEES))
        if isinstance(rows, dict):
            raise RuntimeError(str(rows.get("reason") or rows.get("message") or rows)[:120])
        rows = [r for r in rows or [] if isinstance(r, dict)]
        row = next((r for r in rows if str(r.get("market") or r.get("currency_pair") or "").lower() == FEE_MARKET), rows[0] if rows else {})
        f = row.get("fees") or {}
        return fee_rates(spot_taker=fee_bps(f.get("taker"), 100.0), spot_maker=fee_bps(f.get("maker"), 100.0))
