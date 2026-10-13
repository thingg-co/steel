"""Bitfinex: `POST /v2/auth/r/wallets` and the account's fee rates from `POST /v2/auth/r/summary`
(a key with Read permissions), nothing else."""
from __future__ import annotations

import hashlib
import hmac
import time

from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.bitfinex.com"
WALLETS = "/v2/auth/r/wallets"
SUMMARY = "/v2/auth/r/summary"                         # [..., FEE_INFO at index 4: [[maker, maker, maker, _, _, deriv_rebate], [taker_crypto, taker_stable, taker_fiat, _, _, deriv_taker]], ...]


class BitfinexAccount(Account):
    """Bitfinex API key + secret: `bfx-signature` is the hex HMAC-SHA384 of
    "/api" + path + nonce + body ("{}"), with `bfx-nonce` (ms) and `bfx-apikey` alongside.
    Wallet rows are [TYPE, CURRENCY, BALANCE, UNSETTLED_INTEREST, AVAILABLE_BALANCE, ...] across
    the exchange, margin and funding wallets, summed per currency."""

    id = "bitfinex"
    label = "Bitfinex"
    URL = "https://www.bitfinex.com/api"
    note = "Optional. A read-only (Read permissions only, no Write or Withdraw) API key shows your Bitfinex balances on the Portfolio tab."
    venue = "spot"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (WALLETS, SUMMARY)
    HOST = HOST
    ALIASES = {"UST": "USDT", "UDC": "USDC", "DSH": "DASH", "ALG": "ALGO", "ATO": "ATOM", "IOT": "IOTA", "OMN": "OMNI",
               "GTX": "GT", "OPX": "OP", "LBT": "LBTC", "EUT": "EURT"}

    def __init__(self, key: str = "", secret: str = "", **creds):
        super().__init__(key=creds.get("key") or key, secret=creds.get("secret") or secret)

    def sign(self, path: str, nonce: str, body: str = "{}") -> str:
        return hmac.new(self.secret.encode(), f"/api{path}{nonce}{body}".encode(), hashlib.sha384).hexdigest()

    def headers(self, path: str, nonce: str | None = None, body: str = "{}") -> dict:
        nonce = nonce or str(int(time.time() * 1000))
        return {"bfx-nonce": nonce, "bfx-apikey": self.key, "bfx-signature": self.sign(path, nonce, body),
                "Content-Type": "application/json"}

    @classmethod
    def asset(cls, code: str) -> str:
        """UST -> USDT, UDC -> USDC, the derivatives-wallet F0 suffix dropped (BTCF0 -> BTC);
        TEST (paper) currencies are empty, so they are skipped."""
        c = str(code or "").upper()
        if c.startswith("TEST"):
            return ""
        if c.endswith("F0") and len(c) > 4:
            c = c[:-2]
        return cls.ALIASES.get(c, c)

    async def balances(self) -> list[dict]:
        rows = await self._json("POST", f"{HOST}{WALLETS}", content="{}", headers=self.headers(WALLETS))
        if isinstance(rows, list) and rows and rows[0] == "error":
            raise RuntimeError(str(rows[-1])[:120])
        per: dict[str, list[float]] = {}
        for w in rows or []:
            if not isinstance(w, list) or len(w) < 5:
                continue
            a, q = self.asset(w[1]), fnum(w[2])
            if not a or q <= 0:
                continue
            tot = per.setdefault(a, [0.0, 0.0])
            tot[0] += q
            tot[1] += max(0.0, q - fnum(w[4], q))
        return [{"asset": a, "qty": q, "hold": h, "id": "bitfinex"} for a, (q, h) in per.items()]

    async def fees(self) -> dict | None:
        """The summary's FEE_INFO (index 4): maker rates `[0]` and taker rates `[1]`, each a
        six-slot array of decimals (0.002 is 20 bps). Spot takes the crypto-to-fiat taker rate
        (slot 2: Steel trades Bitfinex's USD pairs; the stable and crypto slots stand in) and the
        maker rate (slot 0); perps take the derivative taker fee (taker slot 5) and the
        derivative maker *rebate* (maker slot 5, a positive number the venue pays), returned
        negative because a rebate is a negative fee."""
        if not self.configured:
            return None
        rows = await self._json("POST", f"{HOST}{SUMMARY}", content="{}", headers=self.headers(SUMMARY))
        if isinstance(rows, list) and rows and rows[0] == "error":
            raise RuntimeError(str(rows[-1])[:120])
        info = rows[4] if isinstance(rows, list) and len(rows) > 4 and isinstance(rows[4], list) else []
        maker = info[0] if len(info) > 0 and isinstance(info[0], list) else []
        taker = info[1] if len(info) > 1 and isinstance(info[1], list) else []

        def slot(arr, i):
            return arr[i] if len(arr) > i else None
        spot_t = next((v for v in (slot(taker, 2), slot(taker, 1), slot(taker, 0)) if v is not None), None)
        rebate = fee_bps(slot(maker, 5))
        return fee_rates(spot_taker=fee_bps(spot_t), spot_maker=fee_bps(slot(maker, 0)), perp_taker=fee_bps(slot(taker, 5)),
                         perp_maker=None if rebate is None else -rebate)
