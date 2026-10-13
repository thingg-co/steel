"""Deribit perpetuals: the inverse BTC/ETH-PERPETUAL and the linear BASE_USDC-PERPETUAL contracts.
A JSON-RPC websocket for trades and ticker, TradingView chart data for candles."""
from __future__ import annotations

import asyncio
import json
import time

from .base import Source, Tick, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick, unscale

INVERSE = ("BTC", "ETH")                        # coins whose most liquid perp is the inverse USD contract


def _div(v, scale: float):
    return float(v) / scale if v not in (None, "", 0, "0") else None


# Deribit --------------------------------------------------------------------------

class DeribitSource(Source):
    """Deribit public data for its perpetuals. BTC and ETH trade the inverse USD contracts
    (BTC-PERPETUAL: a trade's `amount` is USD notional, so base = amount / price); every other
    coin trades the linear BASE_USDC-PERPETUAL (amount in the coin), small-price names as
    1000PEPE_USDC-PERPETUAL (`unscale`). The websocket is JSON-RPC: `trades.X.100ms` (`direction`
    is the taker's) and `ticker.X.100ms` (best bid/ask, last, mark, funding), with a 30 s
    heartbeat whose `test_request` must be answered with public/test or the server drops the
    socket. get_tradingview_chart_data serves candles (`ticks` in ms, `volume` in base, `cost` in
    quote) but only the newest ~5,000 of a span, so a long range pages backward. A 400 names an
    unknown instrument. No key; off by default."""

    id = "deribit"
    name = "Deribit"
    mechanism = "websocket"
    quote = "USDC"
    venue = "perp"
    URL = "https://www.deribit.com"
    priority = 82
    fresh_after = 150.0
    REST = "https://www.deribit.com/api/v2"
    WS = "wss://www.deribit.com/ws/api/v2"
    PER_REQUEST = 5000

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: dict[str, str] = {}      # canonical -> instrument, once listed
        self.products_at = 0.0
        self._scale: dict[str, float] = {}      # instrument -> 1000 for 1000PEPE-style names
        self._insts: dict[str, str] = {}

    @staticmethod
    def inst(sym: str) -> str:
        b = base_of(sym)
        return f"{b}-PERPETUAL" if b in INVERSE else f"{b}_USDC-PERPETUAL"

    @staticmethod
    def is_inverse(inst: str) -> bool:
        return "_" not in str(inst or "")

    def _inst(self, sym: str) -> str:
        return self.products.get(sym) or self.inst(sym)

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    def _note(self, text: str) -> None:
        self.note = text

    async def list_products(self, c=None, max_age: float = 3600.0) -> dict[str, str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/public/get_instruments", params={"currency": "any", "kind": "future"})
            if r.status_code == 200:
                prods, scale = {}, {}
                for i in r.json().get("result") or []:
                    name = str(i.get("instrument_name") or "")
                    if i.get("settlement_period") != "perpetual" or not i.get("is_active", True):
                        continue
                    if not self.is_inverse(name) and "_USDC-" not in name and "_USDT-" not in name:
                        continue
                    base, factor = unscale(name.split("_")[0].split("-")[0])
                    sym = f"{base}-USD"
                    if self.is_inverse(name) or sym not in prods:        # the inverse wins when both exist (BTC, ETH)
                        prods[sym] = name
                    scale[name] = factor
                self.products, self._scale = prods, scale
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"instruments: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _chart(self, c, sym: str, start: float, end: float) -> list[Tick]:
        """[start, end) oldest first; a call returns the newest ~5,000 candles of its span, so
        move `end_timestamp` back to just before the oldest tick while the page came back full."""
        inst = self._inst(sym)
        scale = self._scale.get(inst, 1.0)
        out: dict[float, Tick] = {}
        hi, tries = int(end * 1000), 0
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/public/get_tradingview_chart_data",
                            params={"instrument_name": inst, "start_timestamp": int(start * 1000), "end_timestamp": hi,
                                    "resolution": str(int(self.cfg.bar_seconds) // 60)})
            if r.status_code == 429 and tries < 3:
                tries += 1
                await asyncio.sleep(1.5 * tries)
                continue
            if r.status_code == 400:
                self._unsupported.add(sym)
                return []
            if r.status_code != 200:
                self.note = f"chart {r.status_code}"
                break
            d = r.json().get("result") or {}
            ticks = d.get("ticks") or []
            for i, ms in enumerate(ticks):
                t = _candle_tick(sym, ms / 1000.0, _div(d["open"][i], scale), _div(d["high"][i], scale), _div(d["low"][i], scale),
                                 _div(d["close"][i], scale), float(d["volume"][i] or 0) * scale)
                if t and start <= t.ts < end:
                    out.setdefault(t.ts, t)
            if len(ticks) < self.PER_REQUEST or ticks[0] <= start * 1000:
                break
            hi = int(ticks[0]) - 1
            await asyncio.sleep(0.2)
        return [out[k] for k in sorted(out)]

    async def backfill(self, symbols, cutoff):
        return await self.backfill_range(symbols, cutoff, time.time())

    async def backfill_range(self, symbols, cutoff, until):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        async with client() as c:
            await self.list_products(c)
            if self.products:
                syms = [s for s in syms if self.knows(s)]
            return await gather_symbols(syms, lambda s: self._chart(c, s, cutoff, until), limit=4, note=self._note)

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        self._insts = {self._inst(s): s for s in self.symbols if self.knows(s)}
        if not self._insts:
            self.note = "none of the active symbols is listed"
            return

        async def subscribe(ws):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/set_heartbeat", "params": {"interval": 30}}))
            chans = [f"{k}.{i}.100ms" for i in self._insts for k in ("trades", "ticker")]
            for k in range(0, len(chans), 100):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "public/subscribe", "params": {"channels": chans[k:k + 100]}}))
            self.note = f"streaming trades for {len(self._insts)} contracts"

        await self._ws_loop(emit, self.WS, subscribe, self._handle, reply=self._reply)

    @staticmethod
    def _reply(m: dict) -> str | None:
        """The heartbeat's test_request is answered with public/test, or Deribit closes the socket."""
        if m.get("method") == "heartbeat" and (m.get("params") or {}).get("type") == "test_request":
            return json.dumps({"jsonrpc": "2.0", "id": 0, "method": "public/test", "params": {}})
        return None

    def _handle(self, m: dict, emit) -> None:
        if m.get("method") != "subscription":
            if m.get("error"):
                self.note = f"ws: {str((m.get('error') or {}).get('message'))[:80]}"
            return
        p = m.get("params") or {}
        kind, _, rest = str(p.get("channel") or "").partition(".")
        inst = rest.rsplit(".", 1)[0]
        sym = self._insts.get(inst)
        if not sym:
            return
        scale = self._scale.get(inst, 1.0)
        data = p.get("data")
        if kind == "trades":
            for d in data or []:
                px, amt = float(d.get("price") or 0), float(d.get("amount") or 0)
                qty = amt / px if self.is_inverse(inst) and px > 0 else amt * scale      # inverse: amount is USD notional
                t = trade_tick(sym, px / scale, qty, d.get("direction") == "buy", int(d.get("timestamp") or 0) / 1000.0 or None)
                if t:
                    self._emit(emit, t)
        elif kind == "ticker" and isinstance(data, dict):
            t = quote_tick(sym, _div(data.get("last_price"), scale), _div(data.get("best_bid_price"), scale),
                           _div(data.get("best_ask_price"), scale), ts=int(data.get("timestamp") or 0) / 1000.0 or None)
            if t:
                self._emit(emit, t)
