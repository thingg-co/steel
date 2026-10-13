"""Crypto.com Exchange spot: trades and tickers over the market-data websocket, candles by
range (300 a request). USD-quoted where listed, else USDT; no key, off by default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# Crypto.com -----------------------------------------------------------------------

class CryptoComSource(Source):
    """Crypto.com Exchange public data. Instruments are `BTC_USD` (preferred) or `BTC_USDT`;
    `s` on a trade is the taker's side (BUY/SELL). The websocket's rate limit is pro-rated on
    the calendar second the connection opened, so `subscribe` waits a second first (the docs'
    advice); the server's `public/heartbeat` must be answered with `public/respond-heartbeat`
    within 5 s. The subscribe acknowledgement carries the last 50 trades (history: not emitted);
    pushes come with `id` -1. `get-candlestick` serves the latest `count` (300 at most) candles
    inside [start_ts, end_ts], so pages walk backwards from `until`."""

    id = "cryptocom"
    name = "Crypto.com"
    mechanism = "websocket"
    venue = "spot"
    URL = "https://crypto.com/exchange"
    priority = 62
    fresh_after = 150.0
    REST = "https://api.crypto.com/exchange/v1"
    WS = "wss://stream.crypto.com/exchange/v1/market"
    PER_REQUEST = 300
    TIMEFRAMES = {60: "M1", 300: "M5", 900: "M15", 1800: "M30", 3600: "H1"}

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.names: dict[str, str] = {}                  # canonical -> instrument, once listed
        self.names_at = 0.0
        self._insts: dict[str, str] = {}

    @staticmethod
    def inst(sym: str) -> str:
        return base_of(sym) + "_USD"

    def inst_for(self, sym: str) -> str:
        return self.names.get(sym) or self.inst(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.names if self.names else self.supports(sym)

    async def list_instruments(self, c=None, max_age: float = 3600.0) -> dict:
        """Tradable `CCY_PAIR` instruments quoted in USD or USDT (USD wins), cached an hour."""
        if self.names and time.time() - self.names_at < max_age:
            return self.names
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/public/get-instruments")
            if r.status_code == 200:
                names: dict[str, str] = {}
                for p in (r.json().get("result") or {}).get("data") or []:
                    if p.get("inst_type") != "CCY_PAIR" or not p.get("tradable") or p.get("quote_ccy") not in ("USD", "USDT"):
                        continue
                    sym = f"{str(p.get('base_ccy')).upper()}-USD"
                    if p.get("quote_ccy") == "USD" or sym not in names:
                        names[sym] = str(p.get("symbol"))
                self.names, self.names_at = names, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"instruments: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.names

    async def _candles(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        """Rows {o, h, l, c, v (base), t (bucket start, ms)} oldest first; the newest 300 of the
        window per page, so the window's end walks back a page at a time."""
        out: list[Tick] = []
        hi = int(until * 1000) - 1
        tf = self.TIMEFRAMES.get(int(self.cfg.bar_seconds), "M5")
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = None
            for attempt in range(3):
                r = await c.get(f"{self.REST}/public/get-candlestick",
                                params={"instrument_name": self.inst_for(sym), "timeframe": tf, "count": self.PER_REQUEST,
                                        "start_ts": int(cutoff * 1000), "end_ts": hi})
                if r.status_code != 429:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))
            if r.status_code == 400:                          # code 40004: Invalid instrument_name
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                return out
            rows = (r.json().get("result") or {}).get("data") or []
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, int(k["t"]) / 1000.0, k.get("o"), k.get("h"), k.get("l"), k.get("c"), k.get("v"))
                if t and cutoff <= t.ts < until:
                    out.append(t)
            lo = min(int(k["t"]) for k in rows)
            if len(rows) < self.PER_REQUEST or lo <= int(cutoff * 1000):
                break
            hi = lo - 1
            await asyncio.sleep(0.1)
        out.sort(key=lambda t: t.ts)
        return out

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client() as c:
            await self.list_instruments(c)
            if self.names:
                syms = [s for s in syms if s in self.names]
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4,
                                        note=lambda t: setattr(self, "note", t))

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_instruments()
        syms = [s for s in self.symbols if not self.names or s in self.names]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._insts = {self.inst_for(s): s for s in syms}

        async def subscribe(ws):
            await asyncio.sleep(1.0)                          # the docs: let the connection's rate window start
            chans = [f"{ch}.{i}" for i in self._insts for ch in ("trade", "ticker")]
            for k in range(0, len(chans), 100):
                await ws.send(json.dumps({"id": k // 100 + 1, "method": "subscribe", "nonce": int(time.time() * 1000),
                                          "params": {"channels": chans[k:k + 100]}}))
            self.note = f"streaming trades for {len(self._insts)} instruments"

        await self._ws_loop(emit, self.WS, subscribe, self._handle, reply=self.reply)

    @staticmethod
    def reply(m):
        if isinstance(m, dict) and m.get("method") == "public/heartbeat":
            return json.dumps({"id": m["id"], "method": "public/respond-heartbeat"})
        return None

    def _handle(self, m: dict, emit) -> None:
        if m.get("method") != "subscribe":
            return
        if str(m.get("code") or "0") != "0":
            self.note = f"ws: {str(m.get('message') or m.get('code'))[:80]}"
            return
        res = m.get("result") or {}
        sym = self._insts.get(res.get("instrument_name"))
        if not sym:
            return
        ch, push = res.get("channel"), m.get("id") == -1       # the ack (id of our request) replays the last 50 trades
        for d in res.get("data") or []:
            ts = float(d.get("t") or 0) / 1000.0
            if ch == "trade" and push:
                t = trade_tick(sym, d.get("p"), d.get("q"), str(d.get("s")).upper() == "BUY", ts)
            elif ch == "ticker":
                t = quote_tick(sym, d.get("a"), d.get("b"), d.get("k"), ts)
            else:
                t = None
            if t:
                self._emit(emit, t)
