"""BloFin: the futures balance, the funding balance and open positions, nothing else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from ..sources import unscale
from .base import Account, fnum, mask

HOST = "https://openapi.blofin.com"
BALANCE = "/api/v1/account/balance"
ASSETS = "/api/v1/asset/balances"
POSITIONS = "/api/v1/account/positions"


class BloFinAccount(Account):
    """BloFin API key, secret and passphrase. The signature is the hex HMAC-SHA256 of
    `path + METHOD + timestamp + nonce + body` (the path with its query string, the timestamp in
    milliseconds, the nonce a UUID), and then the hex STRING base64-encoded (its characters, not
    the bytes it spells, as the docs stress). Positions come in contracts and no account
    endpoint states the contract size, so a position's base size is backed out of the venue's own
    numbers: `initialMargin x leverage / mark` (cross) or `upnl / (mark - entry)`; a position
    opened at the mark with no cross margin is reported in contracts, which the row flags."""

    id = "blofin"
    label = "BloFin"
    URL = "https://blofin.com/account/apis"
    note = 'Optional. A read-only ("Read" only, no trading or withdrawal permission) API key shows your BloFin balances and positions on the Portfolio tab.'
    venue = "perp"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}, {"id": "passphrase", "label": "Passphrase"}]
    endpoints = (BALANCE, ASSETS, POSITIONS)
    HOST = HOST

    def sign(self, method: str, path: str, ts: str, nonce: str, body: str = "") -> str:
        hexed = hmac.new(self.secret.encode(), f"{path}{method.upper()}{ts}{nonce}{body}".encode(), hashlib.sha256).hexdigest()
        return base64.b64encode(hexed.encode()).decode()

    def headers(self, method: str, path: str, ts=None, nonce: str | None = None, body: str = "") -> dict:
        ts = str(ts if ts is not None else int(time.time() * 1000))
        nonce = nonce or str(uuid.uuid4())
        return {"ACCESS-KEY": self.key, "ACCESS-SIGN": self.sign(method, path, ts, nonce, body), "ACCESS-TIMESTAMP": ts,
                "ACCESS-NONCE": nonce, "ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}

    @staticmethod
    def asset(code: str) -> str:
        """`1000PEPE-USDT` -> PEPE (the size is scaled by the caller); `BTC-USDT` -> BTC."""
        return unscale(str(code or "").split("-")[0])[0].upper()

    @staticmethod
    def position_size(p: dict) -> tuple[float, bool]:
        """Signed base size of a position row and whether it is exact. `positions` is contracts
        (the sign is the side under `net`; `positionSide` says it otherwise), so the base size is
        the notional the venue implies divided by the mark."""
        contracts = fnum(p.get("positions"))
        if p.get("positionSide") == "short":
            contracts = -abs(contracts)
        sign = 1.0 if contracts >= 0 else -1.0
        mark, entry, upnl = fnum(p.get("markPrice")), fnum(p.get("averagePrice")), fnum(p.get("unrealizedPnl"))
        margin, lev = fnum(p.get("initialMargin")), fnum(p.get("leverage"))
        if margin > 0 and lev > 0 and mark > 0:
            return sign * margin * lev / mark, True
        if upnl and mark > 0 and entry > 0 and abs(mark - entry) > 1e-5 * mark:
            return sign * abs(upnl / (mark - entry)), True
        return contracts, False

    def parse(self, futures: dict, funding: list, positions: list) -> list[dict]:
        acct = mask(self.key)
        out: list[dict] = []
        for d in (futures or {}).get("details") or []:
            qty = fnum(d.get("balance")) or fnum(d.get("equity"))
            if qty > 0:
                out.append({"asset": str(d.get("currency") or "").upper(), "qty": qty, "hold": fnum(d.get("frozen")), "id": acct})
        for d in funding or []:
            qty = fnum(d.get("balance"))
            if qty > 0:
                out.append({"asset": str(d.get("currency") or "").upper(), "qty": qty, "hold": fnum(d.get("frozen")), "id": acct})
        for p in positions or []:
            inst = str(p.get("instId") or "")
            factor = unscale(inst.split("-")[0])[1]
            qty, exact = self.position_size(p)
            if not qty:
                continue
            out.append({"asset": self.asset(inst), "kind": "perp", "qty": qty * factor, "price": fnum(p.get("markPrice")) / factor,
                        "entry": fnum(p.get("averagePrice")) / factor, "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unrealizedPnl")),
                        "id": acct, "note": None if exact else "size in contracts"})
        return out

    async def _get(self, path: str):
        d = await self._json("GET", f"{HOST}{path}", headers=self.headers("GET", path))
        if str(d.get("code")) != "0":
            raise RuntimeError(f"{d.get('code')}: {str(d.get('msg'))[:100]}")
        return d.get("data")

    async def balances(self) -> list[dict]:
        futures = await self._get(BALANCE)
        funding = await self._get(f"{ASSETS}?accountType=funding")
        positions = await self._get(POSITIONS)
        return self.parse(futures or {}, funding or [], positions or [])
