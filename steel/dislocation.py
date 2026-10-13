"""Cross-venue dislocations: the same name quoted on many venues at once, and the moments when
the best bid on one venue stands above the best ask on another by more than both taker fees.

Steel streams every enabled venue's top of book. The monitor keeps the latest bid and ask per
venue per name and, on every quote, compares the best bid across venues with the best ask
across venues within the same dollar (USD, USDT and USDC books sit a stablecoin basis apart)
and the same kind (spot against spot, perp against perp: a spot-perp gap is a basis to carry,
not a round trip). Net of the two venues' taker fees and a latency margin, a positive gap
opens an episode; it closes when the gap is gone. The episode's duration is the execution
window: what an executor would have had to act in. Every closed episode is appended to
state/dislocations.jsonl and kept for the dashboard, with the distribution of windows and
edges per venue pair. Read-only: Steel measures, it never executes.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import deque
from pathlib import Path


class DislocationMonitor:
    def __init__(self, taker_bps, *, min_edge_bps: float = 0.0, latency_bps: float = 0.0, stale_s: float = 10.0,
                 log_path: str | Path | None = None, keep: int = 500, max_gap_bps: float = 500.0):
        """`taker_bps(venue) -> float`: the taker fee of a venue, per leg. `max_gap_bps`: a gross gap
        beyond it is not a dislocation but a quote that is not this asset (a venue's symbol that
        names another coin or unit) or a broken book; it is counted as suspect and never opens."""
        self.taker_bps = taker_bps
        self.min_edge_bps = float(min_edge_bps)
        self.max_gap_bps = float(max_gap_bps)
        self.suspect: dict[str, int] = {}                  # venue -> quotes refused for an impossible gap
        self.latency_bps = float(latency_bps)
        self.stale_s = float(stale_s)
        self.log_path = Path(log_path) if log_path else None
        self.per_sym: dict[str, int] = {}                  # closed episodes per pair, for the screener
        self.books: dict[str, dict[str, dict]] = {}        # sym -> venue -> {bid, ask, bid_size, ask_size, ts, quote, kind}
        self.open: dict[tuple, dict] = {}                   # (sym, quote, kind) -> the episode in progress
        self.episodes: deque = deque(maxlen=keep)           # closed episodes, newest last
        self.n_total = 0
        self.checks = 0
        self.on_open = None                                 # (episode, books-of-the-name) when an episode opens: the arbitrage executor's input
        self.on_close = None                                # (episode record) when one closes

    def update(self, sym: str, venue: str, bid, ask, ts: float | None = None, *, quote: str = "USD", kind: str = "spot",
               bid_size=None, ask_size=None, identity: str | None = None, ambiguous: bool = False) -> dict | None:
        """Record a venue's top of book and re-evaluate the name's group. `identity` is the coin
        the venue's listing resolved to (None when it could not be told) and `ambiguous` whether
        the ticker is shared by several coins: an ambiguous name only pairs venues that resolved
        to the same coin. Returns the episode that just closed, if one did."""
        try:
            b, a = float(bid), float(ask)
        except (TypeError, ValueError):
            return None
        if not (b > 0 and a > 0):
            return None
        now = float(ts or time.time())
        self.books.setdefault(sym, {})[venue] = {"bid": b, "ask": a, "bid_size": bid_size, "ask_size": ask_size, "ts": now,
                                                 "quote": str(quote or "USD"), "kind": "perp" if kind == "perp" else "spot",
                                                 "identity": identity, "ambiguous": bool(ambiguous)}
        return self._check(sym, str(quote or "USD"), "perp" if kind == "perp" else "spot", now, identity if ambiguous else None)

    def _check(self, sym: str, quote: str, kind: str, now: float, identity: str | None = None) -> dict | None:
        self.checks += 1
        books = [(v, q) for v, q in self.books.get(sym, {}).items()
                 if q["quote"] == quote and q["kind"] == kind and now - q["ts"] <= self.stale_s
                 and (not q.get("ambiguous") or (identity is not None and q.get("identity") == identity))]
        key = (sym, quote, kind)
        if len(books) < 2:
            return self._close(key, now)
        bid_v, bid_q = max(books, key=lambda vq: vq[1]["bid"])
        ask_v, ask_q = min(books, key=lambda vq: vq[1]["ask"])
        if bid_v == ask_v:                                   # the same venue cannot be both legs: the next best on each side
            others_bid = [vq for vq in books if vq[0] != ask_v]
            others_ask = [vq for vq in books if vq[0] != bid_v]
            cand = []
            if others_bid:
                v2, q2 = max(others_bid, key=lambda vq: vq[1]["bid"])
                cand.append((q2["bid"] - ask_q["ask"], v2, q2, ask_v, ask_q))
            if others_ask:
                v2, q2 = min(others_ask, key=lambda vq: vq[1]["ask"])
                cand.append((bid_q["bid"] - q2["ask"], bid_v, bid_q, v2, q2))
            if not cand:
                return self._close(key, now)
            _, bid_v, bid_q, ask_v, ask_q = max(cand, key=lambda c: c[0])
        mid = (bid_q["bid"] + ask_q["ask"]) / 2.0
        gross = (bid_q["bid"] - ask_q["ask"]) / mid * 1e4
        if self.max_gap_bps and gross > self.max_gap_bps:
            # which leg is off: the one further from the other books' midpoints (or, with two books, both are suspect)
            others = [(v, (q["bid"] + q["ask"]) / 2.0) for v, q in books if v not in (bid_v, ask_v)]
            ref = sorted(m for _, m in others)[len(others) // 2] if others else None
            for v, q in ((bid_v, bid_q), (ask_v, ask_q)):
                if ref is None or abs((q["bid"] + q["ask"]) / 2.0 / ref - 1.0) * 1e4 > self.max_gap_bps / 2:
                    self.suspect[v] = self.suspect.get(v, 0) + 1
            return self._close(key, now)
        fees = float(self.taker_bps(ask_v)) + float(self.taker_bps(bid_v))
        net = gross - fees - self.latency_bps
        if net <= self.min_edge_bps:
            return self._close(key, now)
        size = None
        if bid_q.get("bid_size") and ask_q.get("ask_size"):
            size = min(float(bid_q["bid_size"]), float(ask_q["ask_size"])) * mid
        ep = self.open.get(key)
        if ep is None or ep["buy"] != ask_v or ep["sell"] != bid_v:
            closed = self._close(key, now)                   # a different pair: that is a new episode
            self.open[key] = {"sym": sym, "quote": quote, "kind": kind, "buy": ask_v, "sell": bid_v, "start": now, "last": now,
                              "peak_bps": net, "sum_bps": net, "samples": 1, "fees_bps": fees, "size_usd": size, "gross_peak_bps": gross}
            if self.on_open:
                try:
                    self.on_open(dict(self.open[key]), {v: dict(q) for v, q in self.books.get(sym, {}).items()})
                except Exception:  # noqa: BLE001 -- the executor's problem must never break the monitor
                    pass
            return closed
        ep["last"] = now
        ep["peak_bps"] = max(ep["peak_bps"], net)
        ep["gross_peak_bps"] = max(ep["gross_peak_bps"], gross)
        ep["sum_bps"] += net
        ep["samples"] += 1
        if size is not None:
            ep["size_usd"] = min(ep["size_usd"], size) if ep.get("size_usd") is not None else size
        return None

    def _close(self, key: tuple, now: float) -> dict | None:
        ep = self.open.pop(key, None)
        if ep is None:
            return None
        rec = {"sym": ep["sym"], "quote": ep["quote"], "kind": ep["kind"], "buy": ep["buy"], "sell": ep["sell"],
               "start": round(ep["start"], 3), "end": round(now, 3), "window_ms": int(round((now - ep["start"]) * 1000)),
               "peak_bps": round(ep["peak_bps"], 2), "mean_bps": round(ep["sum_bps"] / max(1, ep["samples"]), 2),
               "gross_peak_bps": round(ep["gross_peak_bps"], 2), "fees_bps": round(ep["fees_bps"], 2),
               "samples": ep["samples"], "size_usd": round(ep["size_usd"], 2) if ep.get("size_usd") is not None else None}
        self.episodes.append(rec)
        self.n_total += 1
        self.per_sym[rec['sym']] = self.per_sym.get(rec['sym'], 0) + 1
        if self.on_close:
            try:
                self.on_close(rec)
            except Exception:  # noqa: BLE001
                pass
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
        return rec

    def count_for(self, sym: str) -> int:
        return int(self.per_sym.get(sym, 0))

    def sweep(self, now: float | None = None) -> list[dict]:
        """Close every open episode whose quotes have gone stale (the stream stopped mid-gap)."""
        now = float(now or time.time())
        closed = []
        for key, ep in list(self.open.items()):
            if now - ep["last"] > self.stale_s:
                rec = self._close(key, ep["last"])
                if rec:
                    closed.append(rec)
        return closed

    def stats(self, last: int = 50) -> dict:
        """The distribution of windows and edges over the kept episodes, overall and per venue
        pair, the episodes in progress, and the latest closed ones."""
        eps = list(self.episodes)
        pairs: dict[str, list[dict]] = {}
        for e in eps:
            pairs.setdefault(f"{e['buy']}>{e['sell']}", []).append(e)

        def summary(rows: list[dict]) -> dict:
            w = sorted(r["window_ms"] for r in rows)
            p = sorted(r["peak_bps"] for r in rows)
            n = len(rows)
            return {"n": n, "window_ms_median": statistics.median(w) if n else None, "window_ms_p90": w[int(0.9 * (n - 1))] if n else None,
                    "window_ms_max": w[-1] if n else None, "peak_bps_median": statistics.median(p) if n else None, "peak_bps_max": p[-1] if n else None,
                    "over_1s": sum(1 for x in w if x >= 1000), "over_5s": sum(1 for x in w if x >= 5000)}
        by_pair = {k: {**summary(v), "names": sorted({r["sym"] for r in v})[:12]} for k, v in pairs.items()}
        top = sorted(by_pair.items(), key=lambda kv: -kv[1]["n"])[:12]
        now = time.time()
        opened = [{**ep, "window_ms": int(round((now - ep["start"]) * 1000)), "mean_bps": round(ep["sum_bps"] / max(1, ep["samples"]), 2)}
                  for ep in self.open.values()]
        return {"t": now, "total": self.n_total, "kept": len(eps), "checks": self.checks, "overall": summary(eps), "suspect": dict(self.suspect), "max_gap_bps": self.max_gap_bps,
                "pairs": dict(top), "open": sorted(opened, key=lambda e: -e["peak_bps"])[:20],
                "recent": list(reversed(eps[-last:])), "venues": sorted({v for b in self.books.values() for v in b})}
