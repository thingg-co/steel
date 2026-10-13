"""The data API, version 1 (M24): every store Steel keeps, over REST and one websocket, for
other programs. Shapes are the dashboard's, frozen under `/api/v1`; the index route documents
them. Every request needs the operator token whenever auth is on (`STEEL_AUTH` mutate or all:
a program is not the dashboard), as a `Bearer` header or `?token=`. A weight budget per token
(`api_weight_per_minute`, 1200) keeps a runaway client from starving the engine: each route
costs its listed weight, `/api/v1/usage` says what is left, and an exhausted budget answers
429 with `Retry-After`. Reads only: nothing under /api/v1 places an order or changes a setting.

The stream: `ws /api/v1/stream?pairs=BTC-USD,ETH-USD&channels=trades,book,liquidations,bars,rules`
sends one JSON object per event: `{"ch": "trades", "pair": "BTC-USD", "t": ..., "venue": ...,
"px": ..., "sz": ..., "side": 1|-1|0}`; `book` is the top of book per venue (at most four a
second per pair), `liquidations` one per forced order, `bars` the engine's bar message, `rules`
the series alerts. Prints are dropped, never queued without bound, when a client falls behind.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import auth as _auth
from .config import canonical

CHANNELS = ("trades", "book", "liquidations", "bars", "rules")
VERSION = "1.0"

# route -> (weight, what it returns)
ROUTES = {
    "GET /api/v1": (0, "this index: the routes, their weights and the budget"),
    "GET /api/v1/usage": (0, "the weight used and left for this token in the current minute"),
    "GET /api/v1/markets": (1, "every modeled pair: base, serving venue, kind, price, 24-hour dollar volume, trusted"),
    "GET /api/v1/screener": (5, "one row per pair from every stored series (price, 1-hour change, tape delta, OI, funding, liquidations, book imbalance, dislocations, the model's call)"),
    "GET /api/v1/candles/{pair}?tf=300&n=300&venues=a,b&to=": (2, "OHLCV with buy/sell volume per candle from the tape (any tf for the day held) or the 5-minute bars beyond"),
    "GET /api/v1/tape/{pair}?n=50&min_usd=0&bars=60": (2, "the last prints, size-band volumes over the hour, per-bar delta and CVD"),
    "GET /api/v1/delta/{pair}?bars=60&band=": (2, "per-bar buy and sell volume, delta and CVD; `band` selects one size band (0..10) or a `min_usd,max_usd` pair"),
    "GET /api/v1/footprint/{pair}?tf=300&n=120&bucket_bps=2": (5, "bid/ask volume per price bucket per candle, POC, value area, imbalances, large prints"),
    "GET /api/v1/book/{pair}?n=16&bucket_bps=0&quote=": (2, "the aggregate ladder with venue shares, the depth bands, per-venue books"),
    "GET /api/v1/heatmap/{pair}?tf=300&n=120&bucket_bps=5": (5, "resting liquidity per price row per column from the level history"),
    "GET /api/v1/liqmap/{pair}?n=288&bucket_bps=10": (5, "the modeled liquidation map: clusters per leverage band, their evolution"),
    "GET /api/v1/derivs/{pair}?bars=72": (2, "open interest, funding and mark per venue and aggregated; liquidations per bar and the recent list"),
    "GET /api/v1/positioning/{pair}?n=288": (3, "OI-confirmed net longs and shorts per bar, absorption flags"),
    "GET /api/v1/premium/{pair}?tf=300&n=288&reference=binance": (3, "per-venue premium to the reference venue and the perp-spot basis, per candle"),
    "GET /api/v1/levels/{pair}?days=35": (2, "day, week and month opens, highs, lows and closes"),
    "GET /api/v1/options/{asset}": (2, "Deribit's chain: GEX by strike, smile, term structure, max pain, zero gamma"),
    "GET /api/v1/forecast/{pair}": (1, "the model's latest call: action, quantiles, P(up), size, the extra horizons"),
    "GET /api/v1/calendar": (1, "the next scheduled events and the event clock"),
    "GET /api/v1/predictions": (1, "Polymarket's open crypto markets"),
    "WS /api/v1/stream?pairs=&channels=": (0, "trades, book tops, liquidations, bars and rule hits as they happen"),
}


class Usage:
    """A sliding one-minute weight budget per key."""

    def __init__(self, per_minute: int = 1200):
        self.limit = max(1, int(per_minute))
        self.hits: dict[str, deque] = defaultdict(deque)

    def used(self, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        d = self.hits[key]
        while d and d[0][0] <= now - 60.0:
            d.popleft()
        return sum(w for _, w in d)

    def charge(self, key: str, weight: int, now: float | None = None) -> tuple[bool, int]:
        """Charge the weight if it fits; (ok, used after the charge)."""
        now = time.time() if now is None else now
        used = self.used(key, now)
        if weight and used + weight > self.limit:
            return False, used
        if weight:
            self.hits[key].append((now, int(weight)))
        return True, used + weight

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        d = self.hits[key]
        return max(1, int(d[0][0] + 60.0 - now) + 1) if d else 1


def _weight(route: str) -> int:
    for k, (w, _) in ROUTES.items():
        if k.split("?")[0].split(" ")[1].replace("{pair}", "").replace("{asset}", "").rstrip("/") == route.rstrip("/"):
            return w
    return 1


def make_router(engine, usage: Usage | None = None) -> APIRouter:
    r = APIRouter(prefix="/api/v1")
    usage = usage or Usage(int(getattr(getattr(engine, "cfg", None), "api_weight_per_minute", 1200) or 1200))
    mode = str(getattr(getattr(engine, "cfg", None), "auth", "off") or "off").lower()

    def key_of(headers, query, client) -> str:
        given = _auth.presented(headers, dict(query))
        return ("tok:" + given[-8:]) if given else ("ip:" + (client.host if client else "?"))

    def gate(request: Request, route: str):
        """401 without the token (when auth is on), 429 past the budget; else None."""
        given = _auth.presented(request.headers, dict(request.query_params))
        if mode != "off" and not _auth.ok(str(getattr(engine, "token", "") or ""), given):
            return JSONResponse({"error": "operator token required: Bearer header or ?token=", "auth": mode}, status_code=401)
        key = key_of(request.headers, request.query_params, request.client)
        ok, used = usage.charge(key, _weight(route))
        if not ok:
            return JSONResponse({"error": "weight budget exhausted", "used": used, "limit": usage.limit}, status_code=429,
                                headers={"Retry-After": str(usage.retry_after(key)), "X-Weight-Used": str(used), "X-Weight-Limit": str(usage.limit)})
        request.state.weight = (used, usage.limit)
        return None

    def reply(request: Request, body) -> JSONResponse:
        from .engine import clean
        used, limit = getattr(request.state, "weight", (0, usage.limit))
        return JSONResponse(clean(body), headers={"X-Weight-Used": str(used), "X-Weight-Limit": str(limit), "X-API-Version": VERSION})

    def pair_of(name: str):
        p = canonical(name)
        return p if p in set(getattr(engine, "symbols", []) or []) else None

    def venues_of(v: str | None):
        return {x.strip() for x in v.split(",") if x.strip()} if v else None

    async def run(request: Request, route: str, fn, pair: str | None = None, **kw):
        """The gate, the pair check, the view, the reply headers: every v1 route goes through here."""
        bad = gate(request, route)
        if bad is not None:
            return bad
        if pair is not None:
            p = pair_of(pair)
            if not p:
                return JSONResponse({"error": f"unknown pair {pair!r}; see /api/v1/markets"}, status_code=404)
            kw["pair"] = p
        try:
            body = fn(**kw)
            if asyncio.iscoroutine(body):
                body = await body
        except (KeyError, ValueError, TypeError, IndexError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        return reply(request, body)

    @r.get("")
    @r.get("/")
    async def index(request: Request):
        bad = gate(request, "/api/v1")
        if bad is not None:
            return bad
        return reply(request, {"version": VERSION, "auth": mode, "budget_per_minute": usage.limit, "channels": list(CHANNELS),
                               "routes": [{"route": k, "weight": w, "returns": d} for k, (w, d) in ROUTES.items()],
                               "note": "reads only; pairs are canonical BASE-USD; times are unix seconds; dollar amounts are USD"})

    @r.get("/usage")
    async def usage_route(request: Request):
        bad = gate(request, "/api/v1/usage")
        if bad is not None:
            return bad
        key = key_of(request.headers, request.query_params, request.client)
        used = usage.used(key)
        return reply(request, {"used": used, "left": max(0, usage.limit - used), "limit": usage.limit, "window_s": 60})

    @r.get("/markets")
    async def markets(request: Request):
        return await run(request, "/api/v1/markets", engine.markets_v1)

    @r.get("/screener")
    async def screener(request: Request):
        return await run(request, "/api/v1/screener", engine.screener)

    @r.get("/candles/{pair}")
    async def candles(request: Request, pair: str, tf: float = 300.0, n: int = 300, venues: str | None = None, to: float | None = None):
        return await run(request, "/api/v1/candles/", engine.candles, pair=pair, tf=tf, n=min(max(1, n), 5000), venues=venues_of(venues), to=to)

    @r.get("/tape/{pair}")
    async def tape(request: Request, pair: str, n: int = 50, min_usd: float = 0.0, bars: int = 60):
        return await run(request, "/api/v1/tape/", engine.tape_view, pair=pair, n=min(max(1, n), 2000), min_usd=min_usd, bars=min(max(1, bars), 2000))

    @r.get("/delta/{pair}")
    async def delta(request: Request, pair: str, bars: int = 60, band: str | None = None):
        from .tape import BANDS

        def view(pair: str):
            lo, hi = 0.0, None
            if band:
                if "," in band:
                    a, b = band.split(",", 1)
                    lo, hi = float(a or 0), (float(b) if b.strip() else None)
                else:
                    i = int(band)
                    lo = float(BANDS[i]) if i > 0 else 0.0
                    hi = float(BANDS[i + 1]) if i + 1 < len(BANDS) else None
            return engine.tape.cvd(pair, float(engine.cfg.bar_seconds), min(max(1, bars), 2000), min_usd=lo, max_usd=hi)
        return await run(request, "/api/v1/delta/", view, pair=pair)

    @r.get("/footprint/{pair}")
    async def footprint(request: Request, pair: str, tf: float = 300.0, n: int = 120, bucket_bps: float = 2.0, venues: str | None = None, to: float | None = None):
        return await run(request, "/api/v1/footprint/", engine.footprint, pair=pair, tf=tf, n=min(max(1, n), 1000), bucket_bps=bucket_bps, venues=venues_of(venues), to=to)

    @r.get("/book/{pair}")
    async def book(request: Request, pair: str, n: int = 16, bucket_bps: float = 0.0, quote: str | None = None):
        return await run(request, "/api/v1/book/", engine.book_view, pair=pair, n=min(max(1, n), 200), bucket_bps=bucket_bps, quote=quote)

    @r.get("/heatmap/{pair}")
    async def heatmap(request: Request, pair: str, tf: float = 300.0, n: int = 120, bucket_bps: float = 5.0, quote: str | None = None, to: float | None = None):
        return await run(request, "/api/v1/heatmap/", engine.heatmap, pair=pair, tf=tf, n=min(max(1, n), 1000), bucket_bps=bucket_bps, quote=quote, to=to)

    @r.get("/liqmap/{pair}")
    async def liqmap(request: Request, pair: str, n: int = 288, bucket_bps: float = 10.0):
        return await run(request, "/api/v1/liqmap/", engine.liqmap, pair=pair, n=min(max(1, n), 2000), bucket_bps=bucket_bps)

    @r.get("/derivs/{pair}")
    async def derivs(request: Request, pair: str, bars: int = 72):
        return await run(request, "/api/v1/derivs/", engine.derivs_view, pair=pair, bars=min(max(1, bars), 2000))

    @r.get("/positioning/{pair}")
    async def positioning(request: Request, pair: str, n: int = 288):
        return await run(request, "/api/v1/positioning/", engine.positioning, pair=pair, n=min(max(1, n), 2000))

    @r.get("/premium/{pair}")
    async def premium(request: Request, pair: str, tf: float = 300.0, n: int = 288, reference: str = "binance"):
        return await run(request, "/api/v1/premium/", engine.premium, pair=pair, tf=tf, n=min(max(1, n), 2000), reference=reference)

    @r.get("/levels/{pair}")
    async def levels(request: Request, pair: str, days: int = 35):
        return await run(request, "/api/v1/levels/", engine.levels, pair=pair, days=min(max(1, days), 400))

    @r.get("/options/{asset}")
    async def options(request: Request, asset: str):
        return await run(request, "/api/v1/options/", engine.options_view, asset=asset)

    @r.get("/forecast/{pair}")
    async def forecast(request: Request, pair: str):
        return await run(request, "/api/v1/forecast/", engine.forecast_v1, pair=pair)

    @r.get("/calendar")
    async def calendar(request: Request):
        return await run(request, "/api/v1/calendar", engine.calendar_view)

    @r.get("/predictions")
    async def predictions(request: Request):
        return await run(request, "/api/v1/predictions", engine.predictions_view)

    @r.websocket("/stream")
    async def stream(ws: WebSocket):
        q = dict(ws.query_params)
        if mode != "off" and not _auth.ok(str(getattr(engine, "token", "") or ""), _auth.presented(ws.headers, q)):
            await ws.close(code=4401)
            return
        pairs = {canonical(p) for p in (q.get("pairs") or "").split(",") if p.strip()} or None
        chans = {c.strip() for c in (q.get("channels") or "trades").split(",") if c.strip() in CHANNELS} or {"trades"}
        await ws.accept()
        sub = {"q": asyncio.Queue(maxsize=4000), "pairs": pairs, "channels": chans, "dropped": 0, "since": time.time()}
        subs = getattr(engine, "streams", None)
        if subs is None:
            engine.streams = subs = []
        subs.append(sub)
        await ws.send_text(json.dumps({"ch": "hello", "version": VERSION, "pairs": sorted(pairs) if pairs else "all", "channels": sorted(chans)}))

        async def pump():
            while True:
                await ws.send_text(await sub["q"].get())

        async def listen():
            while True:
                await ws.receive_text()                   # pings or anything else; a disconnect raises

        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(listen())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                    task.result()                         # a disconnect is the normal end; nothing to log
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if sub in subs:
                subs.remove(sub)
    return r


def push(engine, ch: str, pair: str | None, msg: dict) -> int:
    """Hand one event to every stream subscriber that wants it; a full queue drops it.
    Returns how many got it. Cheap when nobody listens (one attribute read)."""
    subs = getattr(engine, "streams", None)
    if not subs:
        return 0
    text = None
    n = 0
    for sub in subs:
        if ch not in sub["channels"] or (pair is not None and sub["pairs"] is not None and pair not in sub["pairs"]):
            continue
        if text is None:
            text = json.dumps({"ch": ch, "pair": pair, **msg}, separators=(",", ":"), default=str)
        try:
            sub["q"].put_nowait(text)
            n += 1
        except asyncio.QueueFull:
            sub["dropped"] += 1
    return n
