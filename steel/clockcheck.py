"""The box's clock against the venues': a signed request carries a timestamp the venue
rejects when it is more than a second or so off, and a stale clock also skews every bar. The
offset is the median of the venues that answer (Binance, Bybit, Coinbase, no key needed); the
gate refuses every order while it is past `clock_max_offset_s`."""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx

SOURCES = [("binance", "https://api.binance.com/api/v3/time", lambda d: float(d["serverTime"]) / 1000.0),
           ("bybit", "https://api.bybit.com/v5/market/time", lambda d: float(d["result"]["timeSecond"])),
           ("coinbase", "https://api.exchange.coinbase.com/time", lambda d: float(d["epoch"]))]


async def _one(client: httpx.AsyncClient, name: str, url: str, parse) -> tuple[str, float] | None:
    t0 = time.time()
    try:
        r = await client.get(url)
        d = r.json()
        server = parse(d)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        return None
    t1 = time.time()
    return name, (t0 + t1) / 2.0 - server            # local mid-request minus server time: positive = the box runs fast


async def measure(timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> dict:
    """{offset_s, venues: {name: offset}, checked}; offset_s is None when no venue answered."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "steel/1.0"})
    try:
        res = await asyncio.gather(*(_one(client, n, u, p) for n, u, p in SOURCES))
    finally:
        if own:
            await client.aclose()
    got = {n: round(o, 3) for n, o in (r for r in res if r)}
    return {"offset_s": round(statistics.median(got.values()), 3) if got else None, "venues": got, "checked": time.time()}
