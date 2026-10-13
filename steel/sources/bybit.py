"""Bybit spot against USDT: the publicTrade websocket, a ticker poll, klines by range."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .base import Source, Tick, UA, _candle_tick, base_of


# Bybit ----------------------------------------------------------------------------

class BybitSource(Source):
    """Bybit spot public data against USDT: the publicTrade websocket, a ticker poll for bid/ask,
    and klines by date range (200 a request). No key; off by default."""

    id = "bybit"
    name = "Bybit"
    mechanism = "websocket"
    quote = "USDT"
    URL = "https://www.bybit.com"
    priority = 40
    fresh_after = 150.0
    REST = "https://api.bybit.com"
    WS = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.poll_interval = max(5.0, float(getattr(cfg, "bybit_seconds", 15.0)))

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "USDT"

    async def _klines(self, c, sym: str, start: float, end: float) -> list[Tick]:
        out: list[Tick] = []
        hi = int(end * 1000) - 1
        while hi > int(start * 1000):
            r = await c.get(f"{self.REST}/v5/market/kline",
                            params={"category": "spot", "symbol": self.pair(sym), "interval": "5", "start": int(start * 1000), "end": hi, "limit": 200})
            if r.status_code != 200:
                self.note = f"kline {r.status_code}"
                return out
            d = r.json()
            if d.get("retCode") != 0:
                self._unsupported.add(sym)
                return []
            rows = (d.get("result") or {}).get("list") or []      # newest first: [startMs, o, h, l, c, vol, turnover]
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, int(k[0]) / 1000.0, k[1], k[2], k[3], k[4], k[5])
                if t and start <= t.ts < end:
                    out.append(t)
            hi = int(rows[-1][0]) - 1
            if len(rows) < 200:
                break
            await asyncio.sleep(0.1)
        out.sort(key=lambda t: t.ts)
        return out

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        sem = asyncio.Semaphore(4)

        async def one(c, s):
            async with sem:
                try:
                    return await self._klines(c, s, cutoff, until)
                except Exception as e:  # noqa: BLE001
                    self.note = f"{s}: {type(e).__name__}"
                    return []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            groups = await asyncio.gather(*[one(c, s) for s in syms])
        return [t for g in groups for t in g]

    async def run(self, emit):
        if not self.symbols:
            return
        self._pairs = {self.pair(s): s for s in self.symbols}

        async def subscribe(ws):
            args = [f"publicTrade.{p}" for p in self._pairs]
            for k in range(0, len(args), 10):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 10]}))
            self.note = f"streaming trades for {len(args)} pairs"

        async def ping(ws):
            await ws.send(json.dumps({"op": "ping"}))

        await asyncio.gather(self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping), self._poll_loop(emit))

    def _handle(self, m: dict, emit) -> None:
        topic = str(m.get("topic") or "")
        if not topic.startswith("publicTrade."):
            if m.get("success") is False:
                self.note = f"ws: {str(m.get('ret_msg'))[:80]}"
            return
        for d in m.get("data") or []:
            sym = self._pairs.get(d.get("s"))
            px, qty = float(d.get("p") or 0), float(d.get("v") or 0)
            if sym and px > 0 and qty > 0:
                self._emit(emit, Tick(sym, float(d.get("T") or 0) / 1000.0 or time.time(), px, qty, d.get("S") == "Buy"))

    async def poll_once(self, emit):
        """Bid/ask for every spot pair in one call; quote ticks only, never candles."""
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{self.REST}/v5/market/tickers", params={"category": "spot"})
            if r.status_code != 200:
                self.note = f"tickers {r.status_code}"
                return
            rows = (r.json().get("result") or {}).get("list") or []
        n = 0
        pairs = getattr(self, "_pairs", None) or {self.pair(s): s for s in self.symbols}
        for row in rows:
            sym = pairs.get(row.get("symbol"))
            if not sym:
                continue
            px = float(row.get("lastPrice") or 0)
            bid, ask = row.get("bid1Price"), row.get("ask1Price")
            if px > 0:
                self._emit(emit, Tick(sym, time.time(), px, 0.0, None, float(bid) if bid else None,
                                      float(ask) if ask else None, quote=True))
                n += 1
