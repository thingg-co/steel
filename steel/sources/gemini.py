"""Gemini spot: trades and a top-of-book kept from the v2 L2 stream, the last week of 5-minute
candles. USD-quoted; no key, off by default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Gemini ---------------------------------------------------------------------------

class GeminiSource(Source):
    """Gemini public data. Symbols are `btcusd` on REST and `BTCUSD` on the websocket; the
    symbol list also carries GUSD-, RLUSD- and USDC-quoted pairs (`btcgusd`, `btcrlusd`) and
    perps, which are left out. The v2 stream has no ticker: `l2_updates` carries the whole book
    on subscribe (plus a `trades` backlog that is history) and then deltas as
    [side, price, remaining], so a small per-symbol book keeps the best bid and ask and emits a
    quote when either moves; `trade` events carry the taker's `side`. `/v2/candles` serves a
    week of 5-minute bars newest first, [time ms, open, high, low, close, volume], no range. The
    REST side is slow (seconds a call)."""

    id = "gemini"
    name = "Gemini"
    mechanism = "websocket"
    venue = "spot"
    URL = "https://www.gemini.com"
    priority = 66
    fresh_after = 150.0
    REST = "https://api.gemini.com"
    WS = "wss://api.gemini.com/v2/marketdata"
    TIMEFRAMES = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1hr"}
    BOOK_LEVELS = 1000                                   # a side is trimmed to its best levels past twice this

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()
        self.products_at = 0.0
        self._pairs: dict[str, str] = {}
        self._books: dict[str, tuple[dict, dict]] = {}
        self._best: dict[str, tuple] = {}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym).lower() + "usd"

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    @staticmethod
    def usd_pairs(symbols) -> set[str]:
        """Canonical names for the plain-USD pairs in `/v1/symbols`: `btcusd` yes, `btcgusd` and
        `btcrlusd` no (a base ending in g or rl keeps its pair when no shorter base exists, so
        `paxgusd` is PAXG)."""
        names = set(symbols)
        out = set()
        for s in names:
            if not s.endswith("usd"):
                continue
            base = s[:-3]
            if (base.endswith("g") and base[:-1] + "usd" in names) or (base.endswith("rl") and base[:-2] + "usd" in names):
                continue
            out.add(base.upper() + "-USD")
        return out

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(25)
        try:
            r = await c.get(f"{self.REST}/v1/symbols")
            if r.status_code == 200:
                self.products, self.products_at = self.usd_pairs(r.json()), time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"symbols: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        tf = self.TIMEFRAMES.get(int(self.cfg.bar_seconds), "5m")
        for attempt in range(3):
            r = await c.get(f"{self.REST}/v2/candles/{self.pair(sym)}/{tf}")
            if r.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 400:                          # "Supplied value 'x' is not a valid symbol"
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                return []
            out = []
            for k in r.json() or []:
                if len(k) >= 6:
                    t = _candle_tick(sym, float(k[0]) / 1000.0, k[1], k[2], k[3], k[4], k[5])
                    if t and cutoff <= t.ts < until:
                        out.append(t)
            out.sort(key=lambda t: t.ts)
            await asyncio.sleep(0.2)
            return out
        return []

    async def backfill(self, symbols, cutoff):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client(25) as c:
            await self.list_products(c)
            if self.products:
                syms = [s for s in syms if s in self.products]
            now = time.time()
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, now), limit=4,
                                        note=lambda t: setattr(self, "note", t))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        syms = [s for s in self.symbols if not self.products or s in self.products]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair(s).upper(): s for s in syms}

        async def subscribe(ws):
            self._books.clear()
            names = list(self._pairs)
            for k in range(0, len(names), 50):
                await ws.send(json.dumps({"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": names[k:k + 50]}]}))
            self.note = f"streaming trades for {len(names)} symbols"

        await self._ws_loop(emit, self.WS, subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        kind = m.get("type")
        sym = self._pairs.get(m.get("symbol"))
        if not sym:
            if kind == "error":
                self.note = f"ws: {str(m.get('message') or m.get('reason'))[:80]}"
            return
        if kind == "trade":
            t = trade_tick(sym, m.get("price"), m.get("quantity"), m.get("side") == "buy", float(m.get("timestamp") or 0) / 1000.0)
            if t:
                self._emit(emit, t)
        elif kind == "l2_updates":
            if "trades" in m:                                 # the initial message: the whole book, and old trades
                self._books[sym] = ({}, {})
            bids, asks = self._books.setdefault(sym, ({}, {}))
            for side, price, remaining in m.get("changes") or []:
                book = bids if side == "buy" else asks
                p, q = float(price), float(remaining)
                if q > 0:
                    book[p] = q
                else:
                    book.pop(p, None)
                if len(book) > 2 * self.BOOK_LEVELS:
                    keep = sorted(book, reverse=side == "buy")[:self.BOOK_LEVELS]
                    book.clear() or book.update({k: 1.0 for k in keep})
            best = (max(bids) if bids else None, min(asks) if asks else None)
            if best[0] and best[1] and best != self._best.get(sym):
                self._best[sym] = best
                t = quote_tick(sym, None, best[0], best[1])
                if t:
                    self._emit(emit, t)
