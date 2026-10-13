"""MEXC spot against USDT: protobuf trade streams (decoded by hand), a book-ticker poll, klines by range."""
from __future__ import annotations

import asyncio
import json
import time
from functools import partial

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Protobuf, by hand: MEXC's public spot stream pushes PushDataV3ApiWrapper frames (the old JSON
# channels answer "Blocked!"); two bodies matter, so the fields are walked without a runtime.

def _varint(b: bytes, i: int) -> tuple[int, int]:
    x = shift = 0
    while True:
        c, i = b[i], i + 1
        x |= (c & 0x7F) << shift
        if c < 0x80:
            return x, i
        shift += 7


def _fields(b: bytes) -> list[tuple[int, object]]:
    """One message's (field number, value) pairs: ints for varints, bytes for length-delimited
    fields, None for fixed-width ones (skipped)."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            v, i = None, i + {1: 8, 5: 4}[w]               # KeyError on a wire type this stream never sends
        out.append((f, v))
    return out


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")


def unwrap(raw: bytes) -> dict | None:
    """A pushed frame to {"channel", "symbol", "deals": [{"price", "qty", "buy", "ts"}]} or {...,
    "bid", "ask"}; None for any other body or a frame that does not parse. Field numbers from
    github.com/mexcdevelop/websocket-proto: the wrapper's channel = 1, symbol = 3, publicAggreDeals
    = 314 (deals = 1: price 1, quantity 2, tradeType 3 with 1 a buy and 2 a sell, time 4 in ms)
    and publicAggreBookTicker = 315 (bidPrice 1, askPrice 3)."""
    try:
        m: dict = {"channel": "", "symbol": ""}
        for f, v in _fields(raw):
            if f in (1, 3):
                m["channel" if f == 1 else "symbol"] = _text(v)
            elif f == 314:
                m["deals"] = [{"price": _text(d.get(1)), "qty": _text(d.get(2)), "buy": d.get(3) == 1, "ts": int(d.get(4) or 0) / 1000.0}
                              for d in (dict(_fields(item)) for f2, item in _fields(v) if f2 == 1)]
            elif f == 315:
                d = dict(_fields(v))
                m["bid"], m["ask"] = _text(d.get(1)), _text(d.get(3))
        return m if "deals" in m or "bid" in m else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


# MEXC -----------------------------------------------------------------------------

class MEXCSource(Source):
    """MEXC spot public data against USDT: aggregated deals over the protobuf websocket, 30
    streams a connection (so one connection per 30 names), the book ticker for every symbol in one
    REST call every 15 s, and Binance-shaped klines by date range (1,000 a request). `tradeType`
    1 is a taker buy. An unknown symbol is 400 / code -1121. No key; off by default, and mexc.com
    does not serve US residents."""

    id = "mexc"
    name = "MEXC"
    mechanism = "websocket"
    quote = "USDT"
    venue = "spot"
    URL = "https://www.mexc.com"
    priority = 58
    fresh_after = 150.0
    poll_interval = 15.0
    REST = "https://api.mexc.com"
    WS = "wss://wbs-api.mexc.com/ws"
    PER_CONNECTION = 30

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()
        self.products_at = 0.0
        self._pairs = {self.pair(s): s for s in self.symbols}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "USDT"

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(30)
        try:
            r = await c.get(f"{self.REST}/api/v3/exchangeInfo")
            if r.status_code == 200:
                self.products = {f"{p['baseAsset']}-USD" for p in r.json().get("symbols") or []
                                 if p.get("quoteAsset") == "USDT" and str(p.get("status")) == "1" and p.get("isSpotTradingAllowed")}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"exchangeInfo: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _klines(self, c, sym: str, start: float, end: float) -> list[Tick]:
        out: list[Tick] = []
        lo = int(start * 1000)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            if lo >= int(end * 1000):
                break
            r = await c.get(f"{self.REST}/api/v3/klines",
                            params={"symbol": self.pair(sym), "interval": f"{int(self.cfg.bar_seconds // 60)}m", "startTime": lo,
                                    "endTime": int(end * 1000) - 1, "limit": 1000})
            if r.status_code == 429:
                await asyncio.sleep(1.5)
                continue
            if r.status_code == 400:                        # {"msg": "Invalid symbol.", "code": -1121}
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"klines {r.status_code}"
                return out
            rows = r.json()
            if not rows:
                break
            for k in rows:                                  # [openMs, o, h, l, c, vol, closeMs, quoteVol]
                t = _candle_tick(sym, int(k[0]) / 1000.0, k[1], k[2], k[3], k[4], k[5])
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
        async with client() as c:
            await self.list_products(c)
            if self.products:
                missing = [s for s in syms if s not in self.products]
                syms = [s for s in syms if s in self.products]
                if missing:
                    self.note = f"not listed on MEXC: {', '.join(base_of(s) for s in missing[:8])}"
            return await gather_symbols(syms, lambda s: self._klines(c, s, cutoff, until), limit=4,
                                        note=lambda text: setattr(self, "note", text))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        syms = [s for s in self.symbols if not self.products or s in self.products]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair(s): s for s in syms}
        pairs = list(self._pairs)

        async def subscribe(ws, chunk):
            await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": [f"spot@public.aggre.deals.v3.api.pb@100ms@{p}" for p in chunk]}))
            self.note = f"streaming trades for {len(pairs)} pairs"

        async def ping(ws):
            await ws.send(json.dumps({"method": "PING"}))

        loops = [self._ws_loop(emit, self.WS, partial(subscribe, chunk=pairs[k:k + self.PER_CONNECTION]), self._handle, ping=ping, ping_every=30.0,
                               decode=self._decode) for k in range(0, len(pairs), self.PER_CONNECTION)]
        await asyncio.gather(*loops, self._poll_loop(emit))

    @staticmethod
    def _decode(raw):
        return unwrap(raw) if isinstance(raw, bytes) else json.loads(raw)

    def _handle(self, m: dict, emit) -> None:
        sym = self._pairs.get(m.get("symbol"))
        if "deals" in m:
            for d in m["deals"] if sym else []:
                t = trade_tick(sym, d["price"], d["qty"], d["buy"], d["ts"])
                if t:
                    self._emit(emit, t)
        elif "bid" in m:
            t = quote_tick(sym, None, m.get("bid"), m.get("ask")) if sym else None
            if t:
                self._emit(emit, t)
        elif m.get("code") not in (None, 0) or "Blocked" in str(m.get("msg")) or "Exceeded" in str(m.get("msg")):
            self.note = f"ws: {str(m.get('msg'))[-80:]}"      # the reason comes last, after the stream list

    async def poll_once(self, emit):
        """Bid/ask for every spot pair in one call; quote ticks only, never candles."""
        async with client(15) as c:
            r = await c.get(f"{self.REST}/api/v3/ticker/bookTicker")
        if r.status_code != 200:
            self.note = f"bookTicker {r.status_code}"
            return
        rows = r.json()
        for row in rows if isinstance(rows, list) else []:
            sym = self._pairs.get(row.get("symbol"))
            t = quote_tick(sym, None, row.get("bidPrice"), row.get("askPrice")) if sym else None
            if t:
                self._emit(emit, t)
