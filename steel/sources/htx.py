"""HTX (Huobi) spot against USDT: gzip websocket trades and best bid/offer, the last 2,000
5-minute klines. No key; off by default."""
from __future__ import annotations

import asyncio
import gzip
import json
import math
import time
import zlib

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# HTX ------------------------------------------------------------------------------

class HTXSource(Source):
    """HTX public data against USDT. Pairs are lower-case (`btcusdt`). Every websocket frame is
    gzip-compressed JSON, and the server's `{"ping": ms}` must be answered `{"pong": ms}` or it
    drops the socket; `direction` on a trade is the taker's side, and the first trade message after
    a subscribe replays the pair's last trades (minutes old on a thin tape: dropped when stale, so
    a reconnect never double-counts). `/market/history/kline` serves
    only the latest `size` candles (2,000 at most, no date range), so HTX backfills a week of
    5-minute bars but never the deep history. One connection took 160 topics without complaint."""

    id = "htx"
    name = "HTX"
    mechanism = "websocket"
    quote = "USDT"
    venue = "spot"
    URL = "https://www.htx.com"
    priority = 60
    fresh_after = 150.0
    REST = "https://api.huobi.pro"
    WS = "wss://api.huobi.pro/ws"
    MAX_KLINES = 2000
    PERIODS = {60: "1min", 300: "5min", 900: "15min", 1800: "30min", 3600: "60min"}

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()                  # canonical names online against USDT, once listed
        self.products_at = 0.0
        self._pairs: dict[str, str] = {}
        self._replay: set[str] = set()                   # pairs whose post-subscribe replay is still to come

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym).lower() + "usdt"

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        """Online USDT pairs from `/v2/settings/common/symbols` (`sc` symbol, `bc` base, `qc` quote,
        `state`, `te` trading enabled), cached an hour."""
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/v2/settings/common/symbols")
            if r.status_code == 200:
                self.products = {f"{str(p.get('bc')).upper()}-USD" for p in r.json().get("data") or []
                                 if p.get("state") == "online" and p.get("qc") == "usdt" and p.get("te")}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"symbols: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _klines(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        """The latest `size` candles, newest first: {id (bucket start, s), open, close, low, high,
        amount (base), vol (quote), count}. `size` is what `cutoff` needs, 2,000 at most."""
        bar = float(self.cfg.bar_seconds)
        size = min(self.MAX_KLINES, max(1, math.ceil((until - cutoff) / bar) + 2))
        for attempt in range(3):
            r = await c.get(f"{self.REST}/market/history/kline",
                            params={"symbol": self.pair(sym), "period": self.PERIODS.get(int(bar), "5min"), "size": size})
            if r.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                self.note = f"kline {r.status_code}"
                return []
            d = r.json()
            if d.get("status") != "ok":                       # an unknown pair is a 200 with err-code invalid-parameter
                if "symbol" in str(d.get("err-msg") or "").lower():
                    self._unsupported.add(sym)
                self.note = f"{sym}: {d.get('err-msg') or d.get('err-code')}"[:80]
                return []
            out = []
            for k in d.get("data") or []:
                t = _candle_tick(sym, k.get("id"), k.get("open"), k.get("high"), k.get("low"), k.get("close"), k.get("amount"))
                if t and cutoff <= t.ts < until:
                    out.append(t)
            out.sort(key=lambda t: t.ts)
            await asyncio.sleep(0.1)
            return out
        return []

    async def backfill(self, symbols, cutoff):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client() as c:
            await self.list_products(c)
            if self.products:
                syms = [s for s in syms if s in self.products]
            now = time.time()
            return await gather_symbols(syms, lambda s: self._klines(c, s, cutoff, now), limit=4,
                                        note=lambda t: setattr(self, "note", t))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        syms = [s for s in self.symbols if not self.products or s in self.products]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair(s): s for s in syms}

        async def subscribe(ws):
            self._replay = set(self._pairs)
            for p in self._pairs:
                await ws.send(json.dumps({"sub": f"market.{p}.trade.detail", "id": f"t-{p}"}))
                await ws.send(json.dumps({"sub": f"market.{p}.bbo", "id": f"q-{p}"}))
            self.note = f"streaming trades for {len(self._pairs)} pairs"

        await self._ws_loop(emit, self.WS, subscribe, self._handle, decode=self.decode, reply=self.reply)

    @staticmethod
    def decode(raw):
        """Every frame is gzip JSON; a text frame (never seen, but allowed) is plain JSON."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError, zlib.error):
                return None
        return json.loads(raw)

    @staticmethod
    def reply(m):
        return json.dumps({"pong": m["ping"]}) if isinstance(m, dict) and "ping" in m else None

    def _handle(self, m: dict, emit) -> None:
        parts = str(m.get("ch") or "").split(".")             # market.<pair>.trade.detail | market.<pair>.bbo
        if len(parts) < 3 or parts[0] != "market":
            if m.get("status") == "error":
                self.note = f"ws: {str(m.get('err-msg') or m.get('err-code'))[:80]}"
            return
        sym = self._pairs.get(parts[1])
        tick = m.get("tick") or {}
        if not sym:
            return
        if parts[2] == "trade":
            data = tick.get("data") or []
            if parts[1] in self._replay:
                self._replay.discard(parts[1])
                if data and time.time() - max(float(d.get("ts") or 0) for d in data) / 1000.0 > 5.0:
                    return
            for d in data:
                t = trade_tick(sym, d.get("price"), d.get("amount"), d.get("direction") == "buy", float(d.get("ts") or 0) / 1000.0)
                if t:
                    self._emit(emit, t)
        elif parts[2] == "bbo":
            t = quote_tick(sym, None, tick.get("bid"), tick.get("ask"), float(tick.get("quoteTime") or 0) / 1000.0)
            if t:
                self._emit(emit, t)
