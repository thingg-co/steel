"""The source contract: `Source`, its status record, the tick helpers and the shared
websocket / poll loops every venue builds on. Venues live in their own modules; the manager
in manager.py routes them."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import websockets

from ..feed import Tick

log = logging.getLogger(__name__)

UA = "steel/0.1 (research; https://github.com/thingg-co/steel)"
STABLES = {"USDT", "USDC", "USDS", "DAI", "PAX", "PYUSD", "USD1", "USDE", "FDUSD", "TUSD", "USDG", "USDP", "BUSD",
           "GUSD", "LUSD", "FRAX", "USDD", "RLUSD", "EURC", "EUROC", "EURT", "USDX", "SUSD", "CUSD", "UST"}
FIAT = {"EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "BRL", "MXN", "ARS", "TRY", "SGD", "HKD", "KRW", "INR", "NZD", "ZAR", "PLN", "SEK", "NOK", "DKK", "CZK"}


def asset_class(sym: str) -> str:
    """The vocabulary the dashboard's class toggles, the universe and the tests share: "stable"
    for dollar (and euro) stablecoins, "fiat" for a fiat currency quoted against the dollar
    (Coinbase lists EUR-USD and GBP-USD as USD products), "crypto" for everything else. Only
    crypto is ever modeled."""
    base = base_of(sym)
    return "stable" if base in STABLES else "fiat" if base in FIAT else "crypto"


def base_of(sym: str) -> str:
    return str(sym).split("-")[0].upper()


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Base source ----------------------------------------------------------------------

@dataclass
class SourceStatus:
    id: str
    name: str
    kind: str            # crypto
    mechanism: str       # websocket | poll | simulated
    enabled: bool
    running: bool
    priority: int
    detail: str
    supported: list[str]
    owned: list[str]
    ticks: int
    last_tick: float | None
    note: str | None


class Source:
    """One venue. Subclasses set the class attributes, map canonical BASE-USD names to the venue's
    spelling, stream ticks in `run`, and serve 5-minute history in `backfill` (and in
    `backfill_range` when the venue pages history by date range, which the deep backfill uses).

    `venue` says what the price is: "spot" (the asset itself), "perp" (a perpetual future: funding
    keeps it near spot, but it is a derivative, so perp venues rank below spot ones), "aggregate"
    (an index of venues, quote-only) or "simulated". `default_on` is the out-of-the-box state;
    every other venue is present but off until toggled, so a box in a region a venue blocks (or
    one that wants two websockets, not twenty) starts clean.
    """
    id = "base"
    name = "Base"
    kind = "crypto"
    mechanism = "poll"
    venue = "spot"                          # spot | perp | aggregate | simulated
    quote = "USD"                           # what the venue's dollar pairs are quoted in: USD | USDT | USDC. A USDT venue's
                                            # bid/ask sits a USDT basis (10 bps on the first smoke run) off a USD venue's, so the
                                            # manager only lets a standby venue refresh the spread when its quote matches the active one's
    default_on = False
    priority = 100
    poll_interval = 15.0
    fresh_after = 40.0                      # a symbol is "live" if seen within this many seconds
    URL = ""                                # the venue's site, for the Sources panel

    def __init__(self, cfg, symbols: list[str]):
        self.cfg = cfg
        self.all_symbols = symbols
        self._unsupported: set[str] = set()   # names the venue rejected (learned from a 4xx), never retried
        self.symbols = [s for s in symbols if self.supports(s)]
        self.ticks = 0
        self.last_tick: float | None = None
        self.note: str | None = None

    def supports(self, sym: str) -> bool:
        return "-" in sym and sym not in self._unsupported

    def knows(self, sym: str) -> bool:
        """Whether the venue is known to list this name (a product list, when the source has one)."""
        return self.supports(sym)

    async def backfill(self, symbols: list[str], cutoff: float) -> list[Tick]:
        return []

    async def volumes(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        """Daily dollar volume for every dollar pair the venue lists (canonical name -> USD a
        day), for the liquidity-floor universe. `prices` (canonical -> last price) helps a venue
        whose stats come in base units. Empty when the venue has no such endpoint."""
        return {}

    async def run(self, emit) -> None:
        """Stream ticks for self.symbols via emit(tick). Override per source."""
        raise NotImplementedError

    def _emit(self, emit, tick: Tick) -> None:
        self.ticks += 1
        self.last_tick = time.time()
        emit(tick)

    async def _poll_loop(self, emit) -> None:
        while True:
            try:
                await self.poll_once(emit)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.note = f"{type(e).__name__}: {str(e)[:80]}"
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self, emit) -> None:
        raise NotImplementedError

    async def _ws_loop(self, emit, url: str, subscribe, handle, *, ping=None, ping_every: float = 20.0,
                       decode=None, reply=None, ping_interval: float | None = 20) -> None:
        """Keep a websocket open: `subscribe(ws)` after each connect, `handle(msg, emit)` per
        message, an optional `ping(ws)` coroutine every `ping_every` seconds for venues that want
        an application-level ping. `decode(raw)` replaces json.loads for venues that compress or
        frame their messages (return None to skip one); `reply(msg) -> str | None` answers a
        server heartbeat the venue requires (the string is sent back as is). `ping_interval`
        is the protocol-level ping (None turns it off for venues that reject control frames).
        Reconnects with exponential backoff; never raises."""
        backoff = 1.0
        decode = decode or json.loads
        while True:
            try:
                async with websockets.connect(url, ping_interval=ping_interval, max_queue=8192, open_timeout=20,
                                              user_agent_header=UA) as ws:
                    await subscribe(ws)
                    backoff = 1.0
                    pinger = asyncio.create_task(self._pinger(ws, ping, ping_every)) if ping else None
                    try:
                        async for raw in ws:
                            try:
                                msg = decode(raw)
                            except (ValueError, KeyError, TypeError, OSError, EOFError):
                                continue
                            if msg is None:
                                continue
                            if reply is not None:
                                try:
                                    answer = reply(msg)
                                except (ValueError, KeyError, TypeError, AttributeError):
                                    answer = None
                                if answer:
                                    await ws.send(answer)
                            try:
                                handle(msg, emit)
                            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                                continue
                    finally:
                        if pinger:
                            pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                code = getattr(getattr(e, "response", None), "status_code", None)
                self.note = f"ws {type(e).__name__}{f' {code}' if code else ''}; reconnecting"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    @staticmethod
    async def _pinger(ws, ping, every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await ping(ws)
            except Exception:  # noqa: BLE001
                return


def _candle_tick(sym: str, t: float, o, h, l, c, v) -> Tick | None:  # noqa: E741
    try:
        c0 = float(c)
        if c0 <= 0:
            return None
        return Tick(sym, float(t), c0, float(v or 0.0), None,
                    o=float(o or c0), h=float(h or c0), l=float(l or c0))
    except (TypeError, ValueError):
        return None


def quote_tick(sym: str, price, bid=None, ask=None, ts: float | None = None, bid_size=None, ask_size=None) -> Tick | None:
    """A quote-only tick (updates price and spread, never a candle): the price, or the mid when
    only bid and ask are known; top-of-book sizes when the venue sends them. None when nothing
    usable came."""
    try:
        b = float(bid) if bid not in (None, "", 0, "0") else None
        a = float(ask) if ask not in (None, "", 0, "0") else None
        px = float(price) if price not in (None, "", 0, "0") else ((b + a) / 2 if b and a else 0.0)
        if px <= 0:
            return None
        bs = float(bid_size) if bid_size not in (None, "", 0, "0") else None
        as_ = float(ask_size) if ask_size not in (None, "", 0, "0") else None
        return Tick(sym, float(ts) if ts else time.time(), px, 0.0, None, b, a, quote=True, bid_size=bs, ask_size=as_)
    except (TypeError, ValueError):
        return None


def trade_tick(sym: str, price, size, taker_buy, ts: float | None = None) -> Tick | None:
    """A real trade (builds candles). None when the price or size is not positive."""
    try:
        px, qty = float(price), float(size)
        if px <= 0 or qty <= 0:
            return None
        return Tick(sym, float(ts) if ts else time.time(), px, qty, taker_buy)
    except (TypeError, ValueError):
        return None


SCALE_PREFIXES = (("1000000", 1e6), ("1M", 1e6), ("1000", 1e3), ("k", 1e3))


def unscale(name: str) -> tuple[str, float]:
    """A venue's small-price contract to the base and its unit: Bybit's 1000PEPE and Hyperliquid's
    kPEPE quote a thousand tokens, so the price is divided by the factor and the size multiplied
    before a tick is emitted. ("PEPE", 1000.0) for either; ("BTC", 1.0) for a plain name."""
    n = str(name or "")
    for prefix, factor in SCALE_PREFIXES:
        rest = n[len(prefix):]
        if n.startswith(prefix) and len(rest) >= 2 and rest[0].isupper():
            return rest, factor
    return n, 1.0


async def gather_symbols(syms: list[str], fetch, *, limit: int = 4, note=None) -> list[Tick]:
    """Run `fetch(sym)` for every symbol with at most `limit` in flight and flatten the ticks; a
    failure on one name is noted (via `note(text)`) and never fails the others. The shape every
    venue's `backfill` / `backfill_range` has."""
    sem = asyncio.Semaphore(max(1, limit))

    async def one(s):
        async with sem:
            try:
                return await fetch(s)
            except Exception as e:  # noqa: BLE001
                if note:
                    note(f"{s}: {type(e).__name__}: {str(e)[:60]}")
                return []
    groups = await asyncio.gather(*[one(s) for s in syms])
    return [t for g in groups for t in g]


def client(timeout: float = 25.0, **kw) -> httpx.AsyncClient:
    """An HTTP client with Steel's user agent. Venues that key their rate limits on it get a
    name to write to."""
    headers = {"User-Agent": UA} | dict(kw.pop("headers", {}) or {})
    return httpx.AsyncClient(timeout=timeout, headers=headers, **kw)
