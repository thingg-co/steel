"""OKX spot against USDT: trades and tickers over the websocket, candles by range."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .base import Source, Tick, UA, _candle_tick, base_of


# OKX ------------------------------------------------------------------------------

class OKXSource(Source):
    """OKX spot public data against USDT: trades and tickers over the websocket, candles by
    range (100 a request). No key; off by default."""

    id = "okx"
    name = "OKX"
    mechanism = "websocket"
    quote = "USDT"
    URL = "https://www.okx.com"
    priority = 50
    fresh_after = 150.0
    REST = "https://www.okx.com/api/v5"
    WS = "wss://ws.okx.com:8443/ws/v5/public"

    @staticmethod
    def inst(sym: str) -> str:
        return base_of(sym) + "-USDT"

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        out: list[Tick] = []
        after = int(end * 1000)                            # records earlier than `after`
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/market/history-candles",
                            params={"instId": self.inst(sym), "bar": "5m", "after": after, "limit": 100})
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                return out
            d = r.json()
            if str(d.get("code")) != "0":
                self._unsupported.add(sym)
                return []
            rows = d.get("data") or []                     # newest first: [ts, o, h, l, c, vol, ...]
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, int(k[0]) / 1000.0, k[1], k[2], k[3], k[4], k[5])
                if t and start <= t.ts < end:
                    out.append(t)
            after = int(rows[-1][0])
            if after <= int(start * 1000) or len(rows) < 100:
                break
            await asyncio.sleep(0.1)
        out.sort(key=lambda t: t.ts)
        return out

    async def backfill(self, symbols, cutoff):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        sem = asyncio.Semaphore(4)

        async def one(c, s):
            async with sem:
                try:
                    return await self._candles(c, s, cutoff, time.time())
                except Exception as e:  # noqa: BLE001
                    self.note = f"{s}: {type(e).__name__}"
                    return []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            groups = await asyncio.gather(*[one(c, s) for s in syms])
        return [t for g in groups for t in g]

    async def run(self, emit):
        if not self.symbols:
            return
        self._insts = {self.inst(s): s for s in self.symbols}

        async def subscribe(ws):
            args = [{"channel": ch, "instId": i} for i in self._insts for ch in ("trades", "tickers")]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            self.note = f"streaming trades for {len(self._insts)} pairs"

        async def ping(ws):
            await ws.send("ping")

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=25.0)

    def _handle(self, m: dict, emit) -> None:
        arg = m.get("arg") or {}
        ch = arg.get("channel")
        for d in m.get("data") or []:
            sym = self._insts.get(d.get("instId"))
            if not sym:
                continue
            if ch == "trades":
                px, qty = float(d.get("px") or 0), float(d.get("sz") or 0)
                if px > 0 and qty > 0:
                    self._emit(emit, Tick(sym, int(d.get("ts") or 0) / 1000.0 or time.time(), px, qty, d.get("side") == "buy"))
            elif ch == "tickers":
                px = float(d.get("last") or 0)
                bid, ask = d.get("bidPx"), d.get("askPx")
                if px > 0:
                    self._emit(emit, Tick(sym, time.time(), px, 0.0, None, float(bid) if bid else None,
                                          float(ask) if ask else None, quote=True))
