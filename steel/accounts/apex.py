"""ApeX Omni: the account (wallets and open positions) and its balance summary, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from ..sources import unscale
from .base import Account, fnum, mask

HOST = "https://omni.apex.exchange"
ACCOUNT = "/api/v3/account"
BALANCE = "/api/v3/account-balance"


class ApeXAccount(Account):
    """ApeX Omni API key, secret and passphrase. The signature is the base64 HMAC-SHA256 of
    `timestamp + METHOD + path-with-query + dataString` (the timestamp in milliseconds, the data
    string the sorted `k=v&` form of a POST body, empty for a GET) keyed with the BASE64 of the
    secret, not the secret itself, in the headers `APEX-SIGNATURE`, `APEX-TIMESTAMP`,
    `APEX-API-KEY`, `APEX-PASSPHRASE`. The account lists `contractWallets` (`asset`, `balance`)
    and `spotWallets`, and the open positions as `symbol` (`BTC-USDT`; `1000PEPE-USDT` in
    thousands), `side` LONG or SHORT, `size`, `entryPrice`; the balance call carries the oracle
    mark per symbol, which prices the position and its open PnL."""

    id = "apex"
    label = "ApeX Omni"
    URL = "https://omni.apex.exchange/keyManagement"
    note = 'Optional. A read-only (the default key permission, no trading permission) API key shows your ApeX Omni balances and positions on the Portfolio tab.'
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (ACCOUNT, BALANCE)
    HOST = HOST

    def sign(self, ts: str, method: str, path: str, data: str = "") -> str:
        key = base64.standard_b64encode(self.secret.encode())
        digest = hmac.new(key, f"{ts}{method.upper()}{path}{data}".encode(), hashlib.sha256).digest()
        return base64.standard_b64encode(digest).decode()

    def headers(self, method: str, path: str, ts=None, data: str = "") -> dict:
        ts = str(ts if ts is not None else int(time.time() * 1000))
        return {"APEX-SIGNATURE": self.sign(ts, method, path, data), "APEX-TIMESTAMP": ts, "APEX-API-KEY": self.key, "APEX-PASSPHRASE": self.passphrase}

    @staticmethod
    def asset(code: str) -> str:
        """`1000PEPE-USDT` / `1000PEPEUSDT` -> PEPE (the size is scaled by the caller)."""
        c = str(code or "").upper().split("-")[0]
        for q in ("USDT", "USDC"):
            if c.endswith(q) and len(c) > len(q):
                c = c[:-len(q)]
        return unscale(c)[0]

    def parse(self, account: dict, balance: dict | None = None) -> list[dict]:
        acct = mask(self.key)
        out: list[dict] = []
        for key in ("contractWallets", "spotWallets", "wallets"):
            for w in account.get(key) or []:
                qty = fnum(w.get("balance"))
                code = w.get("asset") or w.get("token") or w.get("tokenId")
                if qty > 0 and code:
                    out.append({"asset": str(code).upper(), "qty": qty, "hold": fnum(w.get("pendingWithdrawAmount")) + fnum(w.get("pendingTransferOutAmount")), "id": acct})
        marks = (balance or {}).get("symbolToOraclePrice") or {}
        for p in account.get("openPositions") or account.get("positions") or []:
            symbol = str(p.get("symbol") or "")
            factor = unscale(symbol.split("-")[0])[1]
            size = abs(fnum(p.get("size")))
            if not size:
                continue
            short = str(p.get("side") or "").upper() in ("SHORT", "SELL")
            qty = (-size if short else size) * factor
            entry = fnum(p.get("entryPrice"))
            mark = fnum((marks.get(symbol) or marks.get(symbol.replace("-", "")) or {}).get("oraclePrice"))
            upnl = (mark - entry) * qty / factor if mark and entry else fnum(p.get("unrealizedPnl"))
            out.append({"asset": self.asset(symbol), "kind": "perp", "qty": qty, "price": (mark or entry) / factor, "entry": entry / factor,
                        "leverage": fnum(p.get("leverage")), "upnl": upnl, "id": acct})
        return out

    async def _get(self, path: str):
        d = await self._json("GET", f"{HOST}{path}", headers=self.headers("GET", path))
        if d.get("code") not in (None, 0, "0") or d.get("key") and d.get("msg"):
            raise RuntimeError(f"{d.get('code') or d.get('key')}: {str(d.get('msg'))[:100]}")
        return d.get("data") or {}

    async def balances(self) -> list[dict]:
        account = await self._get(ACCOUNT)
        try:
            balance = await self._get(BALANCE)
        except Exception:  # noqa: BLE001 -- the positions still list without a mark
            balance = {}
        return self.parse(account, balance)
