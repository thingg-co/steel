"""BitMEX: wallet margins per currency, open positions and the asset scale table, nothing else.
The venue ceased trading on 2026-09-16; balances stay readable while withdrawals run."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

from ..sources import unscale
from .base import Account, fnum, mask

HOST = "https://www.bitmex.com"
MARGIN = "/api/v1/user/margin"
POSITION = "/api/v1/position"
ASSETS = "/api/v1/wallet/assets"
ALIAS = {"XBT": "BTC"}
SCALES = {"XBT": 8, "USDT": 6, "USDC": 6}       # the smallest unit per currency, when the asset table is unreachable


class BitMEXAccount(Account):
    """BitMEX API key and secret. The signature is the hex HMAC-SHA256 of
    `VERB + path-with-query + expires + body`, `expires` a unix-seconds deadline, in the headers
    `api-expires`, `api-key`, `api-signature`. Amounts come in each currency's smallest unit
    (`XBt` satoshi, `USDt` micro-dollars: `scale` in the public asset table) under odd-cased codes
    (`XBt`, `USDt`), and XBT is BTC. A position's `currentQty` is signed contracts; `homeNotional`
    is its size in base units."""

    id = "bitmex"
    label = "BitMEX"
    URL = "https://www.bitmex.com/app/apiKeys"
    note = 'Optional. BitMEX closed its exchange in September 2026 but still holds balances: a read-only ("Key Permissions: none") API key shows what is left there on the Portfolio tab.'
    venue = "perp"
    fields = [{"id": "key", "label": "API key ID"}, {"id": "secret", "label": "API secret"}]
    endpoints = (MARGIN, POSITION, ASSETS)
    HOST = HOST

    def sign(self, verb: str, path: str, expires: int, body: str = "") -> str:
        return hmac.new(self.secret.encode(), f"{verb.upper()}{path}{expires}{body}".encode(), hashlib.sha256).hexdigest()

    def headers(self, verb: str, path: str, expires: int | None = None, body: str = "") -> dict:
        expires = int(expires if expires is not None else time.time() + 60)
        return {"api-expires": str(expires), "api-key": self.key, "api-signature": self.sign(verb, path, expires, body)}

    @staticmethod
    def asset(code: str) -> str:
        """`XBt` / `XBT` -> BTC, `USDt` -> USDT; a perp symbol `XBTUSDT` -> BTC, `ETHUSD` -> ETH."""
        c = str(code or "").upper()
        for q in ("USDT", "USD"):
            if c.endswith(q) and len(c) > len(q):
                c = c[:-len(q)]
                break
        c = unscale(c)[0]
        return ALIAS.get(c, c)

    def parse(self, margins: list, positions: list, scales: dict | None = None) -> list[dict]:
        acct = mask(self.key)
        scales = {**SCALES, **{str(k).upper(): int(v) for k, v in (scales or {}).items()}}
        out: list[dict] = []
        for m in margins or []:
            cur = str(m.get("currency") or "").upper()
            unit = 10.0 ** scales.get(cur, 8)
            qty = fnum(m.get("walletBalance")) / unit
            if qty > 0:
                out.append({"asset": self.asset(cur), "qty": qty, "hold": max(0.0, qty - fnum(m.get("availableMargin")) / unit), "id": acct})
        for p in positions or []:
            contracts = fnum(p.get("currentQty"))
            if not contracts:
                continue
            symbol = str(p.get("symbol") or "")
            factor = unscale(symbol[:-4] if symbol.endswith("USDT") else symbol[:-3])[1]
            size = abs(fnum(p.get("homeNotional"))) or abs(contracts)
            cur = str(p.get("currency") or "").upper()
            mark = fnum(p.get("markPrice"))
            upnl = fnum(p.get("unrealisedPnl")) / 10.0 ** scales.get(cur, 8)
            if ALIAS.get(cur, cur) == "BTC":                 # an inverse contract settles its PnL in bitcoin
                upnl *= mark
            out.append({"asset": self.asset(symbol), "kind": "perp", "qty": (size if contracts > 0 else -size) * factor, "price": mark / factor,
                        "entry": fnum(p.get("avgEntryPrice")) / factor, "leverage": fnum(p.get("leverage")), "upnl": upnl, "id": acct})
        return out

    async def _get(self, path: str):
        return await self._json("GET", f"{HOST}{path}", headers=self.headers("GET", path))

    async def balances(self) -> list[dict]:
        margins = await self._get(f"{MARGIN}?currency=all")
        if isinstance(margins, dict):
            margins = [margins]
        positions = await self._get(f"{POSITION}?filter={urllib.parse.quote('{\"isOpen\":true}')}")
        scales = {}
        try:
            scales = {a.get("asset"): a.get("scale") for a in await self._get(ASSETS) if a.get("asset") is not None}
        except Exception:  # noqa: BLE001 -- the static table covers the margin currencies
            pass
        return self.parse(margins or [], positions or [], scales)
