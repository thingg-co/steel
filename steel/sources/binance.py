"""Binance spot against USDT: trade and book-ticker streams, klines by date range."""
from __future__ import annotations

import asyncio
import time

import httpx

from .base import Source, Tick, UA, _candle_tick, base_of


# Binance --------------------------------------------------------------------------

class BinanceSource(Source):
    """Binance spot public data against USDT: trade and book-ticker streams, and klines by
    date range (1,000 a request). Off by default: binance.com is not for US residents; set
    STEEL_BINANCE_HOST=api.binance.us / STEEL_BINANCE_WS=wss://stream.binance.us:9443 there."""

    id = "binance"
    name = "Binance"
    mechanism = "websocket"
    quote = "USDT"
    URL = "https://www.binance.com"
    priority = 30
    fresh_after = 150.0

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        import os
        self.host = os.environ.get("STEEL_BINANCE_HOST", "api.binance.com")
        self.ws = os.environ.get("STEEL_BINANCE_WS", "wss://stream.binance.com:9443")

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "USDT"

    async def _klines(self, c, sym: str, start: float, end: float) -> list[Tick]:
        out: list[Tick] = []
        lo = int(start * 1000)
        while lo < int(end * 1000):
            r = await c.get(f"https://{self.host}/api/v3/klines",
                            params={"symbol": self.pair(sym), "interval": "5m", "startTime": lo, "endTime": int(end * 1000) - 1, "limit": 1000})
            if r.status_code == 400:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"klines {r.status_code}"
                return out
            rows = r.json()
            if not rows:
                break
            for k in rows:                                # [openTime, o, h, l, c, vol, closeTime, ...]
                t = _candle_tick(sym, k[0] / 1000.0, k[1], k[2], k[3], k[4], k[5])
                if t and start <= t.ts < end:
                    out.append(t)
            lo = int(rows[-1][0]) + 1
            if len(rows) < 1000:
                break
            await asyncio.sleep(0.1)
        return out

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        sem = asyncio.Semaphore(4)

        async def one(c, s):
            async with sem:
                try:
                    return await self._klines(c, s, cutoff, until)
                except Exception as e:  # noqa: BLE001
                    self.note = f"{s}: {type(e).__name__}"
                    return []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            groups = await asyncio.gather(*[one(c, s) for s in syms])
        return [t for g in groups for t in g]

    async def run(self, emit):
        if not self.symbols:
            return
        self._pairs = {self.pair(s): s for s in self.symbols}
        streams = "/".join(f"{p.lower()}@trade/{p.lower()}@bookTicker" for p in self._pairs)

        async def subscribe(ws):
            self.note = f"streaming trades for {len(self._pairs)} pairs"

        await self._ws_loop(emit, f"{self.ws}/stream?streams={streams}", subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        d = m.get("data") or {}
        sym = self._pairs.get(d.get("s"))
        if not sym:
            return
        if d.get("e") == "trade":
            px, qty = float(d.get("p") or 0), float(d.get("q") or 0)
            if px > 0 and qty > 0:
                self._emit(emit, Tick(sym, float(d.get("T") or 0) / 1000.0 or time.time(), px, qty, not d.get("m")))   # m: the buyer was the maker
        elif "b" in d and "a" in d:
            bid, ask = float(d.get("b") or 0), float(d.get("a") or 0)
            if bid > 0 and ask > 0:
                self._emit(emit, Tick(sym, time.time(), (bid + ask) / 2, 0.0, None, bid, ask, quote=True))
