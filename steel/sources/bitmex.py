"""BitMEX perpetuals: trades and quotes over the websocket, bucketed trades by date range. No key,
off by default. The venue ceased trading on 2026-09-16; its history before the close still serves."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, _iso, _iso_ts, base_of, client, gather_symbols, quote_tick, trade_tick, unscale

ALIAS = {"XBT": "BTC"}
UNALIAS = {"BTC": "XBT"}


# BitMEX ---------------------------------------------------------------------------

class BitMEXSource(Source):
    """BitMEX perps. XBT is BTC; a name is the USDT-linear perp (`XBTUSDT`, `ETHUSDT`) when the
    active list has one open, else the USD-quoted inverse or quanto one (`XBTUSD`);
    `1000000MOGUSDT` quotes a million tokens. Trades carry `homeNotional` in base units and `side`
    is the taker's. The bucketed trades stamp each bucket's END (the 00:10 row holds the 00:05-00:10
    prints, checked against the raw tape), so 300 s come off, and buckets with no trades (BitMEX
    pads them with the last close) are skipped as Coinbase skips them: 1000 a page oldest first by
    `startTime`, 180 requests a minute unkeyed. The websocket's `partial` for `trade` replays
    recent prints and is skipped. BitMEX ceased all trading on 2026-09-16 (every perp is
    `Settled`), so the active list holds no perp and nothing is known or streamed unless it
    relists; the history before the close still pages."""

    id = "bitmex"
    name = "BitMEX"
    mechanism = "websocket"
    venue = "perp"
    URL = "https://www.bitmex.com"
    priority = 74
    fresh_after = 150.0
    REST = "https://www.bitmex.com/api/v1"
    WS = "wss://ws.bitmex.com/realtime"
    BUCKETED = "/trade/bucketed"                    # public bucketed trade history: a market-data read
    PER_REQUEST = 1000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.insts: dict[str, tuple[str, float]] = {}      # canonical -> (venue symbol, token factor)
        self.insts_at = 0.0
        self._pairs: dict[str, str] = {}

    @staticmethod
    def pair(sym: str) -> str:
        base = base_of(sym)
        return UNALIAS.get(base, base) + "USDT"

    @staticmethod
    def canonical(symbol: str) -> str | None:
        """`XBTUSDT` / `XBTUSD` -> BTC-USD; None for another quote."""
        s = str(symbol or "")
        for q in ("USDT", "USD"):
            if s.endswith(q) and len(s) > len(q):
                base = unscale(s[:-len(q)])[0]
                return f"{ALIAS.get(base, base)}-USD"
        return None

    def _inst(self, sym: str) -> tuple[str, float]:
        return self.insts.get(sym) or (self.pair(sym), 1.0)

    def knows(self, sym: str) -> bool:
        return sym in self.insts if self.insts_at else self.supports(sym)

    async def list_instruments(self, c=None, max_age: float = 3600.0) -> dict:
        """Open perps, the USDT-linear one preferred for a base; an empty list is a true answer."""
        if self.insts_at and time.time() - self.insts_at < max_age:
            return self.insts
        own = c is None
        c = c or client(timeout=20)
        try:
            r = await c.get(f"{self.REST}/instrument/active")
            if r.status_code == 200:
                insts = {}
                rows = [p for p in r.json() if p.get("typ") == "FFWCSX" and p.get("state") == "Open"]
                for p in sorted(rows, key=lambda p: p.get("quoteCurrency") != "USDT"):
                    sym = self.canonical(p.get("symbol"))
                    if sym:
                        insts.setdefault(sym, (p["symbol"], unscale(p["symbol"][:-4] if p["symbol"].endswith("USDT") else p["symbol"][:-3])[1]))
                self.insts, self.insts_at = insts, time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"instruments: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.insts

    async def _buckets(self, c, sym: str, start: float, end: float, symbol: str | None = None, factor: float = 1.0) -> list[Tick]:
        """Closed 5-minute buckets covering [start, end), oldest first, restamped to their start."""
        symbol = symbol or self._inst(sym)[0]
        bar = float(self.cfg.bar_seconds)
        out: list[Tick] = []
        lo = start
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(self.REST + self.BUCKETED, params={"binSize": "5m", "symbol": symbol, "partial": "false", "count": self.PER_REQUEST,
                                                                    "startTime": _iso(lo + bar), "endTime": _iso(end + bar)})
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("retry-after") or 2.0))
                continue
            if r.status_code != 200:
                self.note = f"bucketed {r.status_code}"
                return out
            rows = r.json() or []
            for k in rows:
                t0 = _iso_ts(k["timestamp"]) - bar
                if int(k.get("trades") or 0) <= 0 or not (start <= t0 < end):
                    continue
                t = _candle_tick(sym, t0, float(k["open"]) / factor, float(k["high"]) / factor, float(k["low"]) / factor,
                                 float(k["close"]) / factor, float(k.get("homeNotional") or 0.0) * factor)
                if t:
                    out.append(t)
            if len(rows) < self.PER_REQUEST:
                break
            lo = _iso_ts(rows[-1]["timestamp"])
            await asyncio.sleep(0.4)
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
            syms = [s for s in syms if s in self.insts]
            if not syms:
                self.note = "no open perp lists any of these names"
                return []
            return await gather_symbols(syms, lambda s: self._buckets(c, s, cutoff, until, *self.insts[s]), limit=2, note=self._noted)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_instruments()
        self._pairs = {self.insts[s][0]: s for s in self.symbols if s in self.insts}
        if not self._pairs:
            self.note = "no open perp lists any of the active symbols"
            return

        async def subscribe(ws):
            args = [f"{t}:{p}" for p in self._pairs for t in ("trade", "quote")]
            for k in range(0, len(args), 40):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 40]}))
            self.note = f"streaming trades for {len(self._pairs)} perps"

        async def ping(ws):
            await ws.send("ping")

        def decode(raw):
            return None if raw == "pong" else json.loads(raw)

        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=25.0, decode=decode)

    def _handle(self, m: dict, emit) -> None:
        table = m.get("table")
        if m.get("error"):
            self.note = f"ws: {str(m['error'])[:80]}"
            return
        if table == "trade" and m.get("action") == "partial":
            return
        for d in m.get("data") or []:
            sym = self._pairs.get(d.get("symbol"))
            if not sym:
                continue
            factor = self.insts.get(sym, ("", 1.0))[1]
            ts = _iso_ts(d["timestamp"]) if d.get("timestamp") else None
            if table == "trade":
                t = trade_tick(sym, float(d["price"]) / factor, float(d.get("homeNotional") or 0.0) * factor, d.get("side") == "Buy", ts)
            elif table == "quote":
                bid, ask = d.get("bidPrice"), d.get("askPrice")
                t = quote_tick(sym, None, float(bid) / factor if bid else None, float(ask) / factor if ask else None, ts)
            else:
                continue
            if t:
                self._emit(emit, t)
