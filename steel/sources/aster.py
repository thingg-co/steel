"""Aster: a Binance-futures-shaped perp DEX. aggTrade and bookTicker streams, klines by date
range, the exchangeInfo list hourly."""
from __future__ import annotations

import asyncio
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick, unscale

QUOTE = "USDT"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _div(v, scale: float):
    f = _f(v)
    return f / scale if f > 0 else None


# Aster ----------------------------------------------------------------------------

class AsterSource(Source):
    """Aster public data (fapi.asterdex.com, the Binance USD-M futures API shape): aggTrade and
    bookTicker streams, klines by date range (Binance row order, 1,500 a request), the
    exchangeInfo list. Only the USDT-quoted perpetuals in TRADING are mapped (the USD1- and
    U-quoted names and the tokenized stocks are skipped by not being asked for); 1000-prefixed
    names (1000PEPEUSDT) scale through `unscale`. aggTrade `m` true means the buyer was the
    maker, so the taker sold. A 400 names an unknown symbol. No key; off by default."""

    id = "aster"
    name = "Aster"
    mechanism = "websocket"
    quote = "USDT"
    venue = "perp"
    URL = "https://www.asterdex.com"
    priority = 84
    fresh_after = 150.0
    REST = "https://fapi.asterdex.com"
    WS = "wss://fstream.asterdex.com"
    PER_REQUEST = 1500
    STREAMS_PER_SOCKET = 200                   # Binance's cap on a combined stream

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: dict[str, str] = {}      # canonical -> venue symbol, once listed
        self.products_at = 0.0
        self._scale: dict[str, float] = {}      # venue symbol -> 1000 for 1000PEPE-style names
        self._pairs: dict[str, str] = {}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + QUOTE

    def _pair(self, sym: str) -> str:
        return self.products.get(sym) or self.pair(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    def _note(self, text: str) -> None:
        self.note = text

    async def list_products(self, c=None, max_age: float = 3600.0) -> dict[str, str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/fapi/v1/exchangeInfo")
            if r.status_code == 200:
                prods, scale = {}, {}
                for s in r.json().get("symbols") or []:
                    name = str(s.get("symbol") or "")
                    if s.get("status") != "TRADING" or s.get("quoteAsset") != QUOTE or s.get("contractType") != "PERPETUAL":
                        continue
                    base, factor = unscale(name[:-len(QUOTE)])
                    sym = f"{base}-USD"
                    if sym not in prods or factor == 1.0:                 # the plain name wins over a 1000x one
                        prods[sym] = name
                    scale[name] = factor
                self.products, self._scale = prods, scale
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"exchangeInfo: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _klines(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first; rows [openTime, o, h, l, c, vol, closeTime, ...] in ms."""
        name = self._pair(sym)
        scale = self._scale.get(name, 1.0)
        out: list[Tick] = []
        lo, tries = int(start * 1000), 0
        for _ in range(int(self.cfg.backfill_pages) or 40):
            if lo >= int(end * 1000):
                break
            r = await c.get(f"{self.REST}/fapi/v1/klines", params={"symbol": name, "interval": f"{int(self.cfg.bar_seconds) // 60}m",
                                                                 "startTime": lo, "endTime": int(end * 1000) - 1, "limit": self.PER_REQUEST})
            if r.status_code in (418, 429) and tries < 3:
                tries += 1
                await asyncio.sleep(2.0 * tries)
                continue
            if r.status_code == 400:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"klines {r.status_code}"
                break
            rows = r.json()
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, k[0] / 1000.0, _f(k[1]) / scale, _f(k[2]) / scale, _f(k[3]) / scale, _f(k[4]) / scale, _f(k[5]) * scale)
                if t and start <= t.ts < end:
                    out.append(t)
            lo = int(rows[-1][0]) + 1
            if len(rows) < self.PER_REQUEST:
                break
            await asyncio.sleep(0.2)
        out.sort(key=lambda t: t.ts)
        return out

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
            return await gather_symbols(syms, lambda s: self._klines(c, s, cutoff, until), limit=4, note=self._note)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        self._pairs = {self._pair(s): s for s in self.symbols if self.knows(s)}
        if not self._pairs:
            self.note = "none of the active symbols is listed"
            return
        names = list(self._pairs)
        per = self.STREAMS_PER_SOCKET // 2
        loops = []
        for k in range(0, len(names), per):                 # two streams a name; a socket holds 200
            streams = "/".join(f"{p.lower()}@aggTrade/{p.lower()}@bookTicker" for p in names[k:k + per])
            loops.append(self._ws_loop(emit, f"{self.WS}/stream?streams={streams}", self._subscribed, self._handle))
        await asyncio.gather(*loops)

    async def _subscribed(self, ws):
        self.note = f"streaming trades for {len(self._pairs)} contracts"

    def _handle(self, m: dict, emit) -> None:
        d = m.get("data") or {}
        name = d.get("s")
        sym = self._pairs.get(name)
        if not sym:
            return
        scale = self._scale.get(name, 1.0)
        ts = _f(d.get("T")) / 1000.0 or None
        if d.get("e") == "aggTrade":
            t = trade_tick(sym, _f(d.get("p")) / scale, _f(d.get("q")) * scale, not d.get("m"), ts)    # m: the buyer was the maker
        elif "b" in d and "a" in d:
            t = quote_tick(sym, None, _div(d.get("b"), scale), _div(d.get("a"), scale), ts=ts)
        else:
            return
        if t:
            self._emit(emit, t)

