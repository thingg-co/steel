"""CoinGecko aggregate prices, polled: quote ticks only, never a candle."""
from __future__ import annotations

import asyncio
import time

import httpx

from .base import Source, Tick, UA, base_of


# CoinGecko ------------------------------------------------------------------------

class CoinGeckoSource(Source):
    """CoinGecko aggregate prices, polled: keeps a price alive for a name no venue streams.
    Quote ticks only (no trades, so no candles). Free tier: about 30 calls a minute across the
    whole app, so this polls once a minute; a demo key raises the limit."""

    id = "coingecko"
    name = "CoinGecko"
    mechanism = "poll"
    venue = "aggregate"
    URL = "https://www.coingecko.com"
    priority = 90
    fresh_after = 240.0
    REST = "https://api.coingecko.com/api/v3"

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.poll_interval = max(30.0, float(getattr(cfg, "coingecko_seconds", 60.0)))
        self.ids: dict[str, str] = {}                     # canonical -> coingecko id

    def _headers(self) -> dict:
        h = {"User-Agent": UA}
        key = getattr(self.cfg, "coingecko_key", "")
        if key:
            h["x-cg-demo-api-key"] = key
        return h

    async def _resolve(self, c) -> None:
        if self.ids:
            return
        want = {base_of(s).lower(): s for s in self.symbols}
        for page in (1, 2):
            r = await c.get(f"{self.REST}/coins/markets", headers=self._headers(),
                            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page})
            if r.status_code != 200:
                self.note = f"markets {r.status_code}"
                return
            for row in r.json():
                sym = want.get(str(row.get("symbol") or "").lower())
                if sym and sym not in self.ids:
                    self.ids[sym] = row["id"]
            await asyncio.sleep(2.0)

    async def run(self, emit):
        if not self.symbols:
            return
        await self._poll_loop(emit)

    async def poll_once(self, emit):
        async with httpx.AsyncClient(timeout=20) as c:
            await self._resolve(c)
            if not self.ids:
                self.note = "no symbol ids resolved"
                return
            r = await c.get(f"{self.REST}/simple/price", headers=self._headers(),
                            params={"ids": ",".join(self.ids.values()), "vs_currencies": "usd"})
            if r.status_code != 200:
                self.note = f"price {r.status_code}"
                return
            d = r.json()
        n = 0
        for sym, cid in self.ids.items():
            px = (d.get(cid) or {}).get("usd")
            if px:
                self._emit(emit, Tick(sym, time.time(), float(px), 0.0, None, quote=True))
                n += 1
        self.note = f"aggregate prices for {n} names"
