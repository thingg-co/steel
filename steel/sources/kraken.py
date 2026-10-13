"""Kraken spot: the v2 websocket and 720 candles of OHLC. USD-quoted, no key."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .base import Source, Tick, UA, _candle_tick, _iso_ts


# Kraken ---------------------------------------------------------------------------

class KrakenSource(Source):
    """Kraken public data: the v2 websocket (trades and ticker) and 5-minute OHLC, which only
    reaches 720 candles back, so Kraken serves live failover, not deep history. No key."""

    id = "kraken"
    name = "Kraken"
    mechanism = "websocket"
    default_on = True
    URL = "https://www.kraken.com"
    priority = 20
    fresh_after = 150.0
    REST = "https://api.kraken.com/0/public"
    WS = "wss://ws.kraken.com/v2"

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.pairs: dict[str, tuple[str, str]] = {}      # canonical -> (REST altname, ws v2 symbol)
        self.pairs_at = 0.0

    def knows(self, sym: str) -> bool:
        return sym in self.pairs if self.pairs else self.supports(sym)

    async def list_pairs(self, c: httpx.AsyncClient | None = None, max_age: float = 3600.0) -> dict:
        if self.pairs and time.time() - self.pairs_at < max_age:
            return self.pairs
        own = c is None
        c = c or httpx.AsyncClient(timeout=20, headers={"User-Agent": UA})
        try:
            r = await c.get(f"{self.REST}/AssetPairs")
            if r.status_code == 200:
                pairs = {}
                keys = {}
                for key, p in (r.json().get("result") or {}).items():
                    ws = str(p.get("wsname") or "")
                    if not ws.endswith("/USD") or p.get("status") not in (None, "online"):
                        continue
                    base = ws.split("/")[0]
                    base = {"XBT": "BTC", "XDG": "DOGE"}.get(base, base)
                    pairs[f"{base}-USD"] = (str(p.get("altname") or key), f"{base}/USD")
                    keys[key] = f"{base}-USD"
                self.pairs = pairs
                self._keys = keys                          # the Ticker endpoint's pair keys (XXBTZUSD) -> canonical
                self.pairs_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"pairs: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.pairs

    async def volumes(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        """`/0/public/Ticker` with no pair lists every pair: 24-hour volume (`v[1]`, base units)
        times the 24-hour VWAP (`p[1]`) is the day's dollar volume of each USD pair."""
        out: dict[str, float] = {}
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            await self.list_pairs(c)
            keys = getattr(self, "_keys", {}) or {}
            if not keys:
                return out
            r = await c.get(f"{self.REST}/Ticker")
            if r.status_code != 200:
                self.note = f"Ticker {r.status_code}"
                return out
            for key, row in (r.json().get("result") or {}).items():
                sym = keys.get(key)
                if not sym:
                    continue
                try:
                    vol, vwap = float(row["v"][1]), float(row["p"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if vol > 0 and vwap > 0:
                    out[sym] = vol * vwap
        return out

    async def backfill(self, symbols, cutoff):
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        ticks: list[Tick] = []
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as c:
            await self.list_pairs(c)
            for sym in syms:
                alt = self.pairs.get(sym, (None, None))[0]
                if not alt:
                    continue
                try:
                    r = await c.get(f"{self.REST}/OHLC", params={"pair": alt, "interval": int(self.cfg.bar_seconds // 60),
                                                                 "since": int(cutoff)})
                    if r.status_code != 200:
                        self.note = f"OHLC {r.status_code}"
                        continue
                    res = r.json().get("result") or {}
                    rows = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), [])
                    for row in rows:                      # [time, open, high, low, close, vwap, volume, count]
                        if len(row) >= 7 and float(row[0]) >= cutoff:
                            t = _candle_tick(sym, row[0], row[1], row[2], row[3], row[4], row[6])
                            if t:
                                ticks.append(t)
                    await asyncio.sleep(0.6)              # public tier: about one call a second
                except Exception as e:  # noqa: BLE001
                    self.note = f"{sym}: {type(e).__name__}"
        return ticks

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_pairs()
        wsn = {self.pairs[s][1]: s for s in self.symbols if s in self.pairs}
        if not wsn:
            self.note = "none of the active symbols is listed"
            return
        self._wsn = wsn

        async def subscribe(ws):
            names = list(wsn)
            for k in range(0, len(names), 50):
                await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": names[k:k + 50], "snapshot": False}}))
                await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": names[k:k + 50]}}))
            self.note = f"streaming trades for {len(names)} pairs"

        await self._ws_loop(emit, self.WS, subscribe, self._handle)

    def _handle(self, m: dict, emit) -> None:
        ch = m.get("channel")
        if ch == "trade":
            for d in m.get("data") or []:
                sym = self._wsn.get(d.get("symbol"))
                px, qty = float(d.get("price") or 0), float(d.get("qty") or 0)
                if sym and px > 0 and qty > 0:
                    ts = _iso_ts(d["timestamp"]) if d.get("timestamp") else time.time()
                    self._emit(emit, Tick(sym, ts, px, qty, d.get("side") == "buy"))   # side is the taker's
        elif ch == "ticker":
            for d in m.get("data") or []:
                sym = self._wsn.get(d.get("symbol"))
                px = float(d.get("last") or 0)
                if sym and px > 0:
                    bid, ask = d.get("bid"), d.get("ask")
                    self._emit(emit, Tick(sym, time.time(), px, 0.0, None,
                                          float(bid) if bid else None, float(ask) if ask else None, quote=True))
        elif m.get("error"):
            self.note = f"ws: {str(m['error'])[:80]}"
