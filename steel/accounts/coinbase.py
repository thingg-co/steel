"""Coinbase Advanced Trade (CDP key): `GET /api/v3/brokerage/accounts` and the fee tier from
`GET /api/v3/brokerage/transaction_summary`, nothing else."""
from __future__ import annotations

import secrets
import time

from .base import Account, fee_bps, fee_rates, fnum, mask

HOST = "api.coinbase.com"
ACCOUNTS = "/api/v3/brokerage/accounts"
SUMMARY = "/api/v3/brokerage/transaction_summary"      # the fee tier: {fee_tier: {pricing_tier, taker_fee_rate, maker_fee_rate}}


class CoinbaseAccount(Account):
    """Coinbase Developer Platform API key (ES256 JWT per request, two-minute lifetime). The
    fields are the two in the json the key portal downloads: `name` and `privateKey`."""

    id = "coinbase"
    label = "Coinbase"
    URL = "https://portal.cdp.coinbase.com/access/api"
    note = "Optional. A read-only (View) CDP API key shows your Coinbase balances on the Portfolio tab; market data needs no key."
    fields = [{"id": "name", "label": "Key name (organizations/.../apiKeys/...)"}, {"id": "privateKey", "label": "Private key (PEM)", "multiline": True}]
    endpoints = (ACCOUNTS, SUMMARY)
    HOST = f"https://{HOST}"

    def __init__(self, key_name: str = "", private_key: str = "", **creds):
        super().__init__(name=creds.get("name") or key_name, privateKey=creds.get("privateKey") or private_key)
        self.privateKey = self._clean("privateKey", self.privateKey)

    def _clean(self, field, v):
        return v.replace("\\n", "\n") if field == "privateKey" else v.strip()   # a one-line field carries the PEM with escaped newlines

    @property
    def key_name(self) -> str:
        return self.name

    @key_name.setter
    def key_name(self, v: str) -> None:
        self.name = str(v or "")

    @property
    def private_key(self) -> str:
        return self.privateKey

    @private_key.setter
    def private_key(self, v: str) -> None:
        self.privateKey = self._clean("privateKey", str(v or ""))

    @property
    def configured(self) -> bool:
        return bool(self.name and self.privateKey.strip())

    def jwt(self, method: str, path: str, now: float | None = None) -> str:
        """The request token Coinbase expects: ES256, `uri` = "METHOD host/path", 120 s life, a
        nonce in the header and the key name as `kid`/`sub`."""
        import jwt as pyjwt  # noqa: PLC0415 -- optional at import time; needed only with a key
        now = int(now if now is not None else time.time())
        payload = {"sub": self.name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"{method} {HOST}{path}"}
        return pyjwt.encode(payload, self.privateKey, algorithm="ES256", headers={"kid": self.name, "nonce": secrets.token_hex(16)})

    async def balances(self) -> list[dict]:
        """Every account with a nonzero balance: [{asset, qty, hold, id}], read-only."""
        out: list[dict] = []
        cursor = None
        for _ in range(20):
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            d = await self._json("GET", f"{self.HOST}{ACCOUNTS}", params=params,
                                 headers={"Authorization": f"Bearer {self.jwt('GET', ACCOUNTS)}"})
            for a in d.get("accounts") or []:
                qty = fnum((a.get("available_balance") or {}).get("value"))
                hold = fnum((a.get("hold") or {}).get("value"))
                if qty + hold <= 0:
                    continue
                out.append({"asset": str(a.get("currency") or "").upper(), "qty": qty + hold, "hold": hold, "id": mask(a.get("uuid"))})
            cursor = d.get("cursor") if d.get("has_next") else None
            if not cursor:
                break
        return out

    async def fees(self) -> dict | None:
        """The account's spot fee tier: `fee_tier.taker_fee_rate` / `maker_fee_rate` are decimal
        strings ("0.0040" is 40 bps). Coinbase has no perps for a retail key here."""
        if not self.configured:
            return None
        d = await self._json("GET", f"{self.HOST}{SUMMARY}", params={"product_type": "SPOT"},
                             headers={"Authorization": f"Bearer {self.jwt('GET', SUMMARY)}"})
        tier = (d or {}).get("fee_tier") or {}
        return fee_rates(spot_taker=fee_bps(tier.get("taker_fee_rate")), spot_maker=fee_bps(tier.get("maker_fee_rate")))
