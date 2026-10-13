"""FastAPI app: serves the dashboard, a JSON snapshot, a control endpoint and the live websocket."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth as _auth
from .engine import Engine, clean

log = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent.parent / "web"
DOCS = Path(__file__).resolve().parent.parent / "docs"


def create_app(engine: Engine) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.start()
        try:
            yield
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await engine.stop()

    app = FastAPI(title="Steel", lifespan=lifespan)
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.requests import Request

    mode = str(getattr(getattr(engine, "cfg", None), "auth", "off") or "off").lower()   # a stub engine with no config is open

    def _token() -> str:
        return str(getattr(engine, "token", "") or "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """The operator token on every mutating route (and on reads with auth=all); the
        dashboard itself and its static files are always served."""
        path = request.url.path
        if mode != "off" and path.startswith("/api/") and (request.method != "GET" or mode == "all"):
            given = _auth.presented(request.headers, dict(request.query_params))
            if not _auth.ok(_token(), given):
                return JSONResponse({"error": "operator token required", "auth": mode}, status_code=401)
        return await call_next(request)
    # A phone or a second machine on the LAN may open the dashboard or probe /api/state
    # from another origin. The server is LAN-only and read-only over HTTP, so a wildcard is fine.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    mimetypes.add_type("application/manifest+json", ".webmanifest")   # the web-app manifest a phone installs from
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    if DOCS.is_dir():
        app.mount("/docs", StaticFiles(directory=DOCS, html=True), name="docs")   # the whitepaper and the design notes, from the box itself
    from .apiv1 import make_router
    app.include_router(make_router(engine))                 # the versioned data API (M24)

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/state")
    async def state():
        return JSONResponse(clean(engine.snapshot()))

    @app.get("/api/news")
    async def news():
        return JSONResponse(clean(engine.news))

    @app.get("/api/sources")
    async def sources():
        return JSONResponse(clean({"sources": engine.sources.status(), "news_sources": engine.news_hub.status(),
                                   "providers": engine.sources.provider_map()}))

    @app.get("/api/signals")
    async def signals():
        return JSONResponse(clean({"signals": engine.signals_state, "providers": engine.signals.status(),
                                   "overlay": engine.overlay_enabled}))

    @app.get("/api/vault")
    async def vault_status():
        return JSONResponse(clean(engine.vault_status()))

    @app.post("/api/vault/backup")
    async def vault_backup():
        return JSONResponse(clean(engine.vault_backup()))

    @app.post("/api/vault/ack")
    async def vault_ack():
        return JSONResponse(clean(engine.vault_ack()))

    @app.get("/api/arb")
    async def arb_status():
        return JSONResponse(clean(engine.arb.status()))

    @app.post("/api/arb")
    async def arb_action(payload: dict):
        return JSONResponse(clean(engine.arb_action(payload)))

    @app.get("/api/live")
    async def live_status():
        return JSONResponse(clean(engine.live_status()))

    @app.post("/api/live")
    async def live_action(payload: dict):
        if str(payload.get("action") or "") == "flatten":
            return JSONResponse(clean(await engine.live_flatten(str(payload.get("reason") or "operator"))))
        return JSONResponse(clean(engine.live_action(payload)))

    @app.get("/api/arb/replay")
    async def arb_replay(days: float = 7.0, force: bool = False):
        return JSONResponse(clean(await engine.arb_replay(days=days, force=force)))

    @app.get("/api/handoff")
    async def handoff():
        return JSONResponse(clean(engine.handoff.status()))

    @app.get("/api/indicators")
    async def indicators():
        return JSONResponse(clean(engine.indicators_view()))

    @app.post("/api/indicators")
    async def indicators_action(payload: dict):
        return JSONResponse(clean(engine.indicators_action(payload)))

    @app.get("/api/indicator/{name}/{sym}")
    async def indicator_values(name: str, sym: str, n: int = 288):
        from .config import canonical
        return JSONResponse(clean(engine.indicator_values(name, canonical(sym), n=min(max(10, n), 2000))))

    @app.get("/api/algo")
    async def algo_status():
        return JSONResponse(clean(engine.algo_status()))

    @app.post("/api/algo")
    async def algo_action(payload: dict):
        return JSONResponse(clean(engine.algo_action(payload)))

    @app.get("/api/rules")
    async def rules():
        return JSONResponse(clean(engine.rules_status()))

    @app.post("/api/rules")
    async def rules_action(payload: dict):
        return JSONResponse(clean(engine.rules_action(payload)))

    @app.post("/api/handoff")
    async def handoff_action(payload: dict):
        return JSONResponse(clean(engine.handoff_action(payload)))

    @app.get("/api/portfolio/history")
    async def portfolio_history(days: float = 30.0):
        return JSONResponse(clean(engine.account_history(days=days)))

    @app.get("/api/export/{what}.csv")
    async def export_csv(what: str):
        from starlette.responses import Response
        res = engine.export_csv(what)
        if res is None:
            return JSONResponse({"error": "unknown export: paper-trades, paper-transfers, account-history, orders, arb"}, status_code=404)
        name, text = res
        return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/monitor")
    async def monitor():
        return JSONResponse(clean(await asyncio.to_thread(engine.monitor)))

    @app.get("/api/ticket/{sym}")
    async def ticket(sym: str):
        from .config import canonical
        return JSONResponse(clean(engine.ticket(canonical(sym))))

    @app.get("/api/premium")
    async def premium(pair: str, tf: float = 300.0, n: int = 288, reference: str = "binance"):
        from .config import canonical
        return JSONResponse(clean(await asyncio.to_thread(engine.premium, canonical(pair), tf, max(2, min(2000, n)), reference.lower())))

    @app.get("/api/calendar")
    async def calendar():
        return JSONResponse(clean(engine.calendar_view()))

    @app.get("/api/predictions")
    async def predictions():
        return JSONResponse(clean(engine.predictions_view()))

    @app.get("/api/screener")
    async def screener():
        return JSONResponse(clean(await asyncio.to_thread(engine.screener)))

    @app.get("/api/options")
    async def options(asset: str = "BTC"):
        return JSONResponse(clean(engine.options_view(asset)))

    @app.get("/api/positioning")
    async def positioning(pair: str, n: int = 288, sensitivity: float | None = None):
        from .config import canonical
        return JSONResponse(clean(await asyncio.to_thread(engine.positioning, canonical(pair), max(12, min(2000, n)), sensitivity)))

    @app.get("/api/liqmap")
    async def liqmap(pair: str, n: int = 288, bucket_bps: float = 10.0):
        from .config import canonical
        return JSONResponse(clean(await asyncio.to_thread(engine.liqmap, canonical(pair), max(12, min(2000, n)), max(1.0, bucket_bps))))

    @app.get("/api/heatmap")
    async def heatmap(pair: str, tf: float = 300.0, n: int = 120, bucket_bps: float = 5.0, quote: str = "", to: float | None = None):
        from .config import canonical
        return JSONResponse(clean(await asyncio.to_thread(engine.heatmap, canonical(pair), tf, max(1, min(600, n)), max(0.5, bucket_bps), quote or None, to)))

    @app.get("/api/levels")
    async def levels(pair: str, days: int = 35):
        from .config import canonical
        return JSONResponse(clean(await asyncio.to_thread(engine.levels, canonical(pair), max(3, min(400, days)))))

    @app.get("/api/footprint")
    async def footprint(pair: str, tf: float = 300.0, n: int = 120, bucket_bps: float = 2.0, venues: str = "", to: float | None = None, bubble_usd: float | None = None):
        from .config import canonical
        vs = {v.strip().lower() for v in venues.split(",") if v.strip()} or None
        return JSONResponse(clean(await asyncio.to_thread(engine.footprint, canonical(pair), tf, max(1, min(600, n)), max(0.1, bucket_bps), vs, to, bubble_usd)))

    @app.get("/api/candles")
    async def candles(pair: str, tf: float = 300.0, n: int = 300, venues: str = "", to: float | None = None):
        from .config import canonical
        vs = {v.strip().lower() for v in venues.split(",") if v.strip()} or None
        return JSONResponse(clean(engine.candles(canonical(pair), tf=tf, n=max(2, min(3000, n)), venues=vs, to=to)))

    @app.get("/api/derivs")
    async def derivs(pair: str, bars: int = 72):
        from .config import canonical
        return JSONResponse(clean(engine.derivs_view(canonical(pair), bars=max(1, min(600, bars)))))

    @app.get("/api/book")
    async def book(pair: str, n: int = 16, bucket_bps: float = 0.0, quote: str = ""):
        from .config import canonical
        return JSONResponse(clean(engine.book_view(canonical(pair), n=max(1, min(100, n)), bucket_bps=max(0.0, bucket_bps), quote=quote or None)))

    @app.get("/api/tape")
    async def tape(pair: str, n: int = 50, min_usd: float = 0.0, since: float | None = None, bars: int = 60):
        from .config import canonical
        return JSONResponse(clean(engine.tape_view(canonical(pair), n=max(1, min(512, n)), min_usd=min_usd, since=since, bars=max(1, min(600, bars)))))

    @app.get("/api/alerts")
    async def alerts():
        return JSONResponse(clean(engine.alerts_status()))

    @app.get("/api/execution")
    async def execution():
        return JSONResponse(clean(engine.execution_status()))

    @app.post("/api/execution")
    async def execution_action(payload: dict):
        return JSONResponse(clean(engine.execution_action(payload)))

    @app.get("/api/dislocations")
    async def dislocations():
        return JSONResponse(clean(engine.dislocations.stats(200)))

    @app.get("/api/keys")
    async def keys():
        return JSONResponse(clean(engine.keys_status()))

    @app.get("/api/backtest")
    async def backtest():
        return JSONResponse(clean({"backtest": engine.backtest, "progress": engine.backtest_progress}))

    @app.post("/api/keys")
    async def set_keys(payload: dict):
        return JSONResponse(clean(engine.set_key(payload.get("service", ""), payload.get("values", {}))))

    @app.post("/api/control")
    async def control(payload: dict):
        return JSONResponse(clean(engine.apply_control(payload)))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if mode == "all" and not _auth.ok(_token(), _auth.presented(websocket.headers, dict(websocket.query_params))):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        q = engine.subscribe()

        async def push() -> None:
            await websocket.send_text(engine.snapshot_json())
            while True:
                await websocket.send_text(await q.get())

        async def listen() -> None:
            # The dashboard never sends anything, but only a read notices the peer going away
            # or uvicorn closing the socket on SIGTERM. A handler that only pushes sits on its
            # queue until the next publish; with the engine idle (or dead) that is forever, and
            # uvicorn's graceful shutdown waits for every handler to return before it stops.
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                while (await websocket.receive())["type"] != "websocket.disconnect":
                    pass

        pusher = asyncio.create_task(push())
        listener = asyncio.create_task(listen())
        try:
            await asyncio.wait({pusher, listener}, return_when=asyncio.FIRST_COMPLETED)
            if pusher.done() and not pusher.cancelled():
                with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                    pusher.result()                 # anything else is a real bug: let uvicorn log it
        finally:
            pusher.cancel()
            listener.cancel()
            await asyncio.gather(pusher, listener, return_exceptions=True)
            engine.unsubscribe(q)

    return app
