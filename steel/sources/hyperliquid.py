"""Hyperliquid perpetuals: trades and the best bid/offer over the websocket, 5-minute candles by
range from the info endpoint. USDC-margined, no key, on by default: the one perp venue in the
default set."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick, unscale

SCALED = {"PEPE", "SHIB", "BONK", "LUNC", "FLOKI", "DOGS", "NEIRO"}     # listed as kPEPE...: a price per thousand tokens


# Hyperliquid ----------------------------------------------------------------------

class HyperliquidSource(Source):
    """Hyperliquid's perps (USDC collateral, funding hourly). The info endpoint is one POST with a
    typed body: `meta` lists the universe (bare coin names; `kPEPE` and friends quote a thousand
    tokens, `isDelisted` marks the dead ones), `candleSnapshot` serves up to 5000 candles by
    millisecond range as `{t open-ms, o, h, l, c, v base}`; an unknown coin answers 500. REST
    weight is 1200 a minute per IP and a candle request costs 20 plus one per 60 candles, so
    candle requests run one at a time, paced to that budget. The websocket streams `trades`
    (`side` is the taker's: `B` printed at the ask, `A` at the bid, checked against the book) and
    `bbo` (`[bid, ask]`, null when a side is empty); `{"method":"ping"}` keeps it open."""

    id = "hyperliquid"
    name = "Hyperliquid"
    mechanism = "websocket"
    quote = "USDC"
    venue = "perp"
    default_on = True
    URL = "https://app.hyperliquid.xyz"
    priority = 70
    fresh_after = 150.0
    REST = "https://api.hyperliquid.xyz/info"
    WS = "wss://api.hyperliquid.xyz/ws"
    PER_REQUEST = 5000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.coins: dict[str, str] = {}            # canonical -> venue coin, once the universe is listed
        self.coins_at = 0.0
        self._rev: dict[str, str] = {}             # venue coin -> canonical, for the stream
        self._sem = asyncio.Semaphore(1)

    @staticmethod
    def coin(sym: str) -> str:
        base = base_of(sym)
        return f"k{base}" if base in SCALED else base

    @staticmethod
    def canonical(coin: str) -> str:
        return f"{unscale(coin)[0]}-USD"

    def _coin(self, sym: str) -> str:
        return self.coins.get(sym) or self.coin(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.coins if self.coins else self.supports(sym)

    async def list_coins(self, c=None, max_age: float = 3600.0) -> dict:
        if self.coins and time.time() - self.coins_at < max_age:
            return self.coins
        own = c is None
        c = c or client(timeout=20)
        try:
            r = await c.post(self.REST, json={"type": "meta"})
            if r.status_code == 200:
                coins = {}
                for u in r.json().get("universe") or []:
                    if u.get("name") and not u.get("isDelisted"):
                        coins.setdefault(self.canonical(u["name"]), u["name"])
                if coins:
                    self.coins, self.coins_at = coins, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"meta: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.coins

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """One snapshot of [start, end), oldest first; sleeps the request's weight off afterwards."""
        coin = self._coin(sym)
        factor = unscale(coin)[1]
        req = {"coin": coin, "interval": f"{int(self.cfg.bar_seconds // 60)}m", "startTime": int(start * 1000), "endTime": int(end * 1000)}
        async with self._sem:
            for attempt in range(3):
                r = await c.post(self.REST, json={"type": "candleSnapshot", "req": req})
                if r.status_code == 429:
                    await asyncio.sleep(3.0 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    self.note = f"{base_of(sym)}: candles {r.status_code}"
                    return []
                rows = r.json() or []
                out = []
                for k in rows:
                    t = _candle_tick(sym, int(k["t"]) / 1000.0, float(k["o"]) / factor, float(k["h"]) / factor,
                                     float(k["l"]) / factor, float(k["c"]) / factor, float(k["v"]) * factor)
                    if t and start <= t.ts < end:
                        out.append(t)
                out.sort(key=lambda t: t.ts)
                await asyncio.sleep((20 + len(rows) // 60) / 20.0)
                return out
        return []

    async def _range(self, c, sym: str, cutoff: float, until: float) -> list[Tick]:
        span = self.PER_REQUEST * float(self.cfg.bar_seconds)
        pages, lo = [], cutoff
        while lo < until:
            pages.append((lo, min(until, lo + span)))
            lo += span
        pages = pages[-int(self.cfg.backfill_pages):] if self.cfg.backfill_pages else pages
        out: list[Tick] = []
        for a, b in pages:
            out.extend(await self._candles(c, sym, a, b))
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
            await self.list_coins(c)
            if self.coins:
                missing = [s for s in syms if s not in self.coins]
                syms = [s for s in syms if s in self.coins]
                if missing:
                    self.note = f"not listed on Hyperliquid: {', '.join(base_of(s) for s in missing[:8])}"
            return await gather_symbols(syms, lambda s: self._range(c, s, cutoff, until), limit=2, note=self._noted)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_coins()
        self._rev = {self._coin(s): s for s in self.symbols if not self.coins or s in self.coins}
        if not self._rev:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            for coin in self._rev:
                for kind in ("trades", "bbo") + (("l2Book",) if self.books else ()):
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": kind, "coin": coin}}))
            self.note = f"streaming trades for {len(self._rev)} perps"

        async def ping(ws):
            await ws.send(json.dumps({"method": "ping"}))

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=30.0)

    def _handle(self, m: dict, emit) -> None:
        ch = m.get("channel")
        if ch == "trades":
            for d in m.get("data") or []:
                sym = self._rev.get(d.get("coin"))
                if not sym:
                    continue
                factor = unscale(d["coin"])[1]
                t = trade_tick(sym, float(d["px"]) / factor, float(d["sz"]) * factor, d.get("side") == "B",
                               int(d.get("time") or 0) / 1000.0 or None)
                if t:
                    self._emit(emit, t)
        elif ch == "bbo":
            d = m.get("data") or {}
            sym = self._rev.get(d.get("coin"))
            if not sym:
                return
            factor = unscale(d["coin"])[1]
            bid, ask = (list(d.get("bbo") or []) + [None, None])[:2]
            t = quote_tick(sym, None, float(bid["px"]) / factor if bid else None, float(ask["px"]) / factor if ask else None,
                           int(d.get("time") or 0) / 1000.0 or None)
            if t:
                self._emit(emit, t)
        elif ch == "l2Book":
            d = m.get("data") or {}
            sym = self._rev.get(d.get("coin"))
            if not sym:
                return
            factor = unscale(d["coin"])[1]
            lv = d.get("levels") or [[], []]
            bids = [(float(x["px"]) / factor, float(x["sz"]) * factor) for x in lv[0]]
            asks = [(float(x["px"]) / factor, float(x["sz"]) * factor) for x in lv[1]]
            self._depth(sym, bids, asks, True, int(d.get("time") or 0) / 1000.0 or None)     # every push is the whole top of the book
        elif ch == "error":
            self.note = f"ws: {str(m.get('data'))[:80]}"
