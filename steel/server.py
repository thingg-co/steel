"""FastAPI app: serves the dashboard, a JSON snapshot, a control endpoint and the live websocket."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth as _auth
from .engine import Engine, clean

log = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent.parent / "web"


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
    app.mount("/static", StaticFiles(directory=WEB), name="static")

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
