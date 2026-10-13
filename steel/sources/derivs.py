"""The perp venues' derivatives streams: liquidations, open interest, funding, mark and
index per contract, for `steel/derivs.py`. Each is a `Source` for the websocket plumbing
(`_ws_loop`) and nothing else: it emits no ticks, builds no bars, and is never a price
provider. It calls `liq_sink(venue, pair, ts, px, usd, side)` and
`stats_sink(venue, pair, ts, **fields)`.

Conventions: a liquidation's side here is the forced order's effect on price, -1 when a long
was liquidated (a forced sell), +1 when a short was (a forced buy). Binance's `forceOrder`
side SELL means a long was liquidated; Bybit's `allLiquidation` side Buy means a long was
(the docs: "a Buy update means a long position has been liquidated"); OKX's
`liquidation-orders` posSide says which side closed. Open interest is kept in base units and
in USD (contracts x contract value x mark on OKX; Bybit sends openInterestValue; Binance's OI
is polled once a minute, its mark and funding stream). Small-price contracts (1000PEPE) go
through `unscale`."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .base import UA, Source, unscale


class DerivSource(Source):
    mechanism = "derivs"
    venue = "perp"
    kind = "derivs"
    priority = 900                                   # never a price provider
    default_on = True
    liq_sink = None
    stats_sink = None

    def supports(self, sym: str) -> bool:
        return "-" in sym

    def knows(self, sym: str) -> bool:
        return False                                 # never counts for the universe or the simulator rule

    def _liq(self, pair: str, ts: float, px: float, usd: float, side: int) -> None:
        if self.liq_sink is not None and usd > 0:
            self.ticks += 1
            self.last_tick = time.time()
            self.liq_sink(self.id, pair, ts, px, usd, side)

    def _stats(self, pair: str, ts: float | None = None, **fields) -> None:
        if self.stats_sink is not None:
            self.last_tick = time.time()
            self.stats_sink(self.id, pair, ts, **fields)


class BinanceFuturesDerivs(DerivSource):
    """USD-M perpetuals: `<sym>@forceOrder` and `<sym>@markPrice@1s` (mark, index, funding rate,
    next funding time) on fstream; open interest polled from `/fapi/v1/openInterest`."""
    id = "binance-perp"
    name = "Binance futures"
    REST = "https://fapi.binance.com"
    WS = "wss://fstream.binance.com/stream"
    OI_EVERY = 60.0

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self._pairs: dict[str, str] = {}

    async def list_products(self, c=None, max_age: float = 3600.0) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{self.REST}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            out = {}
            for s in r.json().get("symbols") or []:
                if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                    base, _ = unscale(str(s.get("baseAsset") or ""))
                    out[s["symbol"]] = f"{base}-USD"
            return out

    async def run(self, emit):
        listed = await self.list_products()
        rev = {v: k for k, v in listed.items()}
        self._pairs = {rev[s]: s for s in self.symbols if s in rev}
        if not self._pairs:
            self.note = "none of the active pairs has a USDT perpetual"
            return
        streams = "/".join(f"{p.lower()}@forceOrder/{p.lower()}@markPrice@1s" for p in self._pairs)

        async def subscribe(ws):
            self.note = f"liquidations and mark/funding for {len(self._pairs)} perps"

        async def poll_oi():
            """OI per contract once a minute, and the premium index (mark, index, funding, next
            funding) for every contract in one call: the REST side carries the stats on a box
            where fstream's websocket is unreachable (this one, 2026-10-13: REST answers, the
            socket never connects)."""
            while True:
                async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
                    try:
                        r = await c.get(f"{self.REST}/fapi/v1/premiumIndex")
                        for d in (r.json() if r.status_code == 200 else []) or []:
                            pair = self._pairs.get(d.get("symbol"))
                            if not pair:
                                continue
                            factor = unscale(str(d.get("symbol") or ""))[1]
                            mark = float(d.get("markPrice") or 0) / factor
                            self._stats(pair, float(d.get("time") or 0) / 1000.0 or None, mark=mark or None, index=(float(d.get("indexPrice") or 0) / factor) or None,
                                        funding=float(d["lastFundingRate"]) if d.get("lastFundingRate") not in (None, "") else None,
                                        next_funding=float(d.get("nextFundingTime") or 0) / 1000.0 or None, funding_hours=8.0)
                    except (httpx.HTTPError, ValueError, OSError, TypeError):
                        pass
                    for sym, pair in list(self._pairs.items()):
                        try:
                            r = await c.get(f"{self.REST}/fapi/v1/openInterest", params={"symbol": sym})
                            d = r.json() if r.status_code == 200 else {}
                            if d.get("openInterest") is not None:
                                factor = unscale(sym)[1]
                                oi = float(d["openInterest"]) * factor
                                st = self.stats_cache(pair)
                                self._stats(pair, oi=oi, oi_usd=(oi * st["mark"]) if st and st.get("mark") else None)
                        except (httpx.HTTPError, ValueError, OSError):
                            pass
                        await asyncio.sleep(max(0.05, self.OI_EVERY / max(1, len(self._pairs))))
        await asyncio.gather(self._ws_loop(emit, f"{self.WS}?streams={streams}", subscribe, self._handle), poll_oi())

    def _handle(self, m: dict, emit) -> None:
        d = m.get("data") or {}
        e = d.get("e")
        if e == "forceOrder":
            o = d.get("o") or {}
            pair = self._pairs.get(o.get("s"))
            if not pair:
                return
            factor = unscale(str(o.get("s") or ""))[1]
            px = float(o.get("ap") or o.get("p") or 0) / factor
            qty = float(o.get("q") or 0) * factor
            self._liq(pair, float(o.get("T") or 0) / 1000.0 or time.time(), px, px * qty, -1 if o.get("S") == "SELL" else 1)
        elif e == "markPriceUpdate":
            pair = self._pairs.get(d.get("s"))
            if not pair:
                return
            factor = unscale(str(d.get("s") or ""))[1]
            mark = float(d.get("p") or 0) / factor
            self._stats(pair, float(d.get("E") or 0) / 1000.0 or None, mark=mark or None, index=(float(d.get("i") or 0) / factor) or None,
                        funding=float(d["r"]) if d.get("r") not in (None, "") else None, next_funding=float(d.get("T") or 0) / 1000.0 or None, funding_hours=8.0)
            st = self.stats_cache(pair)
            if st and st.get("oi") is not None and mark:
                self._stats(pair, oi_usd=st["oi"] * mark)

    def stats_cache(self, pair: str) -> dict | None:
        """The engine's current record for this venue and pair (to price OI in USD), when the sink knows it."""
        getter = getattr(self, "stats_getter", None)
        return getter(self.id, pair) if getter else None


class BybitLinearDerivs(DerivSource):
    """USDT perpetuals on the linear public stream: `allLiquidation.<sym>` and `tickers.<sym>`
    (open interest, its USD value, funding rate, mark and index, next funding time)."""
    id = "bybit-perp"
    name = "Bybit perps"
    REST = "https://api.bybit.com"
    WS = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self._pairs: dict[str, str] = {}

    async def list_products(self, c=None, max_age: float = 3600.0) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{self.REST}/v5/market/instruments-info", params={"category": "linear", "limit": 1000})
            r.raise_for_status()
            out = {}
            for s in (r.json().get("result") or {}).get("list") or []:
                if s.get("contractType") == "LinearPerpetual" and s.get("quoteCoin") == "USDT" and s.get("status") == "Trading":
                    base, _ = unscale(str(s.get("baseCoin") or ""))
                    out[s["symbol"]] = f"{base}-USD"
            return out

    async def run(self, emit):
        listed = await self.list_products()
        rev = {v: k for k, v in listed.items()}
        self._pairs = {rev[s]: s for s in self.symbols if s in rev}
        if not self._pairs:
            self.note = "none of the active pairs has a USDT perpetual"
            return

        async def subscribe(ws):
            args = [f"allLiquidation.{p}" for p in self._pairs] + [f"tickers.{p}" for p in self._pairs]
            for k in range(0, len(args), 10):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 10]}))
            self.note = f"liquidations and tickers for {len(self._pairs)} perps"

        async def ping(ws):
            await ws.send(json.dumps({"op": "ping"}))
        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping)

    def _handle(self, m: dict, emit) -> None:
        topic = str(m.get("topic") or "")
        if topic.startswith("allLiquidation."):
            for d in m.get("data") or []:
                pair = self._pairs.get(d.get("s"))
                if not pair:
                    continue
                factor = unscale(str(d.get("s") or ""))[1]
                px, qty = float(d.get("p") or 0) / factor, float(d.get("v") or 0) * factor
                self._liq(pair, float(d.get("T") or 0) / 1000.0 or time.time(), px, px * qty, -1 if d.get("S") == "Buy" else 1)
        elif topic.startswith("tickers."):
            d = m.get("data") or {}
            pair = self._pairs.get(d.get("symbol"))
            if not pair:
                return
            factor = unscale(str(d.get("symbol") or ""))[1]
            f = lambda k: (float(d[k]) if d.get(k) not in (None, "") else None)
            self._stats(pair, float(m.get("ts") or 0) / 1000.0 or None,
                        oi=(f("openInterest") * factor) if f("openInterest") is not None else None, oi_usd=f("openInterestValue"),
                        funding=f("fundingRate"), mark=(f("markPrice") / factor) if f("markPrice") else None,
                        index=(f("indexPrice") / factor) if f("indexPrice") else None,
                        next_funding=(f("nextFundingTime") / 1000.0) if f("nextFundingTime") else None, funding_hours=8.0)


class OKXSwapDerivs(DerivSource):
    """USDT-margined swaps: `liquidation-orders`, `open-interest`, `funding-rate` and
    `mark-price` on the public websocket; OI in contracts x contract value."""
    id = "okx-perp"
    name = "OKX swaps"
    REST = "https://www.okx.com"
    WS = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self._insts: dict[str, str] = {}
        self._ctval: dict[str, float] = {}

    async def list_products(self, c=None, max_age: float = 3600.0) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{self.REST}/api/v5/public/instruments", params={"instType": "SWAP"})
            r.raise_for_status()
            out = {}
            for s in r.json().get("data") or []:
                if s.get("settleCcy") == "USDT" and s.get("state") == "live":
                    base = str(s.get("instId") or "").split("-")[0]
                    out[s["instId"]] = f"{base}-USD"
                    self._ctval[s["instId"]] = float(s.get("ctVal") or 1.0)
            return out

    async def run(self, emit):
        listed = await self.list_products()
        rev = {v: k for k, v in listed.items()}
        self._insts = {rev[s]: s for s in self.symbols if s in rev}
        if not self._insts:
            self.note = "none of the active pairs has a USDT swap"
            return

        async def subscribe(ws):
            args = [{"channel": "liquidation-orders", "instType": "SWAP"}]
            args += [{"channel": ch, "instId": i} for i in self._insts for ch in ("open-interest", "funding-rate", "mark-price")]
            for k in range(0, len(args), 50):
                await ws.send(json.dumps({"op": "subscribe", "args": args[k:k + 50]}))
            self.note = f"liquidations, OI, funding and mark for {len(self._insts)} swaps"

        async def ping(ws):
            await ws.send("ping")
        await self._ws_loop(emit, self.WS, subscribe, self._handle, ping=ping, ping_every=25.0)

    def _handle(self, m: dict, emit) -> None:
        arg = m.get("arg") or {}
        ch = arg.get("channel")
        for d in m.get("data") or []:
            inst = d.get("instId") or arg.get("instId")
            pair = self._insts.get(inst)
            if not pair:
                continue
            ct = self._ctval.get(inst, 1.0)
            if ch == "liquidation-orders":
                for x in d.get("details") or []:
                    px = float(x.get("bkPx") or 0)
                    qty = float(x.get("sz") or 0) * ct
                    self._liq(pair, float(x.get("ts") or 0) / 1000.0 or time.time(), px, px * qty, -1 if x.get("posSide") == "long" else 1)
            elif ch == "open-interest":
                oi = float(d.get("oi") or 0) * ct
                self._stats(pair, float(d.get("ts") or 0) / 1000.0 or None, oi=oi, oi_usd=None)
                st = self.stats_cache(pair)
                if st and st.get("mark"):
                    self._stats(pair, oi_usd=oi * st["mark"])
            elif ch == "funding-rate":
                self._stats(pair, float(d.get("ts") or 0) / 1000.0 or None, funding=float(d["fundingRate"]) if d.get("fundingRate") else None,
                            next_funding=float(d.get("nextFundingTime") or 0) / 1000.0 or None, funding_hours=8.0)
            elif ch == "mark-price":
                mark = float(d.get("markPx") or 0)
                self._stats(pair, float(d.get("ts") or 0) / 1000.0 or None, mark=mark or None)
                st = self.stats_cache(pair)
                if st and st.get("oi") is not None and mark:
                    self._stats(pair, oi_usd=st["oi"] * mark)

    def stats_cache(self, pair: str) -> dict | None:
        getter = getattr(self, "stats_getter", None)
        return getter(self.id, pair) if getter else None


class HyperliquidDerivs(DerivSource):
    """Hyperliquid's perp statistics from one `metaAndAssetCtxs` POST a minute: open interest
    (base units; USD at the mark), the hourly funding rate, the mark and the oracle price for
    every coin in the universe. Hyperliquid publishes no liquidation feed (its trades carry no
    forced flag), so this source sends no liquidations. Once an hour it also lists the HIP-3
    builder-deployed perp dexes (`perpDexs`, then `meta` per dex) and counts their markets in
    its note; those markets are not pairs Steel models."""
    id = "hyperliquid-perp"
    name = "Hyperliquid perps"
    REST = "https://api.hyperliquid.xyz/info"
    EVERY = 60.0
    DEX_EVERY = 3600.0

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.builder: dict[str, int] = {}             # HIP-3 dex name -> markets

    def parse_ctxs(self, meta: dict, ctxs: list) -> int:
        """Universe and asset contexts side by side (same order); returns how many coins matched a pair."""
        from ..config import canonical
        want = set(self.symbols)
        n = 0
        now = time.time()
        for u, c in zip((meta or {}).get("universe") or [], ctxs or []):
            coin = str(u.get("name") or "")
            if not coin or u.get("isDelisted"):
                continue
            base, factor = unscale(coin)
            pair = canonical(base)
            if pair not in want:
                continue
            mark = float(c.get("markPx") or 0) / factor
            oi = float(c.get("openInterest") or 0) * factor
            self._stats(pair, now, mark=mark or None, index=(float(c.get("oraclePx") or 0) / factor) or None,
                        funding=float(c["funding"]) if c.get("funding") not in (None, "") else None, funding_hours=1.0,
                        oi=oi or None, oi_usd=(oi * mark) if oi and mark else None)
            n += 1
        return n

    async def run(self, emit) -> None:
        last_dex = 0.0
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
            while True:
                try:
                    r = await c.post(self.REST, json={"type": "metaAndAssetCtxs"})
                    body = r.json() if r.status_code == 200 else None
                    if isinstance(body, list) and len(body) == 2:
                        n = self.parse_ctxs(body[0], body[1])
                        self.note = f"OI, funding, mark and oracle for {n} perps" + (f"; HIP-3: {sum(self.builder.values())} markets on {len(self.builder)} builder dex(es)" if self.builder else "")
                    else:
                        self.note = f"metaAndAssetCtxs: HTTP {r.status_code}"
                except (httpx.HTTPError, ValueError, OSError, TypeError) as e:
                    self.note = f"metaAndAssetCtxs: {type(e).__name__}"
                if time.time() - last_dex >= self.DEX_EVERY:
                    last_dex = time.time()
                    try:
                        r = await c.post(self.REST, json={"type": "perpDexs"})
                        dexes = [x for x in (r.json() if r.status_code == 200 else []) or [] if isinstance(x, dict) and x.get("name")]
                        found = {}
                        for dx in dexes[:20]:
                            rm = await c.post(self.REST, json={"type": "meta", "dex": dx["name"]})
                            m = rm.json() if rm.status_code == 200 else {}
                            found[str(dx["name"])] = len([u for u in (m.get("universe") or []) if not u.get("isDelisted")])
                        self.builder = found
                    except (httpx.HTTPError, ValueError, OSError, TypeError):
                        pass
                await asyncio.sleep(self.EVERY)


DERIVS = [BinanceFuturesDerivs, BybitLinearDerivs, OKXSwapDerivs, HyperliquidDerivs]


class DerivManager:
    """Runs the enabled derivatives feeds (`derivs_venues`), restarting each on failure, and
    hands their events to the stores."""

    def __init__(self, cfg, symbols: list[str], liq_sink, stats_sink, stats_getter, trace=None):
        self.cfg, self.symbols, self.trace = cfg, list(symbols), trace
        want = {v.strip().lower() for v in str(getattr(cfg, "derivs_venues", "") or "").split(",") if v.strip()}
        self.sources: dict[str, DerivSource] = {}
        for cls in DERIVS:
            if not want or cls.id in want or cls.id.split("-")[0] in want:
                src = cls(cfg, self.symbols)
                src.liq_sink, src.stats_sink, src.stats_getter = liq_sink, stats_sink, stats_getter
                self.sources[cls.id] = src
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        if not bool(getattr(self.cfg, "derivs", True)):
            return
        for sid, src in self.sources.items():
            if sid not in self.tasks:
                self.tasks[sid] = asyncio.create_task(self._supervise(src))

    async def _supervise(self, src: DerivSource) -> None:
        while True:
            try:
                await src.run(lambda t: None)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                src.note = f"{type(e).__name__}: {str(e)[:80]}"
                if self.trace:
                    self.trace.emit("feed", f"{src.name} derivatives feed: {src.note}; retrying in 30 s", "warn")
                await asyncio.sleep(30)

    async def stop(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        for t in self.tasks.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.tasks.clear()

    def status(self) -> list[dict]:
        return [{"id": s.id, "name": s.name, "note": s.note or "", "events": s.ticks, "last": s.last_tick, "running": sid in self.tasks} for sid, s in self.sources.items()]
