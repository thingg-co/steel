"""HTX (Huobi) spot: `GET /v1/account/accounts`, `GET /v1/account/accounts/{id}/balance` and the
account's fee rate from `GET /v2/reference/transact-fee-rate` (the "Read-Only" permission),
nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

from .base import Account, fee_bps, fee_rates, fnum, mask

HOST = "api.huobi.pro"
ACCOUNTS = "/v1/account/accounts"
FEE_RATE = "/v2/reference/transact-fee-rate"          # ?symbols=btcusdt -> {data: [{symbol, takerFeeRate, makerFeeRate, actualTakerRate, actualMakerRate}]}
FEE_SYMBOL = "btcusdt"


class HTXAccount(Account):
    """HTX API key + secret, signature version 2: HMAC-SHA256 over "METHOD\\nhost\\npath\\n" and
    the sorted, URL-encoded query (AccessKeyId, SignatureMethod, SignatureVersion, a UTC
    Timestamp `YYYY-MM-DDThh:mm:ss`), base64, passed as the `Signature` query parameter."""

    id = "htx"
    label = "HTX"
    URL = "https://www.htx.com/en-us/apikey/"
    note = 'Optional. A read-only ("Read-Only" permission, no trade or withdraw) API key shows your HTX balances on the Portfolio tab.'
    venue = "spot"
    fields = [{"id": "key", "label": "Access key"}, {"id": "secret", "label": "Secret key"}]
    endpoints = (ACCOUNTS, ACCOUNTS + "/*", FEE_RATE)
    HOST = f"https://{HOST}"

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    def params(self, ts: str | None = None) -> dict:
        return {"AccessKeyId": self.key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2",
                "Timestamp": ts or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}

    @staticmethod
    def canonical(method: str, path: str, params: dict) -> str:
        return "\n".join([method.upper(), HOST, path, urllib.parse.urlencode(sorted(params.items()))])

    def sign(self, method: str, path: str, params: dict) -> str:
        mac = hmac.new(self.secret.encode(), self.canonical(method, path, params).encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def signed(self, method: str, path: str, ts: str | None = None, extra: dict | None = None) -> dict:
        """The query for a request: the auth parameters (and any request parameters, which the
        signature covers too) and their signature."""
        p = self.params(ts) | dict(extra or {})
        p["Signature"] = self.sign(method, path, p)
        return p

    async def _read(self, path: str, extra: dict | None = None) -> dict:
        d = await self._json("GET", f"{self.HOST}{path}", params=self.signed("GET", path, extra=extra))
        if d.get("status") != "ok":
            raise RuntimeError(str(d.get("err-msg") or d.get("err-code") or d)[:120])
        return d

    async def balances(self) -> list[dict]:
        """Every working spot account's `trade` plus `frozen` balance per currency; `frozen` is
        the hold."""
        out: list[dict] = []
        for a in (await self._read(ACCOUNTS)).get("data") or []:
            if a.get("type") != "spot" or a.get("state") != "working":
                continue
            per: dict[str, list[float]] = {}
            for row in ((await self._read(f"{ACCOUNTS}/{a['id']}/balance")).get("data") or {}).get("list") or []:
                q, kind = fnum(row.get("balance")), row.get("type")
                if kind not in ("trade", "frozen") or q <= 0:
                    continue
                tot = per.setdefault(self.asset(row.get("currency")), [0.0, 0.0])
                tot[0] += q
                if kind == "frozen":
                    tot[1] += q
            out += [{"asset": cur, "qty": tot, "hold": hold, "id": mask(a.get("id"))} for cur, (tot, hold) in per.items() if tot > 0]
        return out

    async def fees(self) -> dict | None:
        """The account's rate on btcusdt: `actualTakerRate` / `actualMakerRate` (the rate after
        any deduction; the basic `takerFeeRate` / `makerFeeRate` when there is none), decimal
        strings ("0.002" is 20 bps); a negative maker rate is a rebate and stays negative."""
        if not self.configured:
            return None
        d = await self._json("GET", f"{self.HOST}{FEE_RATE}", params=self.signed("GET", FEE_RATE, extra={"symbols": FEE_SYMBOL}))
        if str(d.get("code")) != "200" or d.get("success") is False:     # a v2 answer: {code, success, data}, not v1's {status: "ok"}
            raise RuntimeError(str(d.get("message") or d.get("err-msg") or d.get("code") or d)[:120])
        rows = d.get("data") or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        taker = row.get("actualTakerRate") if row.get("actualTakerRate") not in (None, "") else row.get("takerFeeRate")
        maker = row.get("actualMakerRate") if row.get("actualMakerRate") not in (None, "") else row.get("makerFeeRate")
        return fee_rates(spot_taker=fee_bps(taker), spot_maker=fee_bps(maker))
