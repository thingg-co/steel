"""dYdX v4 (the indexer): perpetuals keyed by their canonical BASE-USD name. Trades over the
v4_trades channel, candles by ISO date range, the market list hourly."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, _iso, _iso_ts, base_of, client, gather_symbols, trade_tick

RESOLUTIONS = {60: "1MIN", 300: "5MINS", 900: "15MINS", 1800: "30MINS", 3600: "1HOUR", 14400: "4HOURS", 86400: "1DAY"}


# dYdX -----------------------------------------------------------------------------

class DydxSource(Source):
    """dYdX v4 public data from the indexer: trades over the `v4_trades` channel (the `subscribed`
    reply carries recent history and is skipped; `channel_data` carries the live fills, `side`
    the taker's, since the indexer builds trades from taker fills) and candles by ISO date range
    (newest first, 1,000 a page, `baseTokenVolume` in the coin). Markets are already spelled
    BASE-USD. No top-of-book channel short of maintaining the order book, so trades only: another
    venue keeps the spread. No key; off by default."""

    id = "dydx"
    name = "dYdX"
    mechanism = "websocket"
    quote = "USDC"
    venue = "perp"
    URL = "https://dydx.trade"
    priority = 78
    fresh_after = 150.0
    REST = "https://indexer.dydx.trade/v4"
    WS = "wss://indexer.dydx.trade/v4/ws"
    PER_REQUEST = 1000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()         # ACTIVE markets, once listed
        self.products_at = 0.0
        self._insts: dict[str, str] = {}

    @staticmethod
    def inst(sym: str) -> str:
        return f"{base_of(sym)}-USD"

    def knows(self, sym: str) -> bool:
        return self.inst(sym) in self.products if self.products else self.supports(sym)

    def _note(self, text: str) -> None:
        self.note = text

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/perpetualMarkets")
            if r.status_code == 200:
                self.products = {str(k) for k, m in (r.json().get("markets") or {}).items() if m.get("status") == "ACTIVE"}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"markets: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first, paging backward from `end`: a page is the newest 1,000
        candles up to `toISO` (inclusive, so the boundary candle repeats and is deduped), rows
        {startedAt, open, high, low, close, baseTokenVolume}. A 400 names an unknown market."""
        res = RESOLUTIONS.get(int(self.cfg.bar_seconds), "5MINS")
        out: dict[float, Tick] = {}
        hi, tries = end, 0
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/candles/perpetualMarkets/{self.inst(sym)}",
                            params={"resolution": res, "fromISO": _iso(start), "toISO": _iso(hi), "limit": self.PER_REQUEST})
            if r.status_code == 429 and tries < 3:
                tries += 1
                await asyncio.sleep(1.5 * tries)
                continue
            if r.status_code == 400:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                break
            rows = r.json().get("candles") or []
            oldest = None
            for k in rows:
                ts = _iso_ts(k["startedAt"])
                oldest = ts if oldest is None else min(oldest, ts)
                t = _candle_tick(sym, ts, k["open"], k["high"], k["low"], k["close"], k["baseTokenVolume"])
                if t and start <= t.ts < end:
                    out.setdefault(t.ts, t)
            if len(rows) < self.PER_REQUEST or oldest is None or oldest <= start:
                break
            hi = oldest
            await asyncio.sleep(0.2)
        return [out[k] for k in sorted(out)]

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client() as c:
            await self.list_products(c)
            if self.products:
                syms = [s for s in syms if self.knows(s)]
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4, note=self._note)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        self._insts = {self.inst(s): s for s in self.symbols if self.knows(s)}
        if not self._insts:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            for i in self._insts:                          # one market per message; spaced for the indexer's subscription limit
                await ws.send(json.dumps({"type": "subscribe", "channel": "v4_trades", "id": i}))
                await asyncio.sleep(0.55)               # the indexer allows two subscribe messages a second
            self.note = f"streaming trades for {len(self._insts)} markets"

        await self._ws_loop(emit, self.WS, subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        if m.get("type") != "channel_data" or m.get("channel") != "v4_trades":
            if m.get("type") == "error":
                self.note = f"ws: {str(m.get('message'))[:80]}"
            return
        sym = self._insts.get(m.get("id"))
        if not sym:
            return
        for d in (m.get("contents") or {}).get("trades") or []:
            t = trade_tick(sym, d.get("price"), d.get("size"), d.get("side") == "BUY",
                           _iso_ts(d["createdAt"]) if d.get("createdAt") else None)
            if t:
                self._emit(emit, t)
