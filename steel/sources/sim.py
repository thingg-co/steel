"""The simulator: a fallback of last resort, never a peer of a live venue."""
from __future__ import annotations

import asyncio
import time

from ..feed import SimFeed
from .base import Source


# Simulator ------------------------------------------------------------------------

class SimSource(Source):
    id = "sim"
    name = "Simulator"
    mechanism = "simulated"
    venue = "simulated"
    default_on = True
    priority = 200            # lowest: only owns a symbol when every live source is down
    fresh_after = 10.0

    def __init__(self, cfg, symbols):
        super().__init__(cfg, symbols)
        self.feed = SimFeed(self.symbols) if self.symbols else None

    SIM_BACKFILL_S = 6 * 3600.0       # enough bars to fill the model window; the live venues serve real history
    SIM_BACKFILL_STEP = 5.0           # coarse steps for history: 6 h is 4,320 steps a name, not 86,400

    async def backfill(self, symbols, cutoff):
        """A short synthetic history, in a worker thread: the manager keeps the richest history per
        name, so this only ever seeds a name no live venue could serve. Five days at the live
        step for a whole universe took minutes of CPU inside the event loop and held the server
        off its port (the first Steel smoke run, 2026-10-11)."""
        syms = [s for s in symbols if self.supports(s)]
        if not syms:
            return []
        seconds = min(self.SIM_BACKFILL_S, time.time() - cutoff if cutoff > 1e6 else float(self.cfg.backfill_seconds))
        feed = SimFeed(syms, step=self.SIM_BACKFILL_STEP)
        return await asyncio.to_thread(lambda: asyncio.run(feed.backfill(seconds)))

    async def run(self, emit):
        if not self.feed:
            return
        async for tick in self.feed.stream():
            self._emit(emit, tick)
