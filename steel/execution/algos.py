"""Execution algorithms (M25): a parent order split into child orders by a plan, each child
sent through the same gate as any order. The planners are pure functions (tested on their
own); `AlgoRunner.run` walks a plan with an executor, waits between slices, stops on the
first refusal or the kill switch, and keeps every result. Nothing here bypasses the gate:
a child order the gate refuses is not sent, and a venue that is not armed sends nothing.

    twap:  n equal slices every `every_s` seconds, at market (or a limit each)
    scale: a ladder of n limit orders from `start` to `end` (buy below, sell above), weights
           flat or front-loaded
    chase: one limit order re-pegged to the top of book every `every_s`, up to `max_chase_bps`
           from the first price, then (optionally) a market order for what is left
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from .base import ExecutionGate, Executor, Order, OrderResult

KINDS = ("twap", "scale", "chase")


def _qty_split(qty: float, n: int, front: float = 0.0) -> list[float]:
    """`front` 0 = flat; 1 = the first slice twice the last, linearly."""
    n = max(1, int(n))
    w = [1.0 + front * (1.0 - i / max(1, n - 1)) for i in range(n)] if n > 1 else [1.0]
    tot = sum(w)
    out = [round(qty * x / tot, 10) for x in w]
    out[-1] = qty - sum(out[:-1])                        # exact total after rounding
    return out


def plan_twap(order: Order, *, slices: int = 6, every_s: float = 60.0, limit_bps: float | None = None, ref_price: float | None = None) -> list[dict]:
    qs = _qty_split(order.qty, slices)
    out = []
    for i, q in enumerate(qs):
        px = None
        if limit_bps is not None and ref_price:
            px = ref_price * (1 + limit_bps / 1e4) if order.side == "buy" else ref_price * (1 - limit_bps / 1e4)
        out.append({"i": i, "at_s": i * float(every_s), "qty": q, "price": px, "note": f"twap {i + 1}/{len(qs)}"})
    return out


def plan_scale(order: Order, *, start: float, end: float, levels: int = 5, front: float = 0.0) -> list[dict]:
    """Limit orders from `start` to `end` inclusive; a buy ladder runs down, a sell ladder up."""
    levels = max(1, int(levels))
    qs = _qty_split(order.qty, levels, front)
    prices = [start + (end - start) * (i / max(1, levels - 1)) for i in range(levels)] if levels > 1 else [start]
    if (order.side == "buy" and end > start) or (order.side == "sell" and end < start):
        raise ValueError("a buy ladder runs down from start to end; a sell ladder runs up")
    return [{"i": i, "at_s": 0.0, "qty": q, "price": round(p, 10), "note": f"scale {i + 1}/{levels} at {p:g}"} for i, (q, p) in enumerate(zip(qs, prices))]


def plan_chase(order: Order, *, every_s: float = 5.0, max_chase_bps: float = 20.0, max_repegs: int = 12, finish_at_market: bool = True) -> dict:
    """Not a list of children but the rule: one working limit, re-pegged to the top of book."""
    return {"every_s": float(every_s), "max_chase_bps": float(max_chase_bps), "max_repegs": int(max_repegs), "finish_at_market": bool(finish_at_market), "qty": order.qty}


class AlgoRunner:
    """Runs a plan through an executor behind the gate. `book(symbol) -> (bid, ask)` feeds the
    chase; `sleep` is injectable for tests."""

    def __init__(self, gate: ExecutionGate, executors: dict[str, Executor], *, book=None, sleep=asyncio.sleep, log=None):
        self.gate, self.executors, self.book, self.sleep, self.log = gate, executors, book, sleep, log
        self.runs: list[dict] = []

    async def _send(self, child: Order, leverage: float, run: dict) -> OrderResult:
        """Every child goes through `Executor.place`: the gate's check, the audit log, the venue (or the dry run)."""
        ex = self.executors.get(child.venue)
        if ex is None:
            r = OrderResult(ok=False, venue=child.venue, status="refused", error=f"{child.venue} has no order client", request=asdict(child), t=time.time())
        else:
            r = await ex.place(child, leverage=leverage)
        run["results"].append({"ok": r.ok, "status": r.status, "error": r.error, "qty": child.qty, "price": child.price, "filled": r.filled_qty, "avg_price": r.avg_price, "t": r.t or time.time(), "note": child.note})
        if self.log:
            self.log(child, r)
        return r

    async def run(self, kind: str, order: Order, *, leverage: float = 1.0, **params) -> dict:
        run = {"kind": kind, "order": asdict(order), "params": params, "started": time.time(), "results": [], "stopped": "", "sent_qty": 0.0, "filled_qty": 0.0}
        self.runs.append(run)
        if kind not in KINDS:
            run["stopped"] = f"unknown algorithm {kind!r}"
            return run
        px = float(order.notional_usd / order.qty) if order.qty and order.notional_usd else None
        try:
            if kind == "twap":
                plan = plan_twap(order, slices=int(params.get("slices", 6)), every_s=float(params.get("every_s", 60.0)), limit_bps=params.get("limit_bps"), ref_price=px)
                t0 = time.time()
                for ch in plan:
                    wait = ch["at_s"] - (time.time() - t0)
                    if wait > 0:
                        await self.sleep(wait)
                    if self.gate.killed or self.gate.kill_file():
                        run["stopped"] = "kill switch"
                        break
                    child = Order(order.venue, order.symbol, order.side, ch["qty"], ch["price"], order.kind, order.reduce_only, notional_usd=(px or 0) * ch["qty"], note=ch["note"])
                    r = await self._send(child, leverage, run)
                    run["sent_qty"] += ch["qty"] if r.ok else 0.0
                    run["filled_qty"] += r.filled_qty
                    if not r.ok:
                        run["stopped"] = r.error or r.status
                        break
            elif kind == "scale":
                plan = plan_scale(order, start=float(params["start"]), end=float(params["end"]), levels=int(params.get("levels", 5)), front=float(params.get("front", 0.0)))
                for ch in plan:
                    if self.gate.killed or self.gate.kill_file():
                        run["stopped"] = "kill switch"
                        break
                    child = Order(order.venue, order.symbol, order.side, ch["qty"], ch["price"], order.kind, order.reduce_only, notional_usd=ch["price"] * ch["qty"], note=ch["note"])
                    r = await self._send(child, leverage, run)
                    run["sent_qty"] += ch["qty"] if r.ok else 0.0
                    run["filled_qty"] += r.filled_qty
                    if not r.ok:
                        run["stopped"] = r.error or r.status
                        break
            else:
                rule = plan_chase(order, every_s=float(params.get("every_s", 5.0)), max_chase_bps=float(params.get("max_chase_bps", 20.0)),
                                  max_repegs=int(params.get("max_repegs", 12)), finish_at_market=bool(params.get("finish_at_market", True)))
                left = order.qty
                first = None
                ex = self.executors.get(order.venue)
                working: OrderResult | None = None
                for i in range(rule["max_repegs"] + 1):
                    if self.gate.killed or self.gate.kill_file():
                        run["stopped"] = "kill switch"
                        break
                    bid, ask = self.book(order.symbol) if self.book else (None, None)
                    peg = (bid if order.side == "buy" else ask) if bid and ask else px
                    if not peg:
                        run["stopped"] = "no book to chase"
                        break
                    first = first or peg
                    if abs(peg / first - 1.0) * 1e4 > rule["max_chase_bps"]:
                        run["stopped"] = f"chased {abs(peg / first - 1.0) * 1e4:.1f} bps, past the limit"
                        break
                    if working and working.ok and working.order_id and ex is not None:
                        await ex.cancel(working.order_id, order.symbol)
                    child = Order(order.venue, order.symbol, order.side, left, peg, order.kind, order.reduce_only, notional_usd=peg * left, note=f"chase peg {i + 1}")
                    working = await self._send(child, leverage, run)
                    if not working.ok:
                        run["stopped"] = working.error or working.status
                        break
                    run["sent_qty"] = left
                    left = max(0.0, left - working.filled_qty)
                    run["filled_qty"] = order.qty - left
                    if left <= 1e-12:
                        break
                    await self.sleep(rule["every_s"])
                if left > 1e-12 and not run["stopped"] and rule["finish_at_market"]:
                    if working and working.ok and working.order_id and ex is not None:
                        await ex.cancel(working.order_id, order.symbol)
                    child = Order(order.venue, order.symbol, order.side, left, None, order.kind, order.reduce_only, notional_usd=(px or first or 0) * left, note="chase: the rest at market")
                    r = await self._send(child, leverage, run)
                    if r.ok:
                        run["filled_qty"] += r.filled_qty
                    else:
                        run["stopped"] = r.error or r.status
        except (KeyError, ValueError) as e:
            run["stopped"] = f"bad plan: {e}"
        run["ended"] = time.time()
        return run
