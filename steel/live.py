"""The live book: the paper book's trades mirrored to real venues, once promoted, and the rule
that decides whether it may be.

Promotion is never automatic. `Promotion.check` reads the weekend backtest's totals and the
paper book's record and says whether the thresholds are met (scored days and net bps per day
and hit rate in the backtest; days running, Sharpe, drawdown and trust on the paper book); the
operator promotes from the Ops tab, and a promotion that fails the checklist needs `force`.
Demotion is automatic: a kill switch, the live drawdown passing `live_max_drawdown`, or the
reconciled positions drifting more than `live_drift_tol` from what was sent demotes the book
back to paper at once.

While live, every paper fill becomes an order of `live_scale` times the paper notional through
the execution client of the venue that side trades on (`trade_venue_spot` for spot,
`trade_venue_perp` for perps), through the execution gate like any order. The book keeps what
it expects to hold per name, reconciles that against the venue's own positions from the
read-only readers every minute, and reports slippage (the venue's average fill against the
paper fill). State: state/live.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .execution import Order
from .sources import base_of


class Promotion:
    """The checklist. Each row: name, whether it passes, the value, the threshold."""

    def __init__(self, cfg):
        self.cfg = cfg

    def check(self, backtest: dict | None, paper: dict | None, trusted: bool) -> dict:
        c = self.cfg
        bt = (backtest or {}).get("totals") or {}
        pp = paper or {}
        days_bt = int(bt.get("days") or len((backtest or {}).get("days") or []))
        net_day = bt.get("net_bps_per_day")
        hit = bt.get("hit_rate")
        started = pp.get("started")
        paper_days = (time.time() - float(started)) / 86400.0 if started else 0.0
        sharpe = pp.get("sharpe")
        dd = float(pp.get("max_drawdown") or 0.0) / 100.0
        ret = float(pp.get("return_pct") or 0.0) / 100.0
        rows = [
            {"name": "model trusted", "ok": bool(trusted), "value": "yes" if trusted else "no", "need": "trusted"},
            {"name": "backtest scored days", "ok": days_bt >= int(c.live_min_backtest_days), "value": days_bt, "need": f">= {int(c.live_min_backtest_days)}"},
            {"name": "backtest net bps / day", "ok": net_day is not None and float(net_day) >= float(c.live_min_backtest_net_bps),
             "value": None if net_day is None else round(float(net_day), 2), "need": f">= {float(c.live_min_backtest_net_bps):g}"},
            {"name": "backtest hit rate", "ok": hit is not None and float(hit) >= float(c.live_min_hit),
             "value": None if hit is None else round(float(hit), 3), "need": f">= {float(c.live_min_hit):g}"},
            {"name": "paper days running", "ok": paper_days >= float(c.live_min_paper_days), "value": round(paper_days, 1), "need": f">= {float(c.live_min_paper_days):g}"},
            {"name": "paper Sharpe (per bar)", "ok": sharpe is not None and float(sharpe) >= float(c.live_min_paper_sharpe),
             "value": None if sharpe is None else round(float(sharpe), 3), "need": f">= {float(c.live_min_paper_sharpe):g}"},
            {"name": "paper max drawdown", "ok": dd <= float(c.live_max_drawdown), "value": f"{dd * 100:.1f}%", "need": f"<= {float(c.live_max_drawdown) * 100:g}%"},
            {"name": "paper return", "ok": ret > 0, "value": f"{ret * 100:+.2f}%", "need": "> 0"},
        ]
        return {"eligible": all(r["ok"] for r in rows), "rows": rows}


class LiveBook:
    def __init__(self, cfg, executors: dict, gate, state_dir: str | Path = "state", trace=None):
        self.cfg = cfg
        self.executors = executors
        self.gate = gate
        self.trace = trace
        self.path = Path(state_dir) / "live.json"
        self.live = False
        self.since: float | None = None
        self.demoted: str = ""
        self.expected: dict[str, float] = {}          # sym -> signed base qty the orders sent should have left us with
        self.kinds: dict[str, str] = {}                # sym -> spot | perp, the venue class the position lives on
        self.heartbeat = 0.0                           # when the book last saved: the dead-man rule reads it at a start
        self.orders: list[dict] = []                   # the last 200 results, newest last
        self.slippage: list[float] = []                # bps, venue fill against the paper fill
        self.peak_equity = 0.0
        self.equity = 0.0
        self.drift: dict[str, float] = {}
        self.last_reconciled = 0.0
        self._load()

    # State -----------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.live = bool(d.get("live"))
        self.since = d.get("since")
        self.demoted = str(d.get("demoted") or "")
        self.expected = {k: float(v) for k, v in (d.get("expected") or {}).items()}
        self.kinds = {k: str(v) for k, v in (d.get("kinds") or {}).items()}
        self.heartbeat = float(d.get("heartbeat") or 0.0)
        self.peak_equity = float(d.get("peak_equity") or 0.0)
        self.slippage = [float(x) for x in (d.get("slippage") or [])][-500:]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat = time.time()
            self.path.write_text(json.dumps({"live": self.live, "since": self.since, "demoted": self.demoted, "expected": self.expected,
                                             "kinds": self.kinds, "heartbeat": self.heartbeat,
                                             "peak_equity": self.peak_equity, "slippage": self.slippage[-500:]}))
        except OSError:
            pass

    def _say(self, text: str, level: str = "act") -> None:
        if self.trace:
            self.trace.emit("system", text, level)

    def promote(self, checklist: dict, force: bool = False) -> dict:
        if self.live:
            return {"ok": True, "live": True}
        if not self.gate.enabled:
            return {"error": "execution is off (STEEL_EXECUTION=1)"}
        if not self.gate.armed:
            return {"error": "no venue is armed"}
        if not checklist.get("eligible") and not force:
            failing = [r["name"] for r in checklist.get("rows", []) if not r["ok"]]
            return {"error": "not eligible: " + ", ".join(failing)}
        self.live, self.since, self.demoted = True, time.time(), ""
        self.peak_equity = self.equity
        self.save()
        self._say(f"LIVE: the paper book's trades now mirror to the armed venues at {float(self.cfg.live_scale):g}x paper notional"
                  + (" (forced past the checklist)" if force and not checklist.get("eligible") else ""), "error")
        return {"ok": True, "live": True}

    def demote(self, reason: str) -> dict:
        was = self.live
        self.live, self.demoted = False, reason
        self.save()
        if was:
            self._say(f"demoted to paper: {reason}", "error")
        return {"ok": True, "live": False, "reason": reason}

    # Mirroring -------------------------------------------------------------------------
    def _venue_for(self, kind: str) -> str:
        return str(getattr(self.cfg, "trade_venue_perp" if kind == "perp" else "trade_venue_spot", "") or "").lower()

    @staticmethod
    def _order_of(trade: dict, scale: float, venue: str) -> Order | None:
        """A paper fill as an order: buy/sell spot, open/close long/short perps; liquidations are
        the venue's own doing and nothing to send."""
        side_txt = str(trade.get("side") or "")
        qty = float(trade.get("shares") or 0.0) * scale
        px = float(trade.get("price") or 0.0)
        if qty <= 0 or px <= 0 or side_txt == "liquidated":
            return None
        kind = "spot" if side_txt in ("buy", "sell") else "perp"
        reduce = side_txt.startswith("close")
        side = "buy" if side_txt in ("buy", "open long", "close short") else "sell"
        return Order(venue, str(trade["sym"]), side, qty, None, kind, reduce, notional_usd=qty * px,
                     client_id=f"steel-{int(float(trade.get('t') or time.time()))}-{base_of(str(trade['sym']))}"[:32], note=side_txt)

    async def mirror(self, trades: list[dict]) -> list[dict]:
        """Send the orders the paper fills imply (newest paper trades first in the deque, so
        they are replayed oldest first). Returns the results, which the Ops tab shows."""
        if not self.live or not trades:
            return []
        scale = float(getattr(self.cfg, "live_scale", 0.0) or 0.0)
        out = []
        for tr in reversed(list(trades)):
            kind = "spot" if str(tr.get("side")) in ("buy", "sell") else "perp"
            venue = self._venue_for(kind)
            order = self._order_of(tr, scale, venue)
            if order is None:
                continue
            ex = self.executors.get(venue)
            if ex is None or venue not in self.gate.armed:
                res = {"ok": False, "venue": venue, "status": "refused", "error": f"{venue} has no order client or is not armed", "order": order.__dict__}
                out.append(res)
                continue
            r = await ex.place(order, leverage=float(getattr(self.cfg, "long_leverage" if order.side == "buy" and kind == "perp" else "short_leverage", 1.0) or 1.0))
            rec = {"ok": r.ok, "venue": venue, "status": r.status, "order_id": r.order_id, "error": r.error, "filled": r.filled_qty,
                   "avg_price": r.avg_price, "paper_price": float(tr.get("price") or 0.0), "t": r.t, "order": order.__dict__}
            if r.ok:
                signed = order.qty if order.side == "buy" else -order.qty
                self.expected[order.symbol] = round(self.expected.get(order.symbol, 0.0) + signed, 10)
                self.kinds[order.symbol] = kind
                if abs(self.expected[order.symbol]) < 1e-9:
                    self.expected.pop(order.symbol, None)
                    self.kinds.pop(order.symbol, None)
                if r.avg_price and rec["paper_price"]:
                    slip = (float(r.avg_price) / rec["paper_price"] - 1.0) * 1e4 * (1 if order.side == "buy" else -1)
                    self.slippage.append(round(slip, 2))
            out.append(rec)
        self.orders = (self.orders + out)[-200:]
        self.save()
        return out

    # Panic -----------------------------------------------------------------------------
    def stale(self, now: float | None = None) -> float:
        """Minutes since the book last saved (the dead-man rule's reading)."""
        return ((time.time() if now is None else now) - self.heartbeat) / 60.0 if self.heartbeat else 0.0

    async def cancel_open(self) -> list[dict]:
        """Cancel every open order on each armed, configured venue. Returns what was cancelled."""
        out = []
        for vid in sorted(self.gate.armed):
            ex = self.executors.get(vid)
            if ex is None or not ex.configured:
                continue
            try:
                opens = await ex.open_orders()
            except Exception as e:  # noqa: BLE001
                out.append({"venue": vid, "ok": False, "error": f"open_orders: {type(e).__name__}"})
                continue
            for o in opens or []:
                r = await ex.cancel(str(o.get("order_id") or o.get("id") or ""), str(o.get("symbol") or ""))
                out.append({"venue": vid, "ok": r.ok, "order_id": r.order_id, "status": r.status, "error": r.error})
        return out

    async def flatten(self, prices: dict | None = None, reason: str = "operator") -> dict:
        """Close everything: cancel the open orders, then a reduce-only market order against
        each expected position, then demote. The paper book is untouched."""
        cancelled = await self.cancel_open()
        closed = []
        for sym, qty in list(self.expected.items()):
            if abs(qty) < 1e-12:
                continue
            kind = self.kinds.get(sym, "perp")
            venue = self._venue_for(kind)
            px = float((prices or {}).get(sym, {}).get("price") or 0.0) if isinstance((prices or {}).get(sym), dict) else float((prices or {}).get(sym) or 0.0)
            order = Order(venue, sym, "sell" if qty > 0 else "buy", abs(qty), kind=kind, reduce_only=True,
                          notional_usd=abs(qty) * px, note=f"flatten: {reason}")
            ex = self.executors.get(venue)
            if ex is None or venue not in self.gate.armed:
                closed.append({"ok": False, "venue": venue, "status": "refused", "error": f"{venue} has no order client or is not armed", "order": order.__dict__})
                continue
            r = await ex.place(order, leverage=1.0)
            rec = {"ok": r.ok, "venue": venue, "status": r.status, "order_id": r.order_id, "error": r.error, "filled": r.filled_qty,
                   "avg_price": r.avg_price, "paper_price": px, "t": r.t, "order": order.__dict__}
            if r.ok:
                self.expected.pop(sym, None)
                self.kinds.pop(sym, None)
            closed.append(rec)
        self.orders = (self.orders + closed)[-200:]
        left = len(self.expected)
        self.demote(f"flattened ({reason})" + (f"; {left} position(s) could not be closed" if left else ""))
        self._say(f"FLATTEN ({reason}): {len(cancelled)} order(s) cancelled, {sum(1 for c in closed if c['ok'])} of {len(closed)} position(s) closed", "error")
        return {"ok": left == 0, "cancelled": cancelled, "closed": closed, "left": sorted(self.expected)}

    # Reconciliation --------------------------------------------------------------------
    def reconcile(self, portfolio: dict | None) -> dict:
        """What the venues say we hold against what the orders should have left: per name, the
        drift as a share of the expected size; the live equity from the trade venues' accounts;
        the demotion rule on drawdown and drift."""
        held: dict[str, float] = {}
        equity = 0.0
        venues = {self._venue_for("spot"), self._venue_for("perp")}
        for a in (portfolio or {}).get("accounts") or []:
            if a.get("id") not in venues:
                continue
            equity += float(a.get("liquidation") or 0.0)
            for p in a.get("positions") or []:
                held[p["symbol"]] = held.get(p["symbol"], 0.0) + float(p.get("qty") or 0.0)
        drift = {}
        for sym, exp in self.expected.items():
            have = held.get(sym, 0.0)
            drift[sym] = round(abs(have - exp) / abs(exp), 3) if exp else (1.0 if have else 0.0)
        self.drift = drift
        self.equity = equity
        self.last_reconciled = time.time()
        if self.live and equity > 0:
            self.peak_equity = max(self.peak_equity, equity)
        tol = float(getattr(self.cfg, "live_drift_tol", 0.25) or 0.25)
        out = {"equity": round(equity, 2), "peak": round(self.peak_equity, 2), "held": {k: round(v, 8) for k, v in held.items() if k in self.expected},
               "drift": drift, "breach": ""}
        if self.live:
            if self.gate.killed or self.gate.kill_file():
                out["breach"] = "kill switch"
            elif self.peak_equity > 0 and equity > 0 and 1.0 - equity / self.peak_equity > float(getattr(self.cfg, "live_max_drawdown", 0.05) or 0.05):
                out["breach"] = f"live drawdown {(1.0 - equity / self.peak_equity) * 100:.1f}% from peak"
            elif any(d > tol for d in drift.values()):
                worst = max(drift.items(), key=lambda kv: kv[1])
                out["breach"] = f"position drift {worst[1] * 100:.0f}% on {base_of(worst[0])} (sent vs held)"
            if out["breach"]:
                self.demote(out["breach"])
        return out

    def status(self, checklist: dict | None = None) -> dict:
        slip = self.slippage[-100:]
        return {"live": self.live, "since": self.since, "demoted": self.demoted, "scale": float(getattr(self.cfg, "live_scale", 0.0) or 0.0),
                "expected": self.expected, "drift": self.drift, "equity": round(self.equity, 2), "peak_equity": round(self.peak_equity, 2),
                "orders": self.orders[-20:], "slippage_bps_mean": round(sum(slip) / len(slip), 2) if slip else None, "fills": len(self.slippage),
                "last_reconciled": self.last_reconciled, "heartbeat": self.heartbeat, "kinds": self.kinds, "checklist": checklist}
