"""Order books: every live venue's resting bids and asks per pair, the depth bands over them,
the aggregate ladder, and a decimated history of the levels for the heatmap.

A venue pushes a snapshot (its whole book, or the top N levels) or a delta (levels that
changed; size 0 removes). `BookStore.apply` keeps each (venue, pair) book as price -> base
size. Reads are in dollars of notional so venues and pairs compare: `bands` sums the resting
size within 0.1, 0.25, 0.5, 1, 2.5, 5 and 10 percent of the mid on each side (MMT's seven),
`ladder` merges the enabled venues' levels into one aggregate ladder, `features` is what a
bar carries into the net (imbalance at 0.5, 2 and 10 percent, log depth within 1 percent),
and `record` keeps one row of the top levels per side per venue every `history_s` for the
heatmap, under a memory budget."""
from __future__ import annotations

import math
import threading
import time

import numpy as np

BANDS_PCT = [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
HIST_DT = np.dtype([("t", "f8"), ("px", "f8"), ("usd", "f8"), ("side", "i1"), ("v", "i2")])


class Book:
    __slots__ = ("bids", "asks", "t", "snapshots", "deltas")

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.t = 0.0
        self.snapshots = 0
        self.deltas = 0

    def apply(self, bids, asks, snapshot: bool, ts: float, depth: int | None = None) -> None:
        """`depth`: the venue keeps the best N each side and never deletes a level that fell out
        of them (Kraken's book channel), so the book is truncated to N after each update; a
        stale level left behind would cross the book and leave it without a mid."""
        if snapshot:
            self.bids, self.asks = {}, {}
            self.snapshots += 1
        else:
            self.deltas += 1
        for px, sz in bids:
            px, sz = float(px), float(sz)
            if sz <= 0:
                self.bids.pop(px, None)
            else:
                self.bids[px] = sz
        for px, sz in asks:
            px, sz = float(px), float(sz)
            if sz <= 0:
                self.asks.pop(px, None)
            else:
                self.asks[px] = sz
        if depth and len(self.bids) > depth:
            self.bids = dict(sorted(self.bids.items(), reverse=True)[:depth])
        if depth and len(self.asks) > depth:
            self.asks = dict(sorted(self.asks.items())[:depth])
        self.t = ts

    def best(self) -> tuple[float | None, float | None]:
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)


class BookStore:
    def __init__(self, pairs: list[str], *, mb: float = 64.0, history_s: float = 15.0, levels: int = 40, stale_s: float = 120.0):
        self.books: dict[tuple[str, str], Book] = {}
        self.quote: dict[str, str] = {}               # venue -> USD | USDT | USDC: books only merge within one dollar
        self.pairs = set(pairs)
        self.history_s = float(history_s)
        self.levels = int(levels)
        self.stale_s = float(stale_s)
        self.venues: list[str] = []
        self._vid: dict[str, int] = {}
        self.mb = float(mb)
        self.hist: dict[str, np.ndarray] = {}
        self.hptr: dict[str, int] = {}
        self.hsize: dict[str, int] = {}
        self.last_rec: dict[str, float] = {}
        self.imb: dict[str, list] = {}                 # pair -> [(t, imb_0.5, imb_2, imb_10, depth_1_usd)] per bar, last 600
        self._lock = threading.Lock()
        self.resize(pairs)

    def resize(self, pairs: list[str]) -> None:
        self.pairs = set(pairs)
        cap = max(2_000, int(self.mb * 1e6 / HIST_DT.itemsize / max(1, len(pairs))))
        with self._lock:
            for p in pairs:
                if p not in self.hist:
                    self.hist[p], self.hptr[p], self.hsize[p] = np.zeros(cap, dtype=HIST_DT), 0, 0
                    self.imb[p] = []
            for p in [p for p in self.hist if p not in self.pairs]:
                self.hist.pop(p), self.hptr.pop(p), self.hsize.pop(p), self.imb.pop(p, None)
            for key in [k for k in self.books if k[1] not in self.pairs]:
                self.books.pop(key)
            self.hcap = cap

    def _v(self, venue: str) -> int:
        i = self._vid.get(venue)
        if i is None:
            i = len(self.venues); self.venues.append(venue); self._vid[venue] = i
        return i

    # Writes ---------------------------------------------------------------------------
    def apply(self, venue: str, pair: str, bids, asks, snapshot: bool, ts: float | None = None, quote: str = "USD", depth: int | None = None) -> bool:
        if pair not in self.pairs:
            return False
        ts = time.time() if ts is None else float(ts)
        self.quote[venue] = str(quote or "USD").upper()
        with self._lock:
            b = self.books.get((venue, pair))
            if b is None:
                b = self.books[(venue, pair)] = Book()
                self._v(venue)
            b.apply(bids, asks, snapshot, ts, depth=depth)
        if ts - self.last_rec.get(pair, 0.0) >= self.history_s:
            self.record(pair, ts)
        return True

    def record(self, pair: str, ts: float) -> None:
        """One decimated row set: the top `levels` per side per venue, for the heatmap."""
        self.last_rec[pair] = ts
        with self._lock:
            ring, cap = self.hist.get(pair), self.hcap
            if ring is None:
                return
            for (venue, p), b in self.books.items():
                if p != pair or ts - b.t > self.stale_s:
                    continue
                v = self._v(venue)
                for side, levels in ((1, sorted(b.bids.items(), reverse=True)[:self.levels]), (-1, sorted(b.asks.items())[:self.levels])):
                    for px, sz in levels:
                        i = self.hptr[pair]
                        ring[i] = (ts, px, px * sz, side, v)
                        self.hptr[pair] = (i + 1) % cap
                        self.hsize[pair] = min(self.hsize[pair] + 1, cap)

    # Reads ----------------------------------------------------------------------------
    def live(self, pair: str, venues: set[str] | None = None, now: float | None = None, quote: str | None = None) -> list[tuple[str, Book]]:
        """The live books for a pair; `quote` keeps one dollar class (a USDT book sits a basis off a USD one)."""
        now = time.time() if now is None else now
        q = str(quote).upper() if quote else None
        with self._lock:
            return [(v, b) for (v, p), b in self.books.items() if p == pair and (not venues or v in venues) and now - b.t <= self.stale_s
                    and (q is None or self.quote.get(v, "USD") == q)]

    def quotes(self, pair: str) -> dict[str, int]:
        """How many live books each dollar class has for the pair."""
        out: dict[str, int] = {}
        for v, _ in self.live(pair):
            out[self.quote.get(v, "USD")] = out.get(self.quote.get(v, "USD"), 0) + 1
        return out

    def mid(self, pair: str, venues: set[str] | None = None, quote: str | None = None) -> float | None:
        mids = []
        for _, b in self.live(pair, venues, quote=quote):
            bid, ask = b.best()
            if bid and ask and ask >= bid:
                mids.append((bid + ask) / 2)
        return float(np.median(mids)) if mids else None

    def bands(self, pair: str, venues: set[str] | None = None, mid: float | None = None, quote: str | None = None) -> dict:
        """Resting USD within each band of the mid, both sides, aggregated and per venue, within one dollar class."""
        books = self.live(pair, venues, quote=quote)
        mid = mid or self.mid(pair, venues, quote=quote)
        out = {"pair": pair, "mid": mid, "quote": quote, "venues": {}, "bands": []}
        if not mid or not books:
            return out
        agg_b = np.zeros(len(BANDS_PCT)); agg_a = np.zeros(len(BANDS_PCT))
        for venue, b in books:
            vb = np.zeros(len(BANDS_PCT)); va = np.zeros(len(BANDS_PCT))
            for px, sz in b.bids.items():
                d = (mid - px) / mid * 100
                for k, pct in enumerate(BANDS_PCT):
                    if d <= pct:
                        vb[k] += px * sz
            for px, sz in b.asks.items():
                d = (px - mid) / mid * 100
                for k, pct in enumerate(BANDS_PCT):
                    if d <= pct:
                        va[k] += px * sz
            agg_b += vb; agg_a += va
            out["venues"][venue] = {"bid": [round(float(x), 2) for x in vb], "ask": [round(float(x), 2) for x in va], "levels": len(b.bids) + len(b.asks),
                                    "age_s": round(time.time() - b.t, 1)}
        for k, pct in enumerate(BANDS_PCT):
            tot = agg_b[k] + agg_a[k]
            out["bands"].append({"pct": pct, "bid": round(float(agg_b[k]), 2), "ask": round(float(agg_a[k]), 2),
                                 "imbalance": round(float((agg_b[k] - agg_a[k]) / tot), 4) if tot > 0 else 0.0})
        return out

    def ladder(self, pair: str, n: int = 20, venues: set[str] | None = None, bucket_bps: float = 0.0, quote: str | None = None) -> dict:
        """The aggregate ladder: the best `n` levels each side across the venues of one dollar
        class, each level's USD and its venue shares; `bucket_bps` merges levels within that
        many bps of each other."""
        books = self.live(pair, venues, quote=quote)
        mid = self.mid(pair, venues, quote=quote)
        if not mid or not books:
            return {"pair": pair, "mid": mid, "quote": quote, "bids": [], "asks": []}
        step = mid * bucket_bps / 1e4 if bucket_bps > 0 else 0.0
        def merge(side):
            acc: dict[float, dict] = {}
            for venue, b in books:
                for px, sz in (b.bids if side > 0 else b.asks).items():
                    key = (math.floor(px / step) * step if step else px)
                    row = acc.setdefault(key, {"px": key, "usd": 0.0, "by": {}})
                    row["usd"] += px * sz
                    row["by"][venue] = row["by"].get(venue, 0.0) + px * sz
            rows = sorted(acc.values(), key=lambda r: -r["px"] if side > 0 else r["px"])[:n]
            cum = 0.0
            for r in rows:
                cum += r["usd"]; r["cum"] = round(cum, 2); r["usd"] = round(r["usd"], 2)
                r["by"] = {k: round(v / max(1e-9, sum(r["by"].values())), 2) for k, v in sorted(r["by"].items(), key=lambda kv: -kv[1])[:4]}
            return rows
        return {"pair": pair, "mid": mid, "quote": quote, "bids": merge(1), "asks": merge(-1), "venues": [v for v, _ in books]}

    def best_quote(self, pair: str, prefer: str | None = None) -> str | None:
        """The dollar class to read the pair in: the preferred one when it has a live book, else the one with the most books."""
        q = self.quotes(pair)
        if not q:
            return None
        usable = {k: n for k, n in q.items() if self.mid(pair, quote=k)}       # a class whose books have both sides
        if not usable:
            return None
        if prefer and usable.get(str(prefer).upper()):
            return str(prefer).upper()
        return max(usable.items(), key=lambda kv: kv[1])[0]

    def features(self, pair: str, quote: str | None = None) -> dict | None:
        """What a bar carries: imbalance within 0.5%, 2% (interpolated from the 1% and 2.5% bands) and
        10%, and the log USD depth within 1% (both sides), over the books of one dollar class
        (the one with the most books unless given). None when no live book."""
        b = self.bands(pair, quote=quote or self.best_quote(pair))
        if not b["bands"]:
            return None
        by = {x["pct"]: x for x in b["bands"]}
        d1 = by[1.0]["bid"] + by[1.0]["ask"]
        return {"ob_imb_05": by[0.5]["imbalance"], "ob_imb_2": round((by[1.0]["imbalance"] + by[2.5]["imbalance"]) / 2, 4),
                "ob_imb_10": by[10.0]["imbalance"], "ob_depth_1": round(math.log1p(d1), 4)}

    def mark(self, pair: str, ts: float) -> None:
        """At a bar close: keep the band readings for the panel's history."""
        f = self.features(pair)
        if f is None:
            return
        with self._lock:
            lst = self.imb.setdefault(pair, [])
            lst.append((round(ts), f["ob_imb_05"], f["ob_imb_2"], f["ob_imb_10"], round(math.expm1(f["ob_depth_1"]), 2)))
            del lst[:-600]

    def history(self, pair: str, since: float | None = None, venues: set[str] | None = None) -> np.ndarray:
        with self._lock:
            ring, n, p = self.hist.get(pair), self.hsize.get(pair, 0), self.hptr.get(pair, 0)
            if ring is None or not n:
                return np.zeros(0, dtype=HIST_DT)
            r = (ring[:n] if n < self.hcap else np.concatenate([ring[p:], ring[:p]])).copy()
        if since:
            r = r[r["t"] >= since]
        if venues:
            ids = [self._vid[v] for v in venues if v in self._vid]
            r = r[np.isin(r["v"], ids)]
        return r

    def heatmap(self, pair: str, tf: float, n: int, bucket_bps: float = 5.0, quote: str | None = None, to: float | None = None, max_levels: int = 160) -> dict:
        """The resting book over time for the chart: `n` columns of `tf` seconds, price rows of
        about `bucket_bps` of the mid, each cell the mean USD resting at that price across the
        samples in the column (bids and asks kept apart). Built from the decimated level
        history of the venues in one dollar class."""
        import math as _m
        to = time.time() if to is None else float(to)
        end = (int(to // tf) + 1) * tf
        start = end - n * tf
        q = str(quote).upper() if quote else None
        venues = {v for v in self.venues if (q is None or self.quote.get(v, "USD") == q)}
        h = self.history(pair, since=start, venues=venues or None)
        if not len(h):
            return {"pair": pair, "tf": tf, "t0": start, "n": n, "bucket": None, "cols": [], "quote": q, "max": 0.0}
        h = h[h["t"] < end]
        mid = float(np.median(h["px"]))
        raw = max(mid * bucket_bps / 1e4, 1e-9)
        k = 10 ** _m.floor(_m.log10(raw))
        bucket = min((m * k for m in (1, 2, 5, 10)), key=lambda v: abs(_m.log(v / raw)))
        lvl = np.floor(h["px"] / bucket + 1e-9).astype(np.int64)
        col = ((h["t"] - start) // tf).astype(np.int64)
        samples = {}
        for c_, t_ in zip(col, h["t"]):
            samples.setdefault(int(c_), set()).add(round(float(t_), 1))
        cols = []
        mx = 0.0
        for c_ in np.unique(col):
            m = col == c_
            ns = max(1, len(samples.get(int(c_), ())))
            cells = {}
            for L, u, sd in zip(lvl[m], h["usd"][m], h["side"][m]):
                key = (int(L), int(sd))
                cells[key] = cells.get(key, 0.0) + float(u)
            out = [[int(L), int(sd), round(u / ns, 2)] for (L, sd), u in cells.items()]
            mx = max(mx, max((x[2] for x in out), default=0.0))
            cols.append({"t": start + int(c_) * tf, "cells": out})
        # trim to the busiest price rows so a thin far level never dominates the scale
        return {"pair": pair, "tf": tf, "t0": start, "n": n, "bucket": bucket, "cols": cols, "quote": q, "max": round(mx, 2), "venues": sorted(venues)}

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            live = sum(1 for b in self.books.values() if now - b.t <= self.stale_s)
            rows = int(sum(self.hsize.values()))
        return {"books": len(self.books), "live": live, "venues": list(self.venues), "history_rows": rows,
                "history_mb": round(rows * HIST_DT.itemsize / 1e6, 1), "budget_mb": self.mb, "history_s": self.history_s}
