"""The cross-venue arbitrage executor: act on a dislocation while it is open.

Off unless `arb` is on, and every leg goes through the execution gate like any other order
(execution on, both venues armed with their own trade keys, every cap set, no kill switch).
When the dislocation monitor opens an episode whose net edge (after both taker fees and the
latency margin) clears `arb_min_edge_bps`, the executor sends two legs at once: a buy on the
venue with the low ask and a sell on the venue with the high bid, each `arb_max_order_usd` or
the smaller of the two top-of-book sizes, whichever is less. Inventory is the operator's:
spot legs need the base on the selling venue and cash on the buying venue (checked against the
readers' last balances when known; refused when they say no), perp legs need collateral on
both; a perp pair is closed with two reduce-only orders when the episode closes, a spot pair
moved the inventory and stays (rebalance off-cycle). A leg that filled while the other failed
is an unhedged position: it is flattened at once with a reduce-only order on the same venue,
and the incident is counted. Every attempt is logged to state/arb.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .execution import Order
from .sources import base_of
from .sources.fees import transfer_cost

CASHLIKE = {"USD", "USDT", "USDC"}


class ArbExecutor:
    def __init__(self, cfg, executors: dict, gate, *, venue_kind, state_dir: str | Path = "state", trace=None):
        self.cfg = cfg
        self.executors = executors
        self.gate = gate
        self.venue_kind = venue_kind                 # venue id -> "spot" | "perp"
        self.trace = trace
        self.path = Path(state_dir) / "arb.jsonl"
        self.portfolio: dict | None = None            # the readers' last balances, for the inventory check
        self.pairs: dict[tuple, dict] = {}            # (sym, quote, kind) -> the open perp pair to unwind
        self.cooldown: dict[str, float] = {}          # sym -> when it may be tried again
        self.stats = {"considered": 0, "sent": 0, "filled_pairs": 0, "refused": 0, "unhedged": 0, "closed_pairs": 0, "edge_bps_sum": 0.0}
        self.recent: list[dict] = []
        self._tasks: set = set()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "arb", False)) and self.gate.enabled

    def _say(self, text: str, level: str = "act") -> None:
        if self.trace:
            self.trace.emit("system", text, level)

    def _log(self, rec: dict) -> None:
        self.recent = (self.recent + [rec])[-100:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    # Inventory ---------------------------------------------------------------------
    def _balance(self, venue: str, asset: str) -> float | None:
        """What the readers last saw on a venue for an asset (cash counts USD, USDT and USDC
        together); None when the venue has not reported."""
        for a in (self.portfolio or {}).get("accounts") or []:
            if a.get("id") != venue:
                continue
            if asset in CASHLIKE:
                return float(a.get("cash") or 0.0)
            return sum(float(p.get("qty") or 0.0) for p in a.get("positions") or [] if p.get("asset") == asset and p.get("kind", "spot") == "spot")
        return None

    def _funded(self, sym: str, buy_v: str, sell_v: str, qty: float, notional: float) -> str:
        """Why the legs cannot be funded, or "". Inventory has to be on both venues already: a
        missing balance means an on-chain transfer first, and the reason says what that would
        cost and how long the dislocation would have to last."""
        base = base_of(sym)
        px = notional / qty if qty > 0 else 0.0
        if self.venue_kind.get(sell_v) == "spot":
            have = self._balance(sell_v, base)
            if have is not None and have < qty:
                return f"only {have:g} {base} on {sell_v} to sell {qty:g}; " + self._move(base, qty - have, px, notional, sell_v)
        if self.venue_kind.get(buy_v) == "spot":
            cash = self._balance(buy_v, "USD")
            if cash is not None and cash < notional:
                return f"only {cash:,.0f} cash on {buy_v} to buy {notional:,.0f}; " + self._move(self._stable(), notional - cash, 1.0, notional, buy_v)
        for v in (buy_v, sell_v):
            if self.venue_kind.get(v) == "perp":
                cash = self._balance(v, "USD")
                need = notional / max(1.0, float(getattr(self.cfg, "short_leverage", 2.0) or 2.0))
                if cash is not None and cash < need:
                    return f"only {cash:,.0f} collateral on {v}; " + self._move(self._stable(), need - cash, 1.0, notional, v)
        return ""

    def _stable(self) -> str:
        return str(getattr(self.cfg, "transfer_asset", "USDC") or "USDC").upper()

    @staticmethod
    def _move(asset: str, qty: float, px: float, notional: float, venue: str) -> str:
        t = transfer_cost(asset, qty, px)
        return (f"moving {qty:,.4g} {asset} to {venue} on {t.rail} costs {t.fee_usd:,.2f} USD ({t.bps(notional):.1f} bps of the trade) "
                f"and ~{t.minutes:.0f} min; the edge would have to outlast that")

    # The decision ------------------------------------------------------------------
    def consider(self, ep: dict, books: dict) -> None:
        """Called by the monitor when an episode opens: decide and dispatch (never blocks)."""
        if not self.enabled:
            return
        self.stats["considered"] += 1
        why = self._why_not(ep, books)
        if why:
            self.stats["refused"] += 1
            self._log({"t": time.time(), "event": "skip", "sym": ep["sym"], "buy": ep["buy"], "sell": ep["sell"], "edge_bps": ep["peak_bps"], "why": why})
            return
        task = asyncio.get_running_loop().create_task(self._open(ep, books))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _why_not(self, ep: dict, books: dict) -> str:
        c = self.cfg
        if ep["peak_bps"] < float(getattr(c, "arb_min_edge_bps", 5.0) or 5.0):
            return f"edge {ep['peak_bps']:.1f} bps under the {float(getattr(c, 'arb_min_edge_bps', 5.0)):g} bps minimum"
        if time.time() < self.cooldown.get(ep["sym"], 0.0):
            return "cooling down on this name"
        if len(self.pairs) >= int(getattr(c, "arb_max_open", 3) or 3) and ep["kind"] == "perp":
            return "too many pairs open"
        for v in (ep["buy"], ep["sell"]):
            if v not in self.executors:
                return f"{v} has no order client"
            if v not in self.gate.armed:
                return f"{v} is not armed"
        if self.venue_kind.get(ep["buy"]) != self.venue_kind.get(ep["sell"]):
            return "a spot leg against a perp leg is a basis trade, not an arbitrage"
        return ""

    def _size(self, ep: dict, books: dict) -> tuple[float, float]:
        """(qty, notional): the cap in dollars, the top-of-book sizes when known, at the mid."""
        bq, aq = books.get(ep["sell"]) or {}, books.get(ep["buy"]) or {}
        mid = ((bq.get("bid") or 0) + (aq.get("ask") or 0)) / 2.0
        if mid <= 0:
            return 0.0, 0.0
        usd = float(getattr(self.cfg, "arb_max_order_usd", 200.0) or 200.0)
        for q, side in ((bq, "bid_size"), (aq, "ask_size")):
            if q.get(side):
                usd = min(usd, float(q[side]) * mid)
        return usd / mid, usd

    async def _open(self, ep: dict, books: dict) -> None:
        sym, buy_v, sell_v, kind = ep["sym"], ep["buy"], ep["sell"], ep["kind"]
        qty, notional = self._size(ep, books)
        if qty <= 0:
            return
        fund = self._funded(sym, buy_v, sell_v, qty, notional)
        self.cooldown[sym] = time.time() + float(getattr(self.cfg, "arb_cooldown_s", 60.0) or 60.0)
        if fund:
            self.stats["refused"] += 1
            self._log({"t": time.time(), "event": "unfunded", "sym": sym, "buy": buy_v, "sell": sell_v, "edge_bps": ep["peak_bps"], "why": fund})
            return
        aq, bq = books.get(buy_v) or {}, books.get(sell_v) or {}
        tag = f"arb-{int(time.time())}-{base_of(sym)}"
        legs = [Order(buy_v, sym, "buy", qty, float(aq.get("ask") or 0) or None, kind, False, notional, f"{tag}-b", "arb buy leg"),
                Order(sell_v, sym, "sell", qty, float(bq.get("bid") or 0) or None, kind, False, notional, f"{tag}-s", "arb sell leg")]
        self.stats["sent"] += 2
        results = await asyncio.gather(*[self.executors[o.venue].place(o) for o in legs], return_exceptions=True)
        res = [r if not isinstance(r, Exception) else None for r in results]
        ok = [bool(r and r.ok) for r in res]
        rec = {"t": time.time(), "event": "open", "sym": sym, "buy": buy_v, "sell": sell_v, "kind": kind, "qty": qty, "notional": notional,
               "edge_bps": ep["peak_bps"], "legs": [{"venue": o.venue, "side": o.side, "ok": bool(r and r.ok), "status": r.status if r else "error",
                                                    "error": (r.error if r else "exception"), "order_id": r.order_id if r else ""} for o, r in zip(legs, res)]}
        if all(ok):
            self.stats["filled_pairs"] += 1
            self.stats["edge_bps_sum"] += float(ep["peak_bps"])
            if kind == "perp":
                self.pairs[(sym, ep["quote"], kind)] = {"sym": sym, "buy": buy_v, "sell": sell_v, "qty": qty, "opened": time.time(), "edge_bps": ep["peak_bps"]}
            self._say(f"arb: {base_of(sym)} bought on {buy_v}, sold on {sell_v}, {notional:,.0f} USD at {ep['peak_bps']:+.1f} bps net", "act")
        elif any(ok):
            self.stats["unhedged"] += 1
            filled = legs[ok.index(True)]
            flat = Order(filled.venue, sym, "sell" if filled.side == "buy" else "buy", qty, None, kind, True, notional, f"{tag}-flat", "arb flatten")
            r = await self.executors[filled.venue].place(flat)
            rec["flatten"] = {"venue": flat.venue, "ok": r.ok, "status": r.status, "error": r.error}
            self._say(f"arb: UNHEDGED {base_of(sym)} leg on {filled.venue} (the other leg failed); flattened: {r.status} {r.error}", "error")
        self._log(rec)

    def on_close(self, ep: dict) -> None:
        """The monitor closed an episode: an open perp pair on that name is unwound."""
        key = (ep["sym"], ep["quote"], ep["kind"])
        pair = self.pairs.pop(key, None)
        if not pair or not self.enabled:
            return
        task = asyncio.get_running_loop().create_task(self._close(pair))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close(self, pair: dict) -> None:
        sym, qty = pair["sym"], pair["qty"]
        tag = f"arb-{int(time.time())}-{base_of(sym)}-x"
        legs = [Order(pair["buy"], sym, "sell", qty, None, "perp", True, 0.0, f"{tag}-b", "arb unwind"),
                Order(pair["sell"], sym, "buy", qty, None, "perp", True, 0.0, f"{tag}-s", "arb unwind")]
        for o in legs:
            o.notional_usd = qty * (pair.get("mark") or 1.0)
        results = await asyncio.gather(*[self.executors[o.venue].place(o) for o in legs], return_exceptions=True)
        res = [r if not isinstance(r, Exception) else None for r in results]
        self.stats["closed_pairs"] += 1
        self._log({"t": time.time(), "event": "close", "sym": sym, "qty": qty, "held_s": round(time.time() - pair["opened"], 1),
                   "legs": [{"venue": o.venue, "ok": bool(r and r.ok), "status": r.status if r else "error", "error": r.error if r else "exception"} for o, r in zip(legs, res)]})

    def status(self) -> dict:
        s = dict(self.stats)
        s["mean_edge_bps"] = round(s["edge_bps_sum"] / s["filled_pairs"], 2) if s["filled_pairs"] else None
        return {"enabled": self.enabled, "switch": bool(getattr(self.cfg, "arb", False)), "min_edge_bps": float(getattr(self.cfg, "arb_min_edge_bps", 5.0) or 5.0),
                "max_order_usd": float(getattr(self.cfg, "arb_max_order_usd", 200.0) or 200.0), "open_pairs": list(self.pairs.values()),
                "stats": s, "recent": self.recent[-20:]}
