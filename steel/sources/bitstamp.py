"""Bitstamp spot: live trades over the websocket, bid/ask from the all-pairs ticker poll, OHLC
by range (1,000 a request). USD-quoted; no key, off by default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Bitstamp -------------------------------------------------------------------------

class BitstampSource(Source):
    """Bitstamp public data. Pairs are `btcusd`; a trade's `type` is 0 for a buy and 1 for a sell,
    the taker's side. The websocket carries trades (`live_trades_<pair>`); its order-book channel
    pushes a hundred levels on every change, so quotes come from `/ticker/` instead, which
    covers every pair in one call every `poll_interval`. `/ohlc/` pages forward from `start`
    (an `end` would override it), rows oldest first with second timestamps as strings. The
    server may send `bts:request_reconnect` and then close; the loop reconnects."""

    id = "bitstamp"
    name = "Bitstamp"
    mechanism = "websocket"
    venue = "spot"
    URL = "https://www.bitstamp.net"
    priority = 68
    fresh_after = 150.0
    poll_interval = 15.0
    REST = "https://www.bitstamp.net/api/v2"
    WS = "wss://ws.bitstamp.net"
    PER_REQUEST = 1000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.pairs: dict[str, str] = {}                  # canonical -> url_symbol, once listed
        self.pairs_at = 0.0
        self._pairs: dict[str, str] = {}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym).lower() + "usd"

    def pair_for(self, sym: str) -> str:
        return self.pairs.get(sym) or self.pair(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.pairs if self.pairs else self.supports(sym)

    async def list_pairs(self, c=None, max_age: float = 3600.0) -> dict:
        """`/trading-pairs-info/`: [{name "BTC/USD", url_symbol "btcusd", trading "Enabled"}]."""
        if self.pairs and time.time() - self.pairs_at < max_age:
            return self.pairs
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/trading-pairs-info/")
            if r.status_code == 200:
                self.pairs = {f"{str(p.get('name')).split('/')[0]}-USD": str(p.get("url_symbol")) for p in r.json()
                              if str(p.get("name", "")).endswith("/USD") and p.get("trading") == "Enabled"}
                self.pairs_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"pairs: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.pairs

    async def _candles(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        out: list[Tick] = []
        bar = int(self.cfg.bar_seconds)
        lo = int(cutoff)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = None
            for attempt in range(3):
                r = await c.get(f"{self.REST}/ohlc/{self.pair_for(sym)}/", params={"step": bar, "limit": self.PER_REQUEST, "start": lo})
                if r.status_code != 429:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))
            if r.status_code == 404:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"ohlc {r.status_code}"
                return out
            rows = (r.json().get("data") or {}).get("ohlc") or []
            for k in rows:
                t = _candle_tick(sym, k.get("timestamp"), k.get("open"), k.get("high"), k.get("low"), k.get("close"), k.get("volume"))
                if t and cutoff <= t.ts < until:
                    out.append(t)
            if len(rows) < self.PER_REQUEST or float(rows[-1].get("timestamp") or 0) + bar >= until:
                break
            lo = int(float(rows[-1]["timestamp"])) + bar
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
            await self.list_pairs(c)
            if self.pairs:
                syms = [s for s in syms if s in self.pairs]
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4,
                                        note=lambda t: setattr(self, "note", t))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_pairs()
        syms = [s for s in self.symbols if not self.pairs or s in self.pairs]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair_for(s): s for s in syms}

        async def subscribe(ws):
            for p in self._pairs:
                await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": f"live_trades_{p}"}}))
            self.note = f"streaming trades for {len(self._pairs)} pairs"

        await asyncio.gather(self._ws_loop(emit, self.WS, subscribe, self._handle), self._poll_loop(emit))

    def _handle(self, m: dict, emit) -> None:
        ev, ch = m.get("event"), str(m.get("channel") or "")
        if ev == "trade" and ch.startswith("live_trades_"):
            sym = self._pairs.get(ch[len("live_trades_"):])
            d = m.get("data") or {}
            if sym:
                ts = float(d.get("microtimestamp") or 0) / 1e6 or float(d.get("timestamp") or 0)
                t = trade_tick(sym, d.get("price"), d.get("amount"), int(d.get("type", -1)) == 0, ts)
                if t:
                    self._emit(emit, t)
        elif ev == "bts:request_reconnect":
            self.note = "server asked for a reconnect"
        elif ev == "bts:error":
            self.note = f"ws: {str((m.get('data') or {}).get('message'))[:80]}"

    async def poll_once(self, emit):
        """Bid/ask for every pair in one call; quote ticks only, never candles."""
        async with client(15) as c:
            r = await c.get(f"{self.REST}/ticker/")
            if r.status_code != 200:
                self.note = f"ticker {r.status_code}"
                return
            self._quotes(r.json(), emit)

    def _quotes(self, rows, emit) -> None:
        pairs = self._pairs or {self.pair_for(s): s for s in self.symbols}
        for row in rows or []:
            base, _, quote = str(row.get("pair") or "").partition("/")
            sym = pairs.get(f"{base.lower()}{quote.lower()}") if quote == "USD" else None
            if sym:
                t = quote_tick(sym, row.get("last"), row.get("bid"), row.get("ask"), float(row.get("timestamp") or 0))
                if t:
                    self._emit(emit, t)
