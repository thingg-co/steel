"""SourceManager: owns every venue, routes ticks to the active provider per symbol, merges the
startup backfill, and lets venues be toggled at runtime (persisted in state/sources.json)."""
from __future__ import annotations

import asyncio
import time

import json
from pathlib import Path

from .base import Source, SourceStatus, Tick
from .registry import REGISTRY
from .sim import SimSource


def enabled_by_default(cls, cfg) -> bool:
    """The out-of-the-box state of a venue: its `default_on`, minus `sources_off`; `sources_on`
    names an exact set instead (plus the simulator), or adds venues when written with a plus
    (`+bitget,+hyperliquid`)."""
    off = {s.strip() for s in str(getattr(cfg, "sources_off", "") or "").split(",") if s.strip()}
    on_list = [s.strip() for s in str(getattr(cfg, "sources_on", "") or "").split(",") if s.strip()]
    added = {s[1:] for s in on_list if s.startswith("+")}
    exact = {s for s in on_list if not s.startswith("+")}
    if cls.id == "sim":
        return True
    if exact:
        return cls.id in exact or cls.id in added
    return (cls.default_on or cls.id in added) and cls.id not in off


class SourceManager:
    """Owns every source, routes ticks to the active provider per symbol, and lets sources be
    toggled at runtime. A toggle is persisted to `state_path` (state/sources.json) and applied
    on the next start, so the panel's choices outlive a restart."""

    def __init__(self, cfg, symbols: list[str], on_tick, on_provider_change=None, trace=None, on_quote=None,
                 state_path: str | Path | None = None, on_book=None):
        self.cfg = cfg
        self.symbols = symbols
        self.deep_venue: dict[str, str] = {}        # name -> the venue its deep history pages from
        self.on_tick = on_tick
        self.on_quote = on_quote
        self.on_book = on_book                              # every live venue's bid/ask, active or standby, for the dislocation monitor
        self.identity_ok = None                             # (src_id, sym) -> bool: whether a venue may serve a name whose ticker several coins share
        self.on_provider_change = on_provider_change
        self.trace = trace
        self.state_path = Path(state_path) if state_path else None
        saved = self._saved()
        self.sources: dict[str, Source] = {}
        self.enabled: dict[str, bool] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        for cls in REGISTRY + [SimSource]:
            src = cls(cfg, symbols)
            self.sources[src.id] = src
            on = enabled_by_default(cls, cfg)
            if src.id in saved and src.id != "sim":
                on = bool(saved[src.id])
            self.enabled[src.id] = on and bool(src.symbols)
        self.seen: dict[str, dict[str, float]] = {s: {} for s in symbols}   # sym -> source_id -> last ts
        self.active: dict[str, str | None] = {s: None for s in symbols}

    def _backfill_timeout(self, n_symbols: int, cutoff: float) -> float:
        """How long one venue may take over a startup backfill: ten minutes, plus a second per
        name per day asked (Coinbase pages 403 names over 30 days in about 15 minutes)."""
        days = max(0.0, (time.time() - cutoff) / 86400.0)
        return 600.0 + float(n_symbols) * days

    def _saved(self) -> dict:
        if not self.state_path:
            return {}
        try:
            d = json.loads(self.state_path.read_text())
            return {str(k): bool(v) for k, v in (d.get("enabled") or {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"enabled": {k: v for k, v in self.enabled.items() if k != "sim"}}, indent=1))
        except OSError:
            pass

    def ordered(self) -> list[Source]:
        return sorted(self.sources.values(), key=lambda s: s.priority)

    def tradable(self, sym: str) -> bool:
        """Whether an enabled live venue lists the name: what the universe may promote."""
        return any(self.enabled.get(src.id) and src.id != "sim" and src.knows(sym) for src in self.sources.values())

    async def volumes(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        """Daily dollar volume per listed dollar pair across the enabled spot venues (the most
        any one venue reports), for the liquidity-floor universe. A venue that fails reports
        nothing; nothing raises."""
        srcs = [src for src in self.ordered() if self.enabled.get(src.id) and src.venue == "spot"]

        async def one(src):
            try:
                return await asyncio.wait_for(src.volumes(prices), timeout=120)
            except Exception as e:  # noqa: BLE001
                if self.trace:
                    self.trace.emit("feed", f"{src.name} volumes failed: {type(e).__name__}: {str(e)[:80]}", "warn")
                return {}
        out: dict[str, float] = {}
        for vols in await asyncio.gather(*[one(src) for src in srcs]):
            for sym, dv in (vols or {}).items():
                if "-" in sym and dv > out.get(sym, 0.0):
                    out[sym] = float(dv)
        return out

    def fiction(self, src_id: str, sym: str) -> bool:
        """Whether a tick from `src_id` for `sym` would be made-up: the simulator speaking for a
        name a live venue lists. The simulator's prices are a random walk from a seed (BTC 60,000,
        SOL 150, 10 for anything else); on the first GPU run it built every name's first live bar
        before the venues' sockets delivered, and in a CPU run it took INJ, UNI and LINK over for
        hours after a socket dropped, so the labels, the calibration and the paper book all saw
        prices no market printed. A name a live venue lists is served by a live venue or by
        nobody, which is what lets the stall alarm fire. A pure simulator run (nothing else
        enabled) is unaffected."""
        return self.sources[src_id].mechanism == "simulated" and self.tradable(sym)

    def provider_for(self, sym: str) -> str | None:
        now = time.time()
        for src in self.ordered():
            if not self.enabled.get(src.id) or not src.supports(sym):
                continue
            if self.identity_ok is not None and src.id != "sim" and not self.identity_ok(src.id, sym):
                continue
            if self.fiction(src.id, sym):
                continue
            seen = self.seen[sym].get(src.id)
            if seen is not None and now - seen <= src.fresh_after:
                return src.id
        return None

    def _route(self, src_id: str, tick: Tick) -> None:
        sym = tick.symbol
        if sym not in self.seen or self.fiction(src_id, sym):
            return
        self.seen[sym][src_id] = time.time()
        if self.on_book and tick.bid and tick.ask and self.sources[src_id].mechanism != "simulated":
            self.on_book(src_id, tick)
        active = self.provider_for(sym)
        if active != self.active.get(sym):
            prev = self.active.get(sym)
            self.active[sym] = active
            if self.on_provider_change:
                self.on_provider_change(sym, prev, active)
        if src_id == active:
            self.on_tick(tick)
        elif self.on_quote and tick.bid and tick.ask and self.sources[src_id].mechanism != "simulated" \
                and (active is None or self.sources[active].quote == self.sources[src_id].quote):
            # a live venue keeps the spread fresh even when another owns the price, as long as it quotes in the same
            # dollar: a USDT book sits a USDT basis off a USD one. The simulator's quotes are fiction.
            self.on_quote(tick.symbol, tick.bid, tick.ask)

    def _emitter(self, src_id: str):
        return lambda tick: self._route(src_id, tick)

    async def backfill(self, cutoff: float) -> tuple[list[Tick], dict[str, str]]:
        """Backfill each symbol from the richest enabled source, in two waves: every spot (and
        aggregate) venue concurrently, then the perp venues and the simulator only for the names
        still empty. A perp venue is a derivative and, on Hyperliquid, a rate budget that
        serializes its candle requests (two minutes for a 60-name universe): it seeds what no spot
        venue could serve and never slows the start otherwise."""
        active = [src for src in self.ordered()
                  if self.enabled.get(src.id) and any(src.supports(s) for s in self.symbols)]
        chosen: dict[str, str] = {}
        collected: dict[str, list[Tick]] = {s: [] for s in self.symbols}
        span: dict[str, int] = {s: 0 for s in self.symbols}

        async def _one(src, want):
            want = [s for s in want if src.supports(s)]
            if not want:
                return src.id, []
            try:
                return src.id, await asyncio.wait_for(src.backfill(want, cutoff), timeout=self._backfill_timeout(len(want), cutoff))
            except Exception as e:  # noqa: BLE001
                if self.trace:
                    self.trace.emit("feed", f"{src.name} backfill failed: {type(e).__name__}: {str(e)[:80]}", "warn")
                return src.id, []

        def _merge(results: dict) -> None:
            dt = float(self.cfg.bar_seconds)
            for src in self.ordered():
                ticks = results.get(src.id)
                if not ticks:
                    continue
                by_sym: dict[str, list[Tick]] = {}
                for t in ticks:
                    by_sym.setdefault(t.symbol, []).append(t)
                # keep the source with the RICHEST history per symbol: the one covering the most bars, in
                # priority order on a tie. Bars, not ticks: the simulator's six hours of synthetic trades
                # outnumbered Coinbase's five days of candles on the first smoke run and won every name.
                for s in self.symbols:
                    bars = len({int(t.ts // dt) for t in by_sym.get(s, [])})
                    if bars > span[s]:
                        collected[s] = by_sym[s]
                        span[s] = bars
                        chosen[s] = src.id
                        self.seen[s][src.id] = time.time()
                if self.trace:
                    self.trace.emit("feed", f"{src.name}: {len(ticks)} ticks for {len(by_sym)} symbols")

        # query every spot source concurrently, then merge in priority order so startup stays fast as the universe grows
        first = [src for src in active if src.venue not in ("perp", "simulated")]
        _merge(dict(await asyncio.gather(*[_one(src, self.symbols) for src in first])))
        missing = [s for s in self.symbols if span[s] == 0]
        second = [src for src in active if src not in first]
        if missing and second:
            if self.trace:
                self.trace.emit("feed", f"{len(missing)} names with no spot history: asking {', '.join(src.name for src in second)}")
            _merge(dict(await asyncio.gather(*[_one(src, missing) for src in second])))
        merged: list[Tick] = []
        for s in self.symbols:
            merged.extend(collected[s])
            self.active[s] = chosen.get(s)
        merged.sort(key=lambda t: t.ts)
        return merged, chosen

    async def backfill_range(self, cutoff: float, until: float) -> list[Tick]:
        """Bounded history page [cutoff, until) for the whole universe, each name from the first
        enabled source in priority order that pages by date range and lists it: Coinbase for its
        names, then Kraken's and Bybit's and so on for the rest, so a name only one venue trades
        gets history too. Quiet on failure so an API hiccup never disturbs the live feeds. The
        venue each name came from is kept in `deep_venue` for the Sources panel."""
        remaining = [s for s in self.symbols]
        out: list[Tick] = []
        for src in self.ordered():
            if not remaining:
                break
            if not self.enabled.get(src.id) or not hasattr(src, "backfill_range"):
                continue
            want = [s for s in remaining if src.supports(s) and src.knows(s)]
            if not want:
                continue
            try:
                ticks = await asyncio.wait_for(src.backfill_range(want, cutoff, until), timeout=600)
            except Exception as e:  # noqa: BLE001
                if self.trace:
                    self.trace.emit("feed", f"{src.name} deep backfill failed: {type(e).__name__}: {str(e)[:80]}", "warn")
                continue
            served = {t.symbol for t in ticks or ()}
            for sym in served:
                self.deep_venue[sym] = src.id
            remaining = [s for s in remaining if s not in served]
            out.extend(ticks or ())
        out.sort(key=lambda t: t.ts)
        return out

    def start(self) -> None:
        for src in self.sources.values():
            if self.enabled.get(src.id) and src.id not in self.tasks:
                self.tasks[src.id] = asyncio.create_task(self._supervise(src))

    async def _supervise(self, src: Source) -> None:
        try:
            await src.run(self._emitter(src.id))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            src.note = f"stopped: {type(e).__name__}"
            if self.trace:
                self.trace.emit("feed", f"{src.name} stopped: {type(e).__name__}: {str(e)[:80]}", "error")

    def toggle(self, src_id: str, on: bool) -> dict:
        src = self.sources.get(src_id)
        if not src:
            return {"error": "unknown source"}
        if on and not src.symbols:
            return {"error": f"{src.name} supports none of the active symbols"}
        self.enabled[src_id] = on
        if on:
            if src_id not in self.tasks:
                self.tasks[src_id] = asyncio.create_task(self._supervise(src))
        else:
            task = self.tasks.pop(src_id, None)
            if task:
                task.cancel()
            for s in self.symbols:                     # drop its freshness so failover is immediate
                self.seen[s].pop(src_id, None)
        for s in self.symbols:
            self.active[s] = self.provider_for(s)
        self._save()
        if self.trace:
            self.trace.emit("feed", f"{src.name} turned {'on' if on else 'off'}", "act")
        return {"ok": True}

    async def stop(self) -> None:
        for task in self.tasks.values():
            task.cancel()

    def status(self) -> list[dict]:
        owned: dict[str, list[str]] = {sid: [] for sid in self.sources}
        for s in self.symbols:
            a = self.active.get(s)
            if a:
                owned[a].append(s)
        out = []
        for src in self.ordered():
            st = SourceStatus(
                id=src.id, name=src.name, kind=src.kind, mechanism=src.mechanism,
                enabled=self.enabled.get(src.id, False), running=src.id in self.tasks,
                priority=src.priority, detail=src.note or "", supported=list(src.symbols),
                owned=owned.get(src.id, []), ticks=src.ticks,
                last_tick=src.last_tick, note=src.note)
            out.append(st.__dict__ | {"venue": src.venue, "quote": src.quote, "default_on": src.default_on, "url": src.URL})
        return out

    def provider_map(self) -> dict[str, str | None]:
        return {s: self.active.get(s) for s in self.symbols}
