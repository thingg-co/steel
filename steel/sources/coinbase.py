"""Coinbase Exchange: trades and ticker over the websocket, 5-minute candles by date range,
the deep backfill. Spot, USD-quoted, no key."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .base import Source, Tick, UA, _candle_tick, _iso, _iso_ts, base_of, client


# Coinbase -------------------------------------------------------------------------

class CoinbaseSource(Source):
    """Coinbase Exchange public data: a trade ("matches") and ticker websocket, and 5-minute
    candles by date range (300 a request, years deep). No key. The primary feed and the deep
    backfill: at 403 USD products it covers nearly everything a US user can hold."""

    id = "coinbase"
    name = "Coinbase"
    mechanism = "websocket"
    default_on = True
    URL = "https://www.coinbase.com"
    priority = 10
    fresh_after = 150.0
    PER_REQUEST = 300
    VOLUME_TICKERS = 250                              # the most one-off ticker reads a volumes() pass makes for unpriced names

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.ws_url = getattr(cfg, "coinbase_ws", "wss://ws-feed.exchange.coinbase.com")
        self.rest = getattr(cfg, "coinbase_rest", "https://api.exchange.coinbase.com")
        self.products: set[str] = set()                  # online USD products, once listed
        self.products_at = 0.0
        self._sem = asyncio.Semaphore(6)                  # public limit is 10 requests a second

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    async def list_products(self, c: httpx.AsyncClient | None = None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or httpx.AsyncClient(timeout=20, headers={"User-Agent": UA})
        try:
            r = await c.get(f"{self.rest}/products")
            if r.status_code == 200:
                self.products = {p["id"] for p in r.json()
                                 if p.get("quote_currency") == "USD" and p.get("status") == "online"
                                 and not p.get("trading_disabled")}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"products: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def volumes(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        """`/products/volume-summary` lists every product's 24-hour spot volume in base units;
        times a price (the caller's map first, then one `/products/{id}/ticker` each for the
        names it lacks, paced, at most `VOLUME_TICKERS` of them) it is the daily dollar volume
        of every USD product the exchange lists."""
        prices = dict(prices or {})
        out: dict[str, float] = {}
        async with client(25) as c:
            r = await c.get(f"{self.rest}/products/volume-summary")
            if r.status_code != 200:
                self.note = f"volume summary {r.status_code}"
                return out
            rows = [p for p in r.json() if p.get("quote_currency") == "USD"]
            base_vol = {str(p["id"]): float(p.get("spot_volume_24hour") or 0.0) for p in rows if p.get("id")}
            missing = [sym for sym, v in base_vol.items() if v > 0 and not prices.get(sym)]
            for sym in sorted(missing, key=lambda x: -base_vol[x])[:self.VOLUME_TICKERS]:
                try:
                    async with self._sem:
                        t = await c.get(f"{self.rest}/products/{sym}/ticker")
                        await asyncio.sleep(0.12)
                    if t.status_code == 200:
                        prices[sym] = float(t.json().get("price") or 0.0)
                except Exception:  # noqa: BLE001
                    continue
        for sym, v in base_vol.items():
            px = float(prices.get(sym) or 0.0)
            if v > 0 and px > 0:
                out[sym] = v * px
        return out

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """One page of candles [start, end), oldest first. Coinbase returns [time, low, high, open,
        close, volume] newest first, `time` the bucket's start."""
        async with self._sem:
            for attempt in range(3):
                r = await c.get(f"{self.rest}/products/{sym}/candles",
                                params={"granularity": int(self.cfg.bar_seconds), "start": _iso(start), "end": _iso(end)})
                if r.status_code == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    self._unsupported.add(sym)
                    return []
                if r.status_code != 200:
                    self.note = f"candles {r.status_code}"
                    return []
                out = []
                for row in r.json():
                    if len(row) >= 6 and start <= float(row[0]) < end:
                        t = _candle_tick(sym, row[0], row[3], row[2], row[1], row[4], row[5])
                        if t:
                            out.append(t)
                out.sort(key=lambda t: t.ts)
                await asyncio.sleep(0.12)
                return out
        return []

    async def _range(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        span = self.PER_REQUEST * float(self.cfg.bar_seconds)
        pages = []
        lo = cutoff
        while lo < until:
            hi = min(until, lo + span)
            pages.append((lo, hi))
            lo = hi
        pages = pages[-int(self.cfg.backfill_pages):] if self.cfg.backfill_pages else pages
        groups = await asyncio.gather(*[self._candles(c, sym, a, b) for a, b in pages], return_exceptions=True)
        out: list[Tick] = []
        for g in groups:
            if isinstance(g, list):
                out.extend(g)
            else:
                self.note = f"{sym}: {type(g).__name__}"
        return out

    async def backfill(self, symbols, cutoff):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            await self.list_products(c)
            if self.products:
                syms = [s for s in syms if s in self.products]
                missing = [s for s in symbols if s not in self.products and "-" in s]
                if missing:
                    self.note = f"not listed on Coinbase: {', '.join(base_of(s) for s in missing[:8])}"
            groups = await asyncio.gather(*[self._range(c, s, cutoff, time.time()) for s in syms])
        return [t for g in groups for t in g]

    async def backfill_range(self, symbols, cutoff, until):
        """Bounded history page [cutoff, until) for the deep-backfill loop. Same tick shape and
        timestamp convention as `backfill`, so pages line up."""
        syms = [s for s in symbols if self.supports(s) and (not self.products or s in self.products)]
        if not syms:
            return []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            groups = await asyncio.gather(*[self._range(c, s, cutoff, until) for s in syms])
        return [t for g in groups for t in g]

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        syms = [s for s in self.symbols if not self.products or s in self.products]
        if not syms:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            await ws.send(json.dumps({"type": "subscribe", "product_ids": syms, "channels": ["matches", "ticker"]}))
            self.note = f"streaming trades for {len(syms)} products"

        await self._ws_loop(emit, self.ws_url, subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        kind = m.get("type")
        sym = m.get("product_id")
        if not sym or sym not in self.symbols:
            if kind == "error":
                self.note = f"ws error: {str(m.get('message') or m.get('reason'))[:80]}"
            return
        if kind == "match":
            # `side` is the maker's side: a sell-side maker means the taker bought (an up-tick)
            px, size = float(m.get("price") or 0), float(m.get("size") or 0)
            if px > 0 and size > 0:
                self._emit(emit, Tick(sym, _iso_ts(m.get("time")) if m.get("time") else time.time(), px, size,
                                      m.get("side") == "sell"))
        elif kind == "ticker":
            px = float(m.get("price") or 0)
            bid, ask = m.get("best_bid"), m.get("best_ask")
            if px > 0:
                self._emit(emit, Tick(sym, _iso_ts(m.get("time")) if m.get("time") else time.time(), px, 0.0, None,
                                      float(bid) if bid else None, float(ask) if ask else None, quote=True))
