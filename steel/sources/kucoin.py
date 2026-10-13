"""KuCoin spot against USDT: match and ticker topics over a token websocket, candles by range."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import websockets

from .base import Source, Tick, UA, _candle_tick, base_of, client, gather_symbols, quote_tick, trade_tick


# KuCoin ---------------------------------------------------------------------------

class KuCoinSource(Source):
    """KuCoin spot public data against USDT. The stream needs a per-connection token from
    `POST /api/v1/bullet-public` (no key), so `run` keeps its own reconnect loop and fetches a
    fresh one each time; `/market/match` reports the taker's side with a NANOSECOND `time`
    string, `/market/ticker` the top of book. Candles come newest first as [time(s), open,
    CLOSE, HIGH, LOW, volume, turnover] (1,500 a request, paged backwards by `endAt`); an
    unknown pair is HTTP 200 with code 400100. Off by default."""

    id = "kucoin"
    name = "KuCoin"
    mechanism = "websocket"
    quote = "USDT"
    venue = "spot"
    URL = "https://www.kucoin.com"
    priority = 54
    fresh_after = 150.0
    REST = "https://api.kucoin.com"
    PER_REQUEST = 1500

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.products: set[str] = set()
        self.products_at = 0.0
        self._pairs = {self.pair(s): s for s in self.symbols}

    @staticmethod
    def pair(sym: str) -> str:
        return base_of(sym) + "-USDT"

    def knows(self, sym: str) -> bool:
        return sym in self.products if self.products else self.supports(sym)

    async def list_products(self, c=None, max_age: float = 3600.0) -> set[str]:
        if self.products and time.time() - self.products_at < max_age:
            return self.products
        own = c is None
        c = c or client(20)
        try:
            r = await c.get(f"{self.REST}/api/v2/symbols")
            if r.status_code == 200:
                self.products = {f"{p['baseCurrency']}-USD" for p in r.json().get("data") or []
                                 if p.get("quoteCurrency") == "USDT" and p.get("enableTrading") and p.get("symbol") == f"{p['baseCurrency']}-USDT"}
                self.products_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.note = f"symbols: {type(e).__name__}"
        finally:
            if own:
                await c.aclose()
        return self.products

    async def _candles(self, c, sym: str, start: float, end: float) -> list[Tick]:
        out: list[Tick] = []
        hi = int(end)
        for _ in range(int(self.cfg.backfill_pages) or 40):
            r = await c.get(f"{self.REST}/api/v1/market/candles",
                            params={"type": f"{int(self.cfg.bar_seconds // 60)}min", "symbol": self.pair(sym), "startAt": int(start), "endAt": hi})
            if r.status_code == 429:
                await asyncio.sleep(1.5)
                continue
            d = r.json()
            if str(d.get("code")) != "200000":
                if str(d.get("code")) == "400100":          # "Unsupported trading pair."
                    self._unsupported.add(sym)
                    return []
                self.note = f"candles {r.status_code}: {str(d.get('msg'))[:60]}"
                return out
            rows = d.get("data") or []
            if not rows:
                break
            for k in rows:                                  # newest first: [t, open, close, high, low, vol, turnover]
                t = _candle_tick(sym, int(k[0]), k[1], k[3], k[4], k[2], k[5])
                if t and start <= t.ts < end:
                    out.append(t)
            hi = int(rows[-1][0]) - 1
            if hi <= int(start) or len(rows) < self.PER_REQUEST:
                break
            await asyncio.sleep(0.2)
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
                    self.note = f"not listed on KuCoin: {', '.join(base_of(s) for s in missing[:8])}"
            return await gather_symbols(syms, lambda s: self._candles(c, s, cutoff, until), limit=4,
                                        note=lambda text: setattr(self, "note", text))

    async def _bullet(self) -> tuple[str, float]:
        """The stream URL for one connection (token and endpoint from bullet-public) and the
        ping interval the server asks for, in seconds."""
        async with client(20) as c:
            r = await c.post(f"{self.REST}/api/v1/bullet-public")
            d = (r.json() if r.status_code == 200 else {}).get("data") or {}
        srv = (d.get("instanceServers") or [{}])[0]
        if not d.get("token") or not srv.get("endpoint"):
            raise RuntimeError(f"bullet-public {r.status_code}")
        return f"{srv['endpoint']}?token={d['token']}&connectId={uuid.uuid4().hex}", float(srv.get("pingInterval") or 18000) / 1000.0

    async def _stream(self, emit, url: str, every: float) -> None:
        """One connection: subscribe (100 symbols a topic), read until it drops. Raises on a
        failure; `run` reconnects with a fresh token."""
        async with websockets.connect(url, ping_interval=20, max_queue=8192, open_timeout=20, user_agent_header=UA) as ws:
            pairs = list(self._pairs)
            for k in range(0, len(pairs), 100):
                for ch in ("match", "ticker"):
                    await ws.send(json.dumps({"id": uuid.uuid4().hex, "type": "subscribe", "topic": f"/market/{ch}:" + ",".join(pairs[k:k + 100]),
                                              "privateChannel": False, "response": True}))
            self.note = f"streaming trades for {len(pairs)} pairs"
            pinger = asyncio.create_task(self._pinger(ws, lambda w: w.send(json.dumps({"id": uuid.uuid4().hex, "type": "ping"})), every))
            try:
                async for raw in ws:
                    try:
                        self._handle(json.loads(raw), emit)
                    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                        continue
            finally:
                pinger.cancel()

    async def run(self, emit):
        if not self.symbols:
            return
        await self.list_products()
        syms = [s for s in self.symbols if not self.products or s in self.products]
        if not syms:
            self.note = "none of the active symbols is listed"
            return
        self._pairs = {self.pair(s): s for s in syms}
        backoff = 1.0
        while True:
            t0 = time.time()
            try:
                url, every = await self._bullet()
                await self._stream(emit, url, every)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.note = f"ws {type(e).__name__}; reconnecting"
            backoff = 1.0 if time.time() - t0 > 60 else min(backoff * 2, 30.0)
            await asyncio.sleep(backoff)

    def _handle(self, m: dict, emit) -> None:
        if m.get("type") != "message":
            if m.get("type") == "error":
                self.note = f"ws: {str(m.get('data'))[:80]}"
            return
        topic, d = str(m.get("topic") or ""), m.get("data") or {}
        if topic.startswith("/market/match:"):
            sym = self._pairs.get(d.get("symbol"))
            t = trade_tick(sym, d.get("price"), d.get("size"), d.get("side") == "buy", int(d.get("time") or 0) / 1e9) if sym else None
        elif topic.startswith("/market/ticker:"):
            sym = self._pairs.get(topic.split(":", 1)[1])
            t = quote_tick(sym, d.get("price"), d.get("bestBid"), d.get("bestAsk")) if sym else None
        else:
            return
        if t:
            self._emit(emit, t)
