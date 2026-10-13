"""BloFin USDT perpetuals: trades and tickers over the websocket, candles by range. An OKX-shaped
API, no key, off by default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick, unscale


# BloFin ---------------------------------------------------------------------------

class BloFinSource(Source):
    """BloFin's USDT-margined perps behind an OKX-shaped API. `/market/instruments?instType=SWAP`
    lists them: `1000PEPE-USDT` and `1000000MOG-USDT` quote a thousand or a million tokens, and
    every size (trades, `vol`) is in contracts of `contractValue` base units, so the list is
    needed before a trade can be sized. Candles page newest first by `after` (records older than
    the timestamp) as `[ts, o, h, l, c, vol, volCurrency, volCurrencyQuote, confirm]`, with
    `volCurrency` the base volume (up to 1440 a page); an unknown name answers 200 with code
    152002. The websocket takes OKX's subscribe shape, the trade `side` is the taker's, and wants
    the bare string `ping` under 30 s (`pong` comes back bare too). 500 requests a minute per IP."""

    id = "blofin"
    name = "BloFin"
    mechanism = "websocket"
    quote = "USDT"
    venue = "perp"
    URL = "https://blofin.com"
    priority = 72
    fresh_after = 150.0
    REST = "https://openapi.blofin.com/api/v1"
    WS = "wss://openapi.blofin.com/ws/public"
    PER_REQUEST = 300

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.insts: dict[str, tuple[str, float, float]] = {}   # canonical -> (instId, token factor, contract size in base)
        self.insts_at = 0.0
        self._pairs: dict[str, str] = {}

    @staticmethod
    def inst(sym: str) -> str:
        return base_of(sym) + "-USDT"

    def _inst(self, sym: str) -> tuple[str, float, float]:
        return self.insts.get(sym) or (self.inst(sym), 1.0, 1.0)

    def knows(self, sym: str) -> bool:
        return sym in self.insts if self.insts else self.supports(sym)

    async def list_instruments(self, c=None, max_age: float = 3600.0) -> dict:
        if self.insts and time.time() - self.insts_at < max_age:
            return self.insts
        own = c is None
        c = c or client(timeout=20)
        try:
            r = await c.get(f"{self.REST}/market/instruments", params={"instType": "SWAP"})
            if r.status_code == 200:
                insts = {}
                for p in r.json().get("data") or []:
                    if p.get("quoteCurrency") != "USDT" or p.get("state") != "live":
                        continue
                    base, factor = unscale(str(p.get("baseCurrency") or ""))
                    insts.setdefault(f"{base}-USD", (p["instId"], factor, float(p.get("contractValue") or 1.0)))
                if insts:
                    self.insts, self.insts_at = insts, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"instruments: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.insts

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        inst, factor, _ = self._inst(sym)
        out: list[Tick] = []
        after = int(end * 1000)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/market/candles", params={"instId": inst, "bar": "5m", "after": after, "limit": self.PER_REQUEST})
            if r.status_code == 429:
                await asyncio.sleep(2.0)
                continue
            if r.status_code != 200:
                self.note = f"candles {r.status_code}"
                return out
            d = r.json()
            if str(d.get("code")) != "0":
                self._unsupported.add(sym)
                return []
            rows = d.get("data") or []
            if not rows:
                break
            for k in rows:
                t = _candle_tick(sym, int(k[0]) / 1000.0, float(k[1]) / factor, float(k[2]) / factor, float(k[3]) / factor,
                                 float(k[4]) / factor, float(k[6]) * factor)
                if t and start <= t.ts < end:
                    out.append(t)
            after = int(rows[-1][0])
            if after <= int(start * 1000) or len(rows) < self.PER_REQUEST:
                break
            await asyncio.sleep(0.2)
        out.sort(key=lambda t: t.ts)
        return out

    def _noted(self, text: str) -> None:
        self.note = text

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client() as c:
            await self.list_instruments(c)
            if self.insts:
                syms = [s for s in syms if s in self.insts]
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=3, note=self._noted)

    async def run(self, emit):
        if not self.symbols:
            return
        while not await self.list_instruments():          # sizes are contracts: no list, no stream
            self.note = "waiting for the instrument list"
            await asyncio.sleep(60.0)
        self._pairs = {self.insts[s][0]: s for s in self.symbols if s in self.insts}
        if not self._pairs:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            args = [{"channel": ch, "instId": i} for i in self._pairs for ch in ("trades", "tickers")]
            for k in range(0, len(args), 50):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 50]}))
            self.note = f"streaming trades for {len(self._pairs)} perps"

        async def ping(ws):
            await ws.send("ping")

        def decode(raw):
            return None if raw == "pong" else json.loads(raw)

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=25.0, decode=decode)

    def _handle(self, m: dict, emit) -> None:
        ch = (m.get("arg") or {}).get("channel")
        if m.get("event") == "error":
            self.note = f"ws: {str(m.get('msg'))[:80]}"
            return
        for d in m.get("data") or []:
            sym = self._pairs.get(d.get("instId"))
            if not sym:
                continue
            _, factor, size = self._inst(sym)
            if ch == "trades":
                t = trade_tick(sym, float(d["price"]) / factor, float(d["size"]) * size * factor, d.get("side") == "buy",
                               int(d.get("ts") or 0) / 1000.0 or None)
            elif ch == "tickers":
                bid, ask = d.get("bidPrice"), d.get("askPrice")
                t = quote_tick(sym, float(d.get("last") or 0) / factor, float(bid) / factor if bid else None, float(ask) / factor if ask else None)
            else:
                continue
            if t:
                self._emit(emit, t)
