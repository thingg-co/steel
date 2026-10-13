"""Gate.io spot against USDT: spot.trades and spot.book_ticker over the v4 websocket, candles by range."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Gate.io --------------------------------------------------------------------------

class GateIOSource(Source):
    """Gate.io spot public data against USDT (`BTC_USDT`): `spot.trades` (one trade per message,
    the taker's side, `create_time_ms` a decimal string) and `spot.book_ticker` over the v4
    websocket, and candlesticks by `from`/`to` (1,000 points a request, paged forward). Rows come
    oldest first as [t, QUOTE_VOL, CLOSE, high, low, OPEN, base_vol, closed]. An unknown pair is
    400 INVALID_CURRENCY_PAIR. No key; off by default."""

    id = "gateio"
    name = "Gate.io"
    mechanism = "websocket"
    quote = "USDT"
    venue = "spot"
    URL = "https://www.gate.com"
    priority = 56
    fresh_after = 150.0
    REST = "https://api.gateio.ws/api/v4"
    WS = "wss://api.gateio.ws/ws/v4/"
    PER_REQUEST = 1000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()
        self.products_at = 0.0
        self._pairs = {self.pair(s): s for s in self.symbols}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "_USDT"

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/spot/currency_pairs")
            if r.status_code == 200:
                self.products = {f"{p['base']}-USD" for p in r.json() if p.get("quote") == "USDT" and p.get("trade_status") == "tradable"}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"pairs: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first, one window of PER_REQUEST bars per request (`limit` is
        refused next to `from`/`to`)."""
        out: list[Tick] = []
        span = self.PER_REQUEST * float(self.cfg.bar_seconds)
        lo = float(start)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            if lo >= end:
                break
            hi = min(end, lo + span)
            r = await c.get(f"{self.REST}/spot/candlesticks",
                            params={"currency_pair": self.pair(sym), "interval": f"{int(self.cfg.bar_seconds // 60)}m", "from": int(lo), "to": int(hi) - 1})
            if r.status_code == 429:
                await asyncio.sleep(1.5)
                continue
            if r.status_code == 400 and "INVALID_CURRENCY_PAIR" in r.text:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"candles {r.status_code}: {r.text[:60]}"
                return out
            for k in r.json():                              # [t, quote_vol, close, high, low, open, base_vol, closed]
                t = _candle_tick(sym, int(k[0]), k[5], k[3], k[4], k[2], k[6])
                if t and start <= t.ts < end:
                    out.append(t)
            lo = hi
            await asyncio.sleep(0.1)                        # public limit is 200 per 10 s
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
                missing = [s for s in syms if s not in self.products]
                syms = [s for s in syms if s in self.products]
                if missing:
                    self.note = f"not listed on Gate.io: {', '.join(base_of(s) for s in missing[:8])}"
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4,
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

        async def subscribe(ws):
            pairs = list(self._pairs)
            for k in range(0, len(pairs), 50):
                for ch in ("spot.trades", "spot.book_ticker"):
                    await ws.send(json.dumps({"time": int(time.time()), "channel": ch, "event": "subscribe", "payload": pairs[k:k + 50]}))
            self.note = f"streaming trades for {len(pairs)} pairs"

        async def ping(ws):
            await ws.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=30.0)

    def _handle(self, m: dict, emit) -> None:
        ch, ev, res = m.get("channel"), m.get("event"), m.get("result")
        if m.get("error") or (ev == "subscribe" and isinstance(res, dict) and res.get("status") != "success"):
            self.note = f"ws: {str(m.get('error') or res)[:80]}"
            return
        if ev != "update":
            return
        for d in res if isinstance(res, list) else [res]:
            if ch == "spot.trades":
                sym = self._pairs.get(d.get("currency_pair"))
                t = trade_tick(sym, d.get("price"), d.get("amount"), d.get("side") == "buy", float(d.get("create_time_ms") or 0) / 1000.0) if sym else None
            elif ch == "spot.book_ticker":
                sym = self._pairs.get(d.get("s"))
                t = quote_tick(sym, None, d.get("b"), d.get("a")) if sym else None
            else:
                return
            if t:
                self._emit(emit, t)
