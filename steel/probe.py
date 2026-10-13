"""`steel probe`: does every venue still answer the way its code expects? Run nightly.

For each market-data source: the product list (when it has one), one hour of 5-minute history
for BTC-USD, and a live tick over its stream within a few seconds. For each account reader and
each execution client: every declared endpoint, unauthenticated, must still exist on the
venue (401, 400, 403 and 405 all say "the path is there"; 404 and 410 say the contract moved).
Nothing here signs: a GET first, and when a venue answers 404 to a GET on a POST-only path
(Kraken's and Bybit's private endpoints do) an empty, unsigned POST, which no venue can act on.
A path declared as a prefix (a trailing slash or star) cannot be probed bare, so its 404 is
noted, not failed. A reader that talks to a second host (Binance futures, KuCoin futures, the
dYdX indexer) is retried on every bare https host constant its module declares. 451 is geo-blocking
and 5xx a venue-side error: both reported, neither failed. The exit code is 1 when a
default-on source or a trade venue the book uses breaks."""
from __future__ import annotations

import asyncio
import sys
import time
import urllib.parse

import httpx

from .config import Config
from .sources import REGISTRY
from .sources.base import Tick

LISTERS = ("list_products", "list_pairs", "list_instruments", "list_coins")
EXISTS = {400, 401, 403, 405, 415, 422}          # the path is there, the request is not what it wants
GEO = {451}
UA = "steel-probe/1.0"


async def probe_source(cls, cfg, symbols=("BTC-USD", "ETH-USD"), stream_s: float = 12.0) -> dict:
    out = {"kind": "source", "id": cls.id, "default_on": bool(getattr(cls, "default_on", False)), "venue": cls.venue,
           "products": None, "history": None, "stream": None, "errors": [], "ms": 0}
    t0 = time.time()
    try:
        src = cls(cfg, list(symbols))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"construct: {type(e).__name__}: {str(e)[:80]}")
        return out
    lister = next((getattr(src, m) for m in LISTERS if hasattr(src, m)), None)
    if lister is not None:
        try:
            got = await asyncio.wait_for(lister(), 20.0)
            out["products"] = len(got) if got is not None else 0
            if not got:
                out["errors"].append("product list empty")
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"products: {type(e).__name__}: {str(e)[:80]}")
    if getattr(cls, "venue", "") != "simulated":
        try:
            ticks = await asyncio.wait_for(src.backfill(["BTC-USD"], time.time() - 3600.0), 30.0)
            out["history"] = len(ticks or [])
            if not ticks and type(src).backfill is not type(src).__mro__[-2].backfill:
                out["errors"].append("history empty")
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"history: {type(e).__name__}: {str(e)[:80]}")
        got: list[Tick] = []
        done = asyncio.Event()

        def emit(t):
            got.append(t)
            if len(got) >= 3:
                done.set()
        task = asyncio.create_task(src.run(emit))
        try:
            await asyncio.wait_for(done.wait(), stream_s)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        out["stream"] = len(got)
        if not got:
            out["errors"].append("no tick on the stream" + (f" ({src.note})" if getattr(src, "note", None) else ""))
    out["ms"] = int((time.time() - t0) * 1000)
    return out


async def _exists(client: httpx.AsyncClient, method: str, url: str) -> tuple[bool, str]:
    try:
        r = await client.request(method, url)
    except (httpx.HTTPError, OSError) as e:
        return False, f"{type(e).__name__}"
    if r.status_code in GEO:
        return True, "geo-blocked (451)"
    if r.status_code < 400 or r.status_code in EXISTS:
        return True, str(r.status_code)
    if r.status_code in (404, 410):
        return False, f"{r.status_code}: the path is gone"
    if r.status_code >= 500:
        return True, f"{r.status_code}: venue-side error"
    return False, str(r.status_code)


def _hosts(cls) -> list[str]:
    """The class's HOST (set in __init__ for some readers, so an unconfigured instance is
    tried), then every https `*HOST*` constant of its module: a reader's second venue host."""
    hosts = []
    h = str(getattr(cls, "HOST", "") or "")
    if not h:
        try:
            h = str(getattr(cls(), "HOST", "") or "")
        except Exception:  # noqa: BLE001
            h = ""
    if h:
        hosts.append(h)
    mod = sys.modules.get(cls.__module__)
    for name in dir(mod):
        v = getattr(mod, name, None)
        if name.isupper() and isinstance(v, str) and v.startswith("https://") and "/" not in v[8:] and v not in hosts:
            hosts.append(v)                              # a bare https host constant: the reader's second venue (futures, the indexer)
    return hosts


async def probe_endpoints(kind: str, cls, client: httpx.AsyncClient, methods=("GET", "POST")) -> dict:
    hosts = _hosts(cls)
    out = {"kind": kind, "id": cls.id, "host": hosts[0] if hosts else "", "paths": {}, "errors": []}
    for ep in getattr(cls, "endpoints", ()) or ():
        raw = str(ep)
        prefix = raw.endswith("*") or raw.endswith("/")
        path = raw.rstrip("*")
        urls = [path] if path.startswith("http") else [h + path for h in hosts]
        if not urls:
            out["errors"].append(f"{path}: no host")
            continue
        ok, why, first = False, "", ""
        for url in urls:
            for m in methods:
                ok, why = await _exists(client, m, url)
                first = first or why
                if ok:
                    break
            if ok:
                break
        if not ok:
            why = first                                     # the primary host's answer is the one to report
        if not ok and prefix and why.startswith("404"):
            ok, why = True, "prefix (404 bare, not probed)"
        out["paths"][path] = why
        if not ok:
            out["errors"].append(f"{path}: {why}")
    return out


async def run(*, sources: bool = True, accounts: bool = True, executors: bool = True, stream_s: float = 20.0, only: str = "") -> dict:
    cfg = Config()
    cfg.news = False
    report = {"t": time.time(), "results": []}
    if sources:
        srcs = [c for c in REGISTRY if c.venue != "simulated" and (not only or c.id == only)]
        res = await asyncio.gather(*(probe_source(c, cfg, stream_s=stream_s) for c in srcs), return_exceptions=True)
        for c, r in zip(srcs, res):
            report["results"].append(r if isinstance(r, dict) else {"kind": "source", "id": c.id, "errors": [f"{type(r).__name__}: {r}"], "default_on": c.default_on})
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA, "Accept": "application/json"}, follow_redirects=True) as client:
        if accounts:
            from .accounts import ACCOUNTS
            res = await asyncio.gather(*(probe_endpoints("account", c, client) for c in ACCOUNTS if not only or c.id == only))
            report["results"].extend(res)
        if executors:
            from .execution import EXECUTORS
            res = await asyncio.gather(*(probe_endpoints("executor", c, client) for c in EXECUTORS if not only or c.id == only))
            report["results"].extend(res)
    report["failed"] = [r["id"] for r in report["results"] if r.get("errors")]
    report["critical"] = [r["id"] for r in report["results"] if r.get("errors") and (r.get("default_on") or r["id"] in (cfg.trade_venue_spot, cfg.trade_venue_perp))]
    return report


def fmt(report: dict) -> str:
    lines = []
    for r in report["results"]:
        if r["kind"] == "source":
            parts = [f"products {r.get('products') if r.get('products') is not None else '-'}", f"history {r.get('history') if r.get('history') is not None else '-'}",
                     f"stream {r.get('stream') if r.get('stream') is not None else '-'}", f"{r.get('ms', 0)} ms"]
            status = "FAIL" if r.get("errors") else "ok"
            lines.append(f"{status:4} source   {r['id']:<14} {'on ' if r.get('default_on') else 'off'} {' · '.join(parts)}" + (f"  {'; '.join(r['errors'])}" if r.get("errors") else ""))
        else:
            status = "FAIL" if r.get("errors") else "ok"
            lines.append(f"{status:4} {r['kind']:<8} {r['id']:<14} {len(r.get('paths') or {})} path(s) " + ", ".join(f"{p.split('/')[-1] or p}={w}" for p, w in (r.get("paths") or {}).items())
                         + (f"  {'; '.join(r['errors'])}" if r.get("errors") else ""))
    lines.append(f"\n{len(report['results'])} probed, {len(report['failed'])} failed" + (f": {', '.join(report['failed'])}" if report["failed"] else "")
                 + (f"; CRITICAL: {', '.join(report['critical'])}" if report["critical"] else ""))
    return "\n".join(lines)
