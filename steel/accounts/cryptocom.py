"""Crypto.com Exchange: `POST /exchange/v1/private/user-balance` and the account's fee tier from
`POST /exchange/v1/private/get-fee-rate` (a key with trading disabled), nothing else."""
from __future__ import annotations

import hashlib
import hmac
import time

from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.crypto.com"
BALANCE = "/exchange/v1/private/user-balance"
FEE_RATE = "/exchange/v1/private/get-fee-rate"        # {result: {spot_tier, deriv_tier, effective_spot_taker_rate_bps, ...}}


class CryptoComAccount(Account):
    """Crypto.com Exchange API key + secret: `sig` is the hex HMAC-SHA256 of
    method + id + api_key + params-string + nonce, where the params string is the sorted keys
    each followed by its value (nested objects and arrays flattened the same way), per the
    docs' example. Numbers inside params must be strings; ours are empty."""

    id = "cryptocom"
    label = "Crypto.com"
    URL = "https://crypto.com/exchange/user/settings/api-management"
    note = "Optional. A read-only (trading disabled, no withdrawal) API key shows your Crypto.com Exchange balances on the Portfolio tab."
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}]
    endpoints = (BALANCE, FEE_RATE)
    HOST = HOST

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    @classmethod
    def params_str(cls, obj, level: int = 0) -> str:
        if level >= 3 or not isinstance(obj, (dict, list)):
            return "null" if obj is None else ("true" if obj is True else "false" if obj is False else str(obj))
        if isinstance(obj, list):
            return "".join(cls.params_str(v, level + 1) for v in obj)
        return "".join(k + cls.params_str(obj[k], level + 1) for k in sorted(obj))

    def sign(self, method: str, req_id: int, params: dict, nonce: int) -> str:
        payload = f"{method}{req_id}{self.key}{self.params_str(params)}{nonce}"
        return hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def request(self, method: str, params: dict | None = None, req_id: int | None = None, nonce: int | None = None) -> dict:
        """The signed POST body for a private method."""
        params = params or {}
        nonce = int(nonce if nonce is not None else time.time() * 1000)
        req_id = int(req_id if req_id is not None else nonce % 1_000_000)
        return {"id": req_id, "method": method, "api_key": self.key, "params": params, "nonce": nonce,
                "sig": self.sign(method, req_id, params, nonce)}

    async def balances(self) -> list[dict]:
        """`position_balances` of the (one) account: every currency's quantity, `reserved_qty`
        the hold. USD rows are cash."""
        d = await self._json("POST", f"{HOST}{BALANCE}", json=self.request("private/user-balance"))
        if str(d.get("code")) != "0":
            raise RuntimeError(f"{d.get('code')}: {str(d.get('message'))[:100]}")
        rows = (((d.get("result") or {}).get("data") or [{}])[0]).get("position_balances") or []
        return [{"asset": self.asset(r.get("instrument_name")), "qty": fnum(r.get("quantity")), "hold": fnum(r.get("reserved_qty")),
                 "id": "cryptocom"} for r in rows if fnum(r.get("quantity")) > 0]

    async def fees(self) -> dict | None:
        """The account's 30-day tier: `effective_spot_taker_rate_bps` and friends are already in
        bps, as strings ("6.9" is 6.9 bps); the deriv pair is the perps'."""
        if not self.configured:
            return None
        d = await self._json("POST", f"{HOST}{FEE_RATE}", json=self.request("private/get-fee-rate"))
        if str(d.get("code")) != "0":
            raise RuntimeError(f"{d.get('code')}: {str(d.get('message'))[:100]}")
        r = d.get("result") or {}
        return fee_rates(spot_taker=fee_bps(r.get("effective_spot_taker_rate_bps"), 1.0), spot_maker=fee_bps(r.get("effective_spot_maker_rate_bps"), 1.0),
                         perp_taker=fee_bps(r.get("effective_deriv_taker_rate_bps"), 1.0), perp_maker=fee_bps(r.get("effective_deriv_maker_rate_bps"), 1.0))
