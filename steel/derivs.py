"""Derivatives feeds: liquidations and the perp statistics (open interest, funding, mark,
index) per venue, from the perp venues' public streams. Market-data sources serve prices;
these serve what the derivatives market is doing around them.

`LiqStore` keeps every liquidation event (time, price, USD, side, venue) in a ring per pair:
a long liquidated is a forced sell (side -1), a short liquidated a forced buy (+1). `PerpStats`
keeps the latest OI, funding, mark and index per venue per pair, aggregates them (OI in USD
summed, funding weighted by OI, mark as the median), and a per-bar history. Two features ride
in every bar (`features.FLOW`): the log USD of longs and of shorts liquidated in the bar."""
from __future__ import annotations

import math
import threading
import time

import numpy as np

LIQ_DT = np.dtype([("t", "f8"), ("px", "f8"), ("usd", "f8"), ("side", "i1"), ("v", "i2")])


class LiqStore:
    def __init__(self, pairs: list[str], *, mb: float = 16.0, keep_s: float = 7 * 86400.0):
        self.mb, self.keep_s = float(mb), float(keep_s)
        self.venues: list[str] = []
        self._vid: dict[str, int] = {}
        self.rings: dict[str, np.ndarray] = {}
        self.ptr: dict[str, int] = {}
        self.size: dict[str, int] = {}
        self.count = 0
        self._lock = threading.Lock()
        self.resize(pairs)

    def resize(self, pairs: list[str]) -> None:
        cap = max(500, int(self.mb * 1e6 / LIQ_DT.itemsize / max(1, len(pairs))))
        with self._lock:
            kept = {p: self._rows(p) for p in pairs if p in self.rings}
            self.rings, self.ptr, self.size = {}, {}, {}
            for p in pairs:
                ring = np.zeros(cap, dtype=LIQ_DT)
                rows = kept.get(p, np.zeros(0, dtype=LIQ_DT))[-cap:]
                ring[:len(rows)] = rows
                self.rings[p], self.ptr[p], self.size[p] = ring, len(rows) % cap, len(rows)
            self.cap = cap

    def _rows(self, pair: str) -> np.ndarray:
        ring = self.rings.get(pair)
        if ring is None:
            return np.zeros(0, dtype=LIQ_DT)
        n, p = self.size[pair], self.ptr[pair]
        return ring[:n] if n < self.cap else np.concatenate([ring[p:], ring[:p]])

    def _v(self, venue: str) -> int:
        i = self._vid.get(venue)
        if i is None:
            i = len(self.venues); self.venues.append(venue); self._vid[venue] = i
        return i

    def add(self, venue: str, pair: str, ts: float, px: float, usd: float, side: int) -> bool:
        """side: -1 a long was liquidated (a forced sell), +1 a short (a forced buy)."""
        if pair not in self.rings or usd <= 0 or px <= 0:
            return False
        with self._lock:
            i = self.ptr[pair]
            self.rings[pair][i] = (float(ts), float(px), float(usd), int(side), self._v(venue))
            self.ptr[pair] = (i + 1) % self.cap
            self.size[pair] = min(self.size[pair] + 1, self.cap)
            self.count += 1
        return True

    def rows(self, pair: str, since: float | None = None) -> np.ndarray:
        with self._lock:
            r = self._rows(pair).copy()
        cut = max(float(since or 0.0), time.time() - self.keep_s)
        return r[r["t"] >= cut] if len(r) else r

    def recent(self, pair: str, n: int = 30, min_usd: float = 0.0) -> list[dict]:
        r = self.rows(pair)
        if min_usd > 0 and len(r):
            r = r[r["usd"] >= min_usd]
        return [{"t": float(x["t"]), "px": float(x["px"]), "usd": round(float(x["usd"]), 2), "side": int(x["side"]),
                 "venue": self.venues[int(x["v"])] if int(x["v"]) < len(self.venues) else "?"} for x in r[-n:][::-1]]

    def per_bar(self, pair: str, bar_s: float, bars: int, now: float | None = None) -> dict:
        """Longs and shorts liquidated (USD) per bar for the last `bars` bars."""
        now = time.time() if now is None else now
        end = (int(now // bar_s) + 1) * bar_s
        start = end - bars * bar_s
        r = self.rows(pair, since=start)
        k = np.clip(((r["t"] - start) // bar_s).astype(int), 0, bars - 1) if len(r) else np.zeros(0, dtype=int)
        longs = np.bincount(k, weights=np.where(r["side"] < 0, r["usd"], 0.0), minlength=bars)[:bars] if len(r) else np.zeros(bars)
        shorts = np.bincount(k, weights=np.where(r["side"] > 0, r["usd"], 0.0), minlength=bars)[:bars] if len(r) else np.zeros(bars)
        return {"pair": pair, "t0": start, "bar_s": bar_s, "longs": [round(float(x), 2) for x in longs], "shorts": [round(float(x), 2) for x in shorts],
                "total_longs": round(float(longs.sum()), 2), "total_shorts": round(float(shorts.sum()), 2), "n": int(len(r))}

    def last_bar(self, pair: str, bar_s: float, now: float | None = None) -> tuple[float, float]:
        """(longs USD, shorts USD) liquidated in the bar that just closed."""
        now = time.time() if now is None else now
        end = int(now // bar_s) * bar_s
        r = self.rows(pair, since=end - bar_s)
        r = r[r["t"] < end] if len(r) else r
        return (float(r["usd"][r["side"] < 0].sum()) if len(r) else 0.0, float(r["usd"][r["side"] > 0].sum()) if len(r) else 0.0)

    def status(self) -> dict:
        with self._lock:
            held = int(sum(self.size.values()))
        return {"pairs": len(self.rings), "events": held, "total": self.count, "venues": list(self.venues), "mb": round(held * LIQ_DT.itemsize / 1e6, 2)}


class PerpStats:
    FIELDS = ("oi", "oi_usd", "funding", "mark", "index", "next_funding", "funding_hours")

    def __init__(self, stale_s: float = 300.0):
        self.stats: dict[tuple[str, str], dict] = {}
        self.history: dict[str, list] = {}            # pair -> [(t, oi_usd, funding, mark)] per bar, last 600
        self.stale_s = float(stale_s)
        self._lock = threading.Lock()

    def apply(self, venue: str, pair: str, ts: float | None = None, **fields) -> None:
        ts = time.time() if ts is None else float(ts)
        with self._lock:
            d = self.stats.setdefault((venue, pair), {k: None for k in self.FIELDS})
            for k, v in fields.items():
                if k in self.FIELDS and v is not None:
                    d[k] = float(v)
            if d.get("oi") is not None and d.get("mark") and d.get("oi_usd") is None:
                d["oi_usd"] = d["oi"] * d["mark"]
            d["t"] = ts

    def venues(self, pair: str, now: float | None = None) -> dict[str, dict]:
        now = time.time() if now is None else now
        with self._lock:
            return {v: dict(d) for (v, p), d in self.stats.items() if p == pair and now - float(d.get("t") or 0.0) <= self.stale_s}

    def aggregate(self, pair: str, now: float | None = None) -> dict | None:
        vs = self.venues(pair, now)
        if not vs:
            return None
        oi = sum(float(d.get("oi_usd") or 0.0) for d in vs.values())
        w = [(float(d.get("oi_usd") or 0.0), float(d["funding"])) for d in vs.values() if d.get("funding") is not None]
        funding = (sum(a * f for a, f in w) / sum(a for a, _ in w)) if w and sum(a for a, _ in w) > 0 else (sum(f for _, f in w) / len(w) if w else None)
        marks = [float(d["mark"]) for d in vs.values() if d.get("mark")]
        return {"oi_usd": round(oi, 2), "funding": round(funding, 8) if funding is not None else None, "mark": float(np.median(marks)) if marks else None,
                "venues": sorted(vs), "n": len(vs)}

    def mark(self, pair: str, ts: float) -> None:
        a = self.aggregate(pair)
        if not a:
            return
        with self._lock:
            lst = self.history.setdefault(pair, [])
            lst.append((round(ts), a["oi_usd"], a["funding"], a["mark"]))
            del lst[:-600]

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            live = sum(1 for d in self.stats.values() if now - float(d.get("t") or 0.0) <= self.stale_s)
            venues = sorted({v for v, _ in self.stats})
        return {"series": len(self.stats), "live": live, "venues": venues}
