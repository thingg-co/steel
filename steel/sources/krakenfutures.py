"""Kraken Futures: the USD-margined ("flex", PF_) perpetuals. Trades and ticker over the v1
websocket, 5-minute candles by date range from the charts API, the instrument list hourly."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick

ALIASES = {"BTC": "XBT"}                       # Kraken spells bitcoin XBT
UNALIAS = {v: k for k, v in ALIASES.items()}


# Kraken Futures -------------------------------------------------------------------

class KrakenFuturesSource(Source):
    """Kraken Futures public data for the PF_ contracts (flex: USD-margined, linear, sized in the
    base coin, `contractSize` 1): the v1 websocket's trade and ticker feeds, and 5-minute candles
    by date range from the charts API (oldest first, 2,000 a call, `more_candles` says to page
    on). The older PI_/FI_ inverse contracts are sized in USD and are not mapped. A trade's `side`
    is the taker's; the `trade_snapshot` that opens a subscription is history and is skipped (the
    backfill has it). An unknown symbol gets 200 with no candles, never a 4xx, so `knows` comes
    from the instrument list. No key; off by default."""

    id = "krakenfutures"
    name = "Kraken Futures"
    mechanism = "websocket"
    venue = "perp"
    URL = "https://futures.kraken.com"
    priority = 76
    fresh_after = 150.0
    REST = "https://futures.kraken.com"
    WS = "wss://futures.kraken.com/ws/v1"
    PER_REQUEST = 2000
    CHART = "trade"                             # the charts API's tick type: traded prices ("mark" and "spot" carry no volume)

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()         # tradeable PF_ names, once listed
        self.products_at = 0.0
        self._pairs: dict[str, str] = {}

    @staticmethod
    def pair(sym: str) -> str:
        b = base_of(sym)
        return f"PF_{ALIASES.get(b, b)}USD"

    @staticmethod
    def unpair(name: str) -> str | None:
        n = str(name or "").upper()
        if not (n.startswith("PF_") and n.endswith("USD")):
            return None
        b = n[3:-3]
        return f"{UNALIAS.get(b, b)}-USD"

    def knows(self, sym: str) -> bool:
        return self.pair(sym) in self.products if self.products else self.supports(sym)

    def _note(self, text: str) -> None:
        self.note = text

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/derivatives/api/v3/instruments")
            if r.status_code == 200:
                self.products = {str(i.get("symbol") or "").upper() for i in r.json().get("instruments") or []
                                 if str(i.get("symbol") or "").upper().startswith("PF_") and i.get("tradeable")}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"instruments: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first. Rows are {time (ms, the bucket's start), open, high, low,
        close, volume (base)}, `from`/`to` are seconds, a call is capped at 2,000 candles from
        `from`, so page forward while `more_candles`."""
        bar = int(self.cfg.bar_seconds)
        url = f"{self.REST}/api/charts/v1/{self.CHART}/{self.pair(sym)}/{bar // 60}m"
        out: list[Tick] = []
        lo, tries = int(start), 0
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(url, params={"from": lo, "to": int(end)})
            if r.status_code == 429 and tries < 3:
                tries += 1
                await asyncio.sleep(1.5 * tries)
                continue
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                break
            d = r.json()
            rows = d.get("candles") or []
            for k in rows:
                t = _candle_tick(sym, int(k["time"]) / 1000.0, k["open"], k["high"], k["low"], k["close"], k["volume"])
                if t and start <= t.ts < end:
                    out.append(t)
            if not rows or not d.get("more_candles"):
                break
            lo = int(rows[-1]["time"]) // 1000 + bar
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
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4, note=self._note)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        self._pairs = {self.pair(s): s for s in self.symbols if self.knows(s)}
        if not self._pairs:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            for feed in ("trade", "ticker"):
                await ws.send(json.dumps({"event": "subscribe", "feed": feed, "product_ids": list(self._pairs)}))
            self.note = f"streaming trades for {len(self._pairs)} contracts"

        await self._ws_loop(emit, self.WS, subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        feed = m.get("feed")
        sym = self._pairs.get(m.get("product_id"))
        if feed == "trade" and sym:
            if m.get("type") in ("termination", "assignment"):     # settlement events, not price discovery
                return
            t = trade_tick(sym, m.get("price"), m.get("qty"), m.get("side") == "buy", int(m.get("time") or 0) / 1000.0 or None)
            if t:
                self._emit(emit, t)
        elif feed == "ticker" and sym:
            t = quote_tick(sym, m.get("last"), m.get("bid"), m.get("ask"), ts=int(m.get("time") or 0) / 1000.0 or None)
            if t:
                self._emit(emit, t)
        elif m.get("event") in ("error", "subscribed_failed"):
            self.note = f"ws: {str(m.get('message') or m.get('feed'))[:80]}"
