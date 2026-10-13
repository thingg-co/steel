"""dYdX v4: `GET /v4/addresses/{address}` on the indexer (every subaccount's USDC collateral and
open perpetual positions), nothing else. No key: the public chain address is the credential."""
from __future__ import annotations

import urllib.parse

from ..sources import base_of
from .base import Account, fnum

HOST = "https://indexer.dydx.trade"
ADDRESSES = "/v4/addresses/"


class DydxAccount(Account):
    """A dYdX chain address (dydx1...). The indexer is public: a subaccount's equity, USDC and
    open perpetual positions are readable by anyone who knows the address, so there is no
    secret to keep. Sizes are positive with `side` LONG or SHORT; the response carries no mark
    price (`fetch_balances` marks a position at the live price) and no leverage."""

    id = "dydx"
    label = "dYdX"
    URL = "https://dydx.trade"
    note = ("Optional. dYdX needs no API key: your public chain address (dydx1...) is enough for a read-only view of your "
            "subaccounts' USDC collateral and open perpetual positions on the Portfolio tab.")
    venue = "perp"
    fields = [{"id": "address", "label": "dYdX chain address (dydx1...)"}]
    endpoints = (ADDRESSES + "*",)
    HOST = HOST

    @property
    def configured(self) -> bool:
        return self.address.startswith("dydx1") and len(self.address) > 20

    def path(self) -> str:
        return ADDRESSES + urllib.parse.quote(self.address, safe="")

    async def balances(self) -> list[dict]:
        try:
            d = await self._json("GET", f"{HOST}{self.path()}")
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                raise RuntimeError("no subaccounts found for this address") from None
            raise
        out: list[dict] = []
        for sub in d.get("subaccounts") or []:
            sid = f"subaccount {sub.get('subaccountNumber', 0)}"
            for code, a in (sub.get("assetPositions") or {}).items():
                q = abs(fnum(a.get("size"))) * (-1.0 if a.get("side") == "SHORT" else 1.0)
                if q:
                    out.append({"asset": self.asset(a.get("symbol") or code), "qty": q, "hold": 0.0, "id": sid})
            for market, p in (sub.get("openPerpetualPositions") or {}).items():
                q = abs(fnum(p.get("size"))) * (-1.0 if p.get("side") == "SHORT" else 1.0)
                if q:
                    out.append({"asset": base_of(p.get("market") or market), "kind": "perp", "qty": q, "entry": fnum(p.get("entryPrice")),
                                "leverage": 0.0, "upnl": fnum(p.get("unrealizedPnl"))})
        return out
