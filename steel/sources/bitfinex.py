"""Bitfinex spot: trades and tickers over the v2 websocket (a connection per 25 names), candles
by range (10,000 a request). USD-quoted, UST (USDT) where that is all there is; no key, off by
default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Bitfinex -------------------------------------------------------------------------

class BitfinexSource(Source):
    """Bitfinex public data. Pairs are `tBTCUSD`, or `tAAVE:USD` when the base has more than
    three letters; a few codes differ from the base (UST is USDT, UDC is USDC, DSH is DASH) and
    `pub:map:currency:sym` says which. Websocket messages are arrays keyed by channel id: a
    trade is `[chanId, "te", [ID, MTS, AMOUNT, PRICE]]` with the sign of AMOUNT the taker's side
    (`"tu"` repeats it once the id settles, so only "te" counts; the snapshot on subscribe is
    history); a ticker is `[chanId, [BID, BID_SIZE, ASK, ASK_SIZE, ..., LAST_PRICE, ...]]`. A
    connection is capped at a few dozen subscriptions (10305 "subscribe: limit" past ~56 live),
    so names are spread over connections of `PER_CONN`. Candle rows are [MTS, OPEN, CLOSE, HIGH,
    LOW, VOLUME]: close before high. The candles endpoint allows about 30 requests a minute."""

    id = "bitfinex"
    name = "Bitfinex"
    mechanism = "websocket"
    venue = "spot"
    URL = "https://www.bitfinex.com"
    priority = 64
    fresh_after = 150.0
    REST = "https://api-pub.bitfinex.com/v2"
    WS = "wss://api-pub.bitfinex.com/ws/2"
    PER_REQUEST = 10000
    PER_CONN = 25
    REQUEST_GAP = 2.0
    TIMEFRAMES = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h"}

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.names: dict[str, str] = {}                  # canonical -> tPAIR, once listed
        self.names_at = 0.0
        self._pairs: dict[str, str] = {}
        self._chans: dict[int, tuple] = {}

    @staticmethod
    def pair(sym: str) -> str:
        b = base_of(sym)
        return f"t{b}USD" if len(b) == 3 else f"t{b}:USD"

    def pair_for(self, sym: str) -> str:
        return self.names.get(sym) or self.pair(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.names if self.names else self.supports(sym)

    async def list_pairs(self, c=None, max_age: float = 3600.0) -> dict:
        """`pub:list:pair:exchange` filtered to USD (else UST) quotes, bases renamed through
        `pub:map:currency:sym`; the TEST (paper) pairs are skipped. Cached an hour."""
        if self.names and time.time() - self.names_at < max_age:
            return self.names
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/conf/pub:list:pair:exchange")
            m = await c.get(f"{self.REST}/conf/pub:map:currency:sym")
            if r.status_code == 200:
                alias = {}
                if m.status_code == 200:
                    alias = {str(k).upper(): str(v).upper() for k, v in (m.json() or [[]])[0] if str(v).isalnum()}
                names: dict[str, str] = {}
                for p in (r.json() or [[]])[0]:
                    base, quote = p.split(":", 1) if ":" in p else (p[:-3], p[-3:])
                    if base.startswith("TEST") or quote not in ("USD", "UST"):
                        continue
                    sym = f"{alias.get(base, base)}-USD"
                    if quote == "USD" or sym not in names:
                        names[sym] = f"t{p}"
                self.names, self.names_at = names, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"pairs: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.names

    async def _candles(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        """Pages forward from `start` with sort=1 (oldest first)."""
        out: list[Tick] = []
        lo = int(cutoff * 1000)
        tf = self.TIMEFRAMES.get(int(self.cfg.bar_seconds), "5m")
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = None
            for attempt in range(3):
                r = await c.get(f"{self.REST}/candles/trade:{tf}:{self.pair_for(sym)}/hist",
                                params={"start": lo, "end": int(until * 1000) - 1, "limit": self.PER_REQUEST, "sort": 1})
                if r.status_code != 429:
                    break
                await asyncio.sleep(5.0 * (attempt + 1))
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                return out
            rows = r.json() or []
            if rows and rows[0] == "error":                   # ["error", code, "symbol: invalid"]
                if "symbol" in str(rows).lower():
                    self._unsupported.add(sym)
                self.note = f"{sym}: {str(rows[-1])[:60]}"
                return []
            for k in rows:
                t = _candle_tick(sym, int(k[0]) / 1000.0, k[1], k[3], k[4], k[2], k[5])
                if t and cutoff <= t.ts < until:
                    out.append(t)
            await asyncio.sleep(self.REQUEST_GAP)
            if len(rows) < self.PER_REQUEST:
                break
            lo = int(rows[-1][0]) + 1
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
            if self.names:
                syms = [s for s in syms if s in self.names]
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=1,
                                        note=lambda t: setattr(self, "note", t))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_pairs()
        syms = [s for s in self.symbols if not self.names or s in self.names]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair_for(s): s for s in syms}
        pairs = list(self._pairs)
        await asyncio.gather(*[self._connection(emit, pairs[k:k + self.PER_CONN]) for k in range(0, len(pairs), self.PER_CONN)])

    async def _connection(self, emit, pairs: list[str]) -> None:
        chans: dict[int, tuple] = {}                     # chanId -> (channel, sym); ids are per connection

        async def subscribe(ws):
            chans.clear()
            for p in pairs:
                await ws.send(json.dumps({"event": "subscribe", "channel": "trades", "symbol": p}))
                await ws.send(json.dumps({"event": "subscribe", "channel": "ticker", "symbol": p}))
            self.note = f"streaming trades for {len(self._pairs)} pairs"

        await self._ws_loop(emit, self.WS, subscribe, lambda m, e: self._handle(m, e, chans))

    def _handle(self, m, emit, chans: dict | None = None) -> None:
        chans = self._chans if chans is None else chans
        if isinstance(m, dict):
            if m.get("event") == "subscribed":
                chans[int(m["chanId"])] = (m.get("channel"), self._pairs.get(m.get("symbol")))
            elif m.get("event") == "error":
                self.note = f"ws: {str(m.get('msg'))[:80]}"
            return
        if not isinstance(m, list) or len(m) < 2 or m[1] == "hb":
            return
        channel, sym = chans.get(m[0], (None, None))
        if not sym:
            return
        if channel == "trades" and m[1] == "te":
            _, mts, amount, price = m[2][:4]
            t = trade_tick(sym, price, abs(float(amount)), float(amount) > 0, float(mts) / 1000.0)
        elif channel == "ticker" and isinstance(m[1], list) and len(m[1]) >= 7:
            t = quote_tick(sym, m[1][6], m[1][0], m[1][2])
        else:
            t = None
        if t:
            self._emit(emit, t)
