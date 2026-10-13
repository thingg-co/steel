"""Bitget spot against USDT: trades and tickers over the v2 websocket, candles by end time."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Bitget ---------------------------------------------------------------------------

class BitgetSource(Source):
    """Bitget spot public data against USDT: the v2 `trade` and `ticker` channels and
    history-candles by `endTime` (200 a request, ascending rows, paged backwards). The stream
    reports the taker's side; its trade `snapshot` (recent trades on every subscribe) is skipped
    so a reconnect never counts a trade twice. An unknown pair is 400 / code 40034 on REST and an
    `error` event (code 30001) on the stream. No key; off by default."""

    id = "bitget"
    name = "Bitget"
    mechanism = "websocket"
    quote = "USDT"
    venue = "spot"
    URL = "https://www.bitget.com"
    priority = 52
    fresh_after = 150.0
    REST = "https://api.bitget.com"
    WS = "wss://ws.bitget.com/v2/ws/public"
    PER_REQUEST = 200

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()                  # canonical names listed online, once fetched
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
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/api/v2/spot/public/symbols")
            if r.status_code == 200:
                self.products = {f"{p['baseCoin']}-USD" for p in r.json().get("data") or []
                                 if p.get("quoteCoin") == "USDT" and p.get("status") == "online"}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"symbols: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first. Rows come ascending as [ts(ms), o, h, l, c, baseVol, usdtVol,
        quoteVol] and end just before `endTime`, so each page moves `endTime` to the oldest row."""
        out: list[Tick] = []
        end_ms = int(end * 1000)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/api/v2/spot/market/history-candles",
                            params={"symbol": self.pair(sym), "granularity": f"{int(self.cfg.bar_seconds // 60)}min",
                                    "endTime": end_ms, "limit": self.PER_REQUEST})
            if r.status_code == 429:
                await asyncio.sleep(1.5)
                continue
            d = r.json()
            if str(d.get("code")) != "00000":
                if str(d.get("code")) == "40034":           # "Parameter NOPEUSDT does not exist"
                    self._unsupported.add(sym)
                    return []
                self.note = f"candles {r.status_code}: {str(d.get('msg'))[:60]}"
                return out
            rows = d.get("data") or []
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, int(k[0]) / 1000.0, k[1], k[2], k[3], k[4], k[5])
                if t and start <= t.ts < end:
                    out.append(t)
            end_ms = int(rows[0][0]) - 1
            if end_ms <= int(start * 1000) or len(rows) < self.PER_REQUEST:
                break
            await asyncio.sleep(0.15)                       # public limit is 20 a second
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
                    self.note = f"not listed on Bitget: {', '.join(base_of(s) for s in missing[:8])}"
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
            args = [{"instType": "SPOT", "channel": ch, "instId": p} for p in self._pairs for ch in ("trade", "ticker")]
            for k in range(0, len(args), 50):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 50]}))
            self.note = f"streaming trades for {len(self._pairs)} pairs"

        async def ping(ws):
            await ws.send("ping")                          # the reply is the bare string "pong"

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=30.0,
                            decode=lambda raw: None if raw == "pong" else json.loads(raw))

    def _handle(self, m: dict, emit) -> None:
        arg = m.get("arg") or {}
        sym = self._pairs.get(arg.get("instId"))
        if m.get("event") == "error":
            self.note = f"ws: {str(m.get('msg'))[:80]}"
            if sym and str(m.get("code")) == "30001":      # "... doesn't exist"
                self._unsupported.add(sym)
            return
        if not sym:
            return
        ch = arg.get("channel")
        if ch == "trade" and m.get("action") == "update":
            for d in m.get("data") or []:
                t = trade_tick(sym, d.get("price"), d.get("size"), d.get("side") == "buy", int(d.get("ts") or 0) / 1000.0)
                if t:
                    self._emit(emit, t)
        elif ch == "ticker":
            for d in m.get("data") or []:
                t = quote_tick(sym, d.get("lastPr"), d.get("bidPr"), d.get("askPr"))
                if t:
                    self._emit(emit, t)
