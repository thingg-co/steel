"""ApeX Omni perpetuals: trades and the instrument ticker over the websocket, klines by range. No
key, off by default."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick, unscale


# ApeX Omni ------------------------------------------------------------------------

class ApeXSource(Source):
    """ApeX Omni's USDT-settled perps on its zk rollup. `/v3/symbols` returns every config; the
    perps are `contractConfig.perpetualContract` (the prediction and stock lists beside it are
    not crypto), named `BTC-USDT` with a `crossSymbolName` `BTCUSDT` that klines and the stream
    use; `1000PEPE`, `1000SHIB`, `1000BONK` and `1000000MOG` quote a thousand or a million tokens.
    Klines take `start`/`end` in SECONDS (milliseconds return nothing), 200 a page oldest first,
    rows `{t ms, o, h, l, c, v base, tr}` under `data[symbol]` (an unknown name is an empty map).
    The websocket wants `{"op":"ping","args":[ms]}` every 15 s and a pong to the server's own
    `{"op":"ping"}`; `recentlyTrade.H.<sym>` opens with a snapshot of recent prints (skipped, so a
    reconnect never replays) then deltas with `S` the taker side; `instrumentInfo.H.<sym>` is a
    snapshot then sparse deltas carrying `lastPrice` and no bid/ask, so its quotes are price only."""

    id = "apex"
    name = "ApeX Omni"
    mechanism = "websocket"
    quote = "USDT"
    venue = "perp"
    URL = "https://omni.apex.exchange"
    priority = 80
    fresh_after = 150.0
    REST = "https://omni.apex.exchange/api/v3"
    WS = "wss://quote.omni.apex.exchange/realtime_public?v=2"
    PER_REQUEST = 200

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.insts: dict[str, tuple[str, float]] = {}      # canonical -> (crossSymbolName, token factor)
        self.insts_at = 0.0
        self._pairs: dict[str, str] = {}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "USDT"

    def _inst(self, sym: str) -> tuple[str, float]:
        return self.insts.get(sym) or (self.pair(sym), 1.0)

    def knows(self, sym: str) -> bool:
        return sym in self.insts if self.insts else self.supports(sym)

    async def list_instruments(self, c=None, max_age: float = 3600.0) -> dict:
        if self.insts and time.time() - self.insts_at < max_age:
            return self.insts
        own = c is None
        c = c or client(timeout=25)
        try:
            r = await c.get(f"{self.REST}/symbols")
            if r.status_code == 200:
                insts = {}
                for p in ((r.json().get("data") or {}).get("contractConfig") or {}).get("perpetualContract") or []:
                    name = str(p.get("crossSymbolName") or "")
                    if not p.get("enableTrade") or not name.endswith("USDT") or p.get("settleAssetId") not in (None, "USDT", "USDC"):
                        continue
                    base, factor = unscale(name[:-4])
                    insts.setdefault(f"{base}-USD", (name, factor))
                if insts:
                    self.insts, self.insts_at = insts, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"symbols: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.insts

    async def _klines(self, c, sym: str, start: float, end: float) -> list[Tick]:
        name, factor = self._inst(sym)
        bar = float(self.cfg.bar_seconds)
        out: list[Tick] = []
        lo = start
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/klines", params={"symbol": name, "interval": int(bar // 60), "start": int(lo), "end": int(end), "limit": self.PER_REQUEST})
            if r.status_code == 429:
                await asyncio.sleep(2.0)
                continue
            if r.status_code != 200:
                self.note = f"klines {r.status_code}"
                return out
            rows = ((r.json().get("data") or {}).get(name)) or []
            for k in rows:
                t = _candle_tick(sym, int(k["t"]) / 1000.0, float(k["o"]) / factor, float(k["h"]) / factor, float(k["l"]) / factor,
                                 float(k["c"]) / factor, float(k["v"]) * factor)
                if t and start <= t.ts < end:
                    out.append(t)
            if len(rows) < self.PER_REQUEST:
                break
            lo = int(rows[-1]["t"]) / 1000.0 + bar
            if lo >= end:
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
            return await gather_symbols(syms, lambda s: self._klines(c, s, cutoff, until), limit=3, note=self._noted)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_instruments()
        self._pairs = {self._inst(s)[0]: s for s in self.symbols if not self.insts or s in self.insts}
        if not self._pairs:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            args = [f"{t}.H.{p}" for p in self._pairs for t in ("recentlyTrade", "instrumentInfo")]
            for k in range(0, len(args), 20):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 20]}))
            self.note = f"streaming trades for {len(self._pairs)} perps"

        async def ping(ws):
            await ws.send(json.dumps({"op": "ping", "args": [str(int(time.time() * 1000))]}))

        def reply(m):
            return json.dumps({"op": "pong", "args": [str(int(time.time() * 1000))]}) if m.get("op") == "ping" else None

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=15.0, reply=reply)

    def _handle(self, m: dict, emit) -> None:
        kind, _, name = str(m.get("topic") or "").partition(".H.")
        sym = self._pairs.get(name)
        if not sym:
            if m.get("success") is False:
                self.note = f"ws: {str(m.get('ret_msg'))[:80]}"
            return
        factor = self._inst(sym)[1]
        if kind == "recentlyTrade":
            if m.get("type") == "snapshot":
                return
            for d in m.get("data") or []:
                t = trade_tick(sym, float(d["p"]) / factor, float(d["v"]) * factor, d.get("S") == "Buy", int(d.get("T") or 0) / 1000.0 or None)
                if t:
                    self._emit(emit, t)
        elif kind == "instrumentInfo":
            px = (m.get("data") or {}).get("lastPrice")
            t = quote_tick(sym, float(px) / factor) if px else None
            if t:
                self._emit(emit, t)
