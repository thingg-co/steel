"""The trade tape: every real print from every live venue, kept in memory for a day.

Bars throw the prints away; the order-flow views need them back. Each pair has a ring of
(time, price, size, side, venue) rows in a structured numpy array sized from a memory budget
shared across the universe (`tape_mb`), trimmed to `tape_hours`. Reads are vectorized:
the last N prints, the prints above a notional, volume and delta by trade-size band (the
eleven bands MMT uses, $0 to $5M+), and a CVD series per bar per band. Everything here is
in dollars of notional so pairs compare."""
from __future__ import annotations

import math
import threading
import time

import numpy as np

DT = np.dtype([("t", "f8"), ("px", "f8"), ("sz", "f8"), ("side", "i1"), ("v", "i2")])
# trade-size bands in dollars of notional: the lower edge of each; MMT's eleven
BANDS = [0.0, 1_000.0, 5_000.0, 10_000.0, 25_000.0, 50_000.0, 100_000.0, 250_000.0, 500_000.0, 1_000_000.0, 5_000_000.0]
BAND_LABELS = ["<1k", "1k", "5k", "10k", "25k", "50k", "100k", "250k", "500k", "1M", "5M+"]


class Tape:
    def __init__(self, pairs: list[str], *, mb: float = 256.0, keep_s: float = 86400.0, venues: list[str] | None = None):
        self.keep_s = float(keep_s)
        self.venues: list[str] = list(venues or [])
        self._vid: dict[str, int] = {v: i for i, v in enumerate(self.venues)}
        self.cap = 0
        self.rings: dict[str, np.ndarray] = {}
        self.ptr: dict[str, int] = {}
        self.size: dict[str, int] = {}
        self.count = 0
        self._lock = threading.Lock()
        self.resize(pairs, mb)

    # Shape ----------------------------------------------------------------------------
    def resize(self, pairs: list[str], mb: float | None = None) -> None:
        """A ring per pair, the budget split evenly; a universe change keeps the prints of the pairs that stay."""
        if mb is not None:
            self.mb = float(mb)
        n = max(1, len(pairs))
        cap = max(1_000, int(self.mb * 1e6 / DT.itemsize / n))
        with self._lock:
            old = self.rings
            kept = {p: self._rows_unlocked(p, old) for p in pairs if p in old}   # before the counters reset
            self.rings, self.ptr, self.size = {}, {}, {}
            for p in pairs:
                ring = np.zeros(cap, dtype=DT)
                if p in kept:
                    rows = kept[p][-cap:]
                    ring[:len(rows)] = rows
                    self.ptr[p], self.size[p] = len(rows) % cap, len(rows)
                else:
                    self.ptr[p], self.size[p] = 0, 0
                self.rings[p] = ring
            self.cap = cap

    def _rows_unlocked(self, pair: str, rings: dict | None = None) -> np.ndarray:
        rings = self.rings if rings is None else rings
        ring = rings.get(pair)
        if ring is None:
            return np.zeros(0, dtype=DT)
        n, p, cap = self.size.get(pair, 0), self.ptr.get(pair, 0), len(ring)
        if n < cap:
            return ring[:n]
        return np.concatenate([ring[p:], ring[:p]])

    def venue_id(self, venue: str) -> int:
        i = self._vid.get(venue)
        if i is None:
            i = len(self.venues)
            self.venues.append(venue)
            self._vid[venue] = i
        return i

    # Writes ---------------------------------------------------------------------------
    def add(self, venue: str, pair: str, ts: float, price: float, size: float, taker_buy: bool | None) -> bool:
        """One real print. Quote ticks and zero sizes never land here."""
        ring = self.rings.get(pair)
        if ring is None or not size or not price or price <= 0:
            return False
        with self._lock:
            i = self.ptr[pair]
            ring[i] = (float(ts), float(price), float(size), 1 if taker_buy else (-1 if taker_buy is False else 0), self.venue_id(venue))
            self.ptr[pair] = (i + 1) % self.cap
            self.size[pair] = min(self.size[pair] + 1, self.cap)
            self.count += 1
        return True

    # Reads ----------------------------------------------------------------------------
    def rows(self, pair: str, since: float | None = None, min_usd: float = 0.0, venues: set[str] | None = None) -> np.ndarray:
        """The prints for a pair, oldest first, within `since`, at or above `min_usd` of notional."""
        with self._lock:
            r = self._rows_unlocked(pair).copy()
        if not len(r):
            return r
        cut = max(float(since or 0.0), time.time() - self.keep_s)
        m = r["t"] >= cut
        if min_usd > 0:
            m &= r["px"] * r["sz"] >= min_usd
        if venues:
            ids = {self._vid[v] for v in venues if v in self._vid}
            m &= np.isin(r["v"], list(ids))
        return r[m]

    def recent(self, pair: str, n: int = 50, min_usd: float = 0.0, since: float | None = None) -> list[dict]:
        r = self.rows(pair, since=since, min_usd=min_usd)[-n:]
        return [{"t": float(x["t"]), "px": float(x["px"]), "sz": float(x["sz"]), "usd": round(float(x["px"] * x["sz"]), 2),
                 "side": int(x["side"]), "venue": self.venues[int(x["v"])] if int(x["v"]) < len(self.venues) else "?"} for x in r[::-1]]

    @staticmethod
    def band_index(usd: np.ndarray) -> np.ndarray:
        return np.searchsorted(np.asarray(BANDS[1:]), usd, side="right")

    def bands(self, pair: str, since: float | None = None) -> dict:
        """Buy and sell volume (USD) and prints per size band, and the delta, over the window."""
        r = self.rows(pair, since=since)
        usd = r["px"] * r["sz"] if len(r) else np.zeros(0)
        idx = self.band_index(usd) if len(r) else np.zeros(0, dtype=int)
        out = []
        for b in range(len(BANDS)):
            m = idx == b
            buy = float(usd[m & (r["side"] > 0)].sum()) if len(r) else 0.0
            sell = float(usd[m & (r["side"] < 0)].sum()) if len(r) else 0.0
            out.append({"band": BAND_LABELS[b], "min_usd": BANDS[b], "buy": round(buy, 2), "sell": round(sell, 2), "delta": round(buy - sell, 2),
                        "n": int(m.sum()) if len(r) else 0})
        tot_b = sum(x["buy"] for x in out); tot_s = sum(x["sell"] for x in out)
        return {"pair": pair, "since": since, "n": int(len(r)), "buy": round(tot_b, 2), "sell": round(tot_s, 2), "delta": round(tot_b - tot_s, 2),
                "bands": out, "venues": self.venue_shares(r)}

    def venue_shares(self, r: np.ndarray) -> dict:
        if not len(r):
            return {}
        usd = r["px"] * r["sz"]
        tot = float(usd.sum()) or 1.0
        return {self.venues[int(v)]: round(float(usd[r["v"] == v].sum()) / tot, 3) for v in np.unique(r["v"]) if int(v) < len(self.venues)}

    def cvd(self, pair: str, bar_s: float, bars: int, min_usd: float = 0.0, max_usd: float | None = None, since: float | None = None) -> dict:
        """Per-bar buy, sell and delta in USD for the last `bars` bars (bar close times), plus the
        running CVD, for prints in [min_usd, max_usd) of notional."""
        now = time.time()
        end = (int(now // bar_s) + 1) * bar_s
        start = end - bars * bar_s
        r = self.rows(pair, since=max(start, since or 0.0))
        usd = r["px"] * r["sz"] if len(r) else np.zeros(0)
        if len(r):
            m = usd >= min_usd
            if max_usd is not None:
                m &= usd < max_usd
            r, usd = r[m], usd[m]
        k = np.clip(((r["t"] - start) // bar_s).astype(int), 0, bars - 1) if len(r) else np.zeros(0, dtype=int)
        buy = np.bincount(k, weights=np.where(r["side"] > 0, usd, 0.0), minlength=bars)[:bars] if len(r) else np.zeros(bars)
        sell = np.bincount(k, weights=np.where(r["side"] < 0, usd, 0.0), minlength=bars)[:bars] if len(r) else np.zeros(bars)
        delta = buy - sell
        return {"pair": pair, "bar_s": bar_s, "t0": start, "buy": [round(float(x), 2) for x in buy], "sell": [round(float(x), 2) for x in sell],
                "delta": [round(float(x), 2) for x in delta], "cvd": [round(float(x), 2) for x in np.cumsum(delta)]}

    def candles(self, pair: str, tf: float, n: int, venues: set[str] | None = None, to: float | None = None) -> list[dict]:
        """Candles of `tf` seconds built from the prints (any venue set), the last `n` ending at
        `to` (now by default): open, high, low, close, volume in base, buy and sell volume in USD,
        prints. Buckets with no print are left out (the chart draws the gap)."""
        to = time.time() if to is None else float(to)
        end = (int(to // tf) + 1) * tf
        start = end - n * tf
        r = self.rows(pair, since=start, venues=venues)
        if not len(r):
            return []
        r = r[r["t"] < end]
        k = ((r["t"] - start) // tf).astype(int)
        out = []
        order = np.argsort(r["t"], kind="stable")
        r, k = r[order], k[order]
        usd = r["px"] * r["sz"]
        for b in np.unique(k):
            m = k == b
            px, sz = r["px"][m], r["sz"][m]
            out.append({"t": start + int(b) * tf, "o": float(px[0]), "h": float(px.max()), "l": float(px.min()), "c": float(px[-1]),
                        "v": float(sz.sum()), "vb": round(float(usd[m][r["side"][m] > 0].sum()), 2), "vs": round(float(usd[m][r["side"][m] < 0].sum()), 2),
                        "n": int(m.sum())})
        return out

    @staticmethod
    def nice_bucket(mid: float, bps: float) -> float:
        """A price bucket about `bps` of the mid, rounded to 1, 2 or 5 x 10^k so rows line up."""
        raw = max(mid * bps / 1e4, 1e-9)
        k = 10 ** math.floor(math.log10(raw))
        return min((m * k for m in (1, 2, 5, 10)), key=lambda v: abs(math.log(v / raw)))   # the nearest nice size, not the next one up

    def footprint(self, pair: str, tf: float, n: int, bucket_bps: float = 2.0, venues: set[str] | None = None, to: float | None = None,
                  imb_ratio: float = 3.0, imb_min_usd: float = 0.0, bubble_usd: float = 0.0, bands: bool = True) -> dict:
        """Per candle: the prints bucketed by price into bid (sells, at the bid) and ask (buys, at
        the ask) USD, the point of control, the 70% value area, stacked diagonal imbalances (buys
        at a price against sells one bucket lower, or the reverse, past `imb_ratio` to one), the
        running delta's low and high inside the bar, the largest prints, and CVD by size band."""
        to = time.time() if to is None else float(to)
        end = (int(to // tf) + 1) * tf
        start = end - n * tf
        r = self.rows(pair, since=start, venues=venues)
        out = {"pair": pair, "tf": tf, "t0": start, "bucket": None, "candles": [], "bands": BAND_LABELS}
        if not len(r):
            return out
        r = r[r["t"] < end]
        if not len(r):
            return out
        order = np.argsort(r["t"], kind="stable")
        r = r[order]
        mid = float(np.median(r["px"]))
        bucket = self.nice_bucket(mid, bucket_bps)
        out["bucket"] = bucket
        usd = r["px"] * r["sz"]
        k = ((r["t"] - start) // tf).astype(int)
        lvl = np.floor(r["px"] / bucket + 1e-9).astype(np.int64)
        band = self.band_index(usd) if bands else None
        for b in np.unique(k):
            m = k == b
            px, u, sd, lv = r["px"][m], usd[m], r["side"][m], lvl[m]
            levels = np.unique(lv)
            rows = []
            for L in levels:
                mm = lv == L
                buy, sell = float(u[mm & (sd > 0)].sum()), float(u[mm & (sd < 0)].sum())
                rows.append({"px": round(float(L) * bucket, 10), "b": round(sell, 2), "a": round(buy, 2)})   # b: traded at the bid (sells), a: at the ask (buys)
            tot = [x["a"] + x["b"] for x in rows]
            poc_i = int(np.argmax(tot)) if rows else 0
            # the value area: expand from the POC until 70% of the bar's volume is inside
            total = sum(tot) or 1.0
            lo_i = hi_i = poc_i
            acc = tot[poc_i]
            while acc < 0.7 * total and (lo_i > 0 or hi_i < len(rows) - 1):
                up = tot[hi_i + 1] if hi_i < len(rows) - 1 else -1
                dn = tot[lo_i - 1] if lo_i > 0 else -1
                if up >= dn:
                    hi_i += 1; acc += up
                else:
                    lo_i -= 1; acc += dn
            imb = []
            for i in range(len(rows)):
                a_here = rows[i]["a"]
                b_below = rows[i - 1]["b"] if i > 0 else 0.0
                if a_here >= imb_min_usd and a_here > 0 and (b_below == 0 or a_here / b_below >= imb_ratio) and (i > 0):
                    imb.append({"px": rows[i]["px"], "side": 1})
                b_here = rows[i]["b"]
                a_above = rows[i + 1]["a"] if i < len(rows) - 1 else 0.0
                if b_here >= imb_min_usd and b_here > 0 and (a_above == 0 or b_here / a_above >= imb_ratio) and (i < len(rows) - 1):
                    imb.append({"px": rows[i]["px"], "side": -1})
            cum = np.cumsum(np.where(sd > 0, u, -u))
            big = []
            if bubble_usd > 0:
                bm = u >= bubble_usd
                for j in np.where(bm)[0][:40]:
                    big.append({"px": float(px[j]), "usd": round(float(u[j]), 2), "side": int(sd[j]), "t": float(r["t"][m][j])})
            cvd_bands = None
            if bands:
                bb = band[m]
                cvd_bands = [round(float(u[(bb == i) & (sd > 0)].sum() - u[(bb == i) & (sd < 0)].sum()), 2) for i in range(len(BANDS))]
            out["candles"].append({"t": start + int(b) * tf, "rows": rows, "poc": rows[poc_i]["px"] if rows else None,
                                   "va": [rows[lo_i]["px"], rows[hi_i]["px"]] if rows else None, "imb": imb,
                                   "delta": round(float(cum[-1]), 2) if len(cum) else 0.0, "delta_min": round(float(cum.min()), 2) if len(cum) else 0.0,
                                   "delta_max": round(float(cum.max()), 2) if len(cum) else 0.0, "buy": round(float(u[sd > 0].sum()), 2),
                                   "sell": round(float(u[sd < 0].sum()), 2), "n": int(m.sum()), "big": big, "cvd_bands": cvd_bands,
                                   "max_print": round(float(u.max()), 2) if len(u) else 0.0})
        return out

    def venue_vwap(self, pair: str, tf: float, n: int, to: float | None = None) -> dict:
        """Each venue's volume-weighted price per bar over the last `n` bars: what a premium is
        measured from. {venue: [price or None per bar]}, plus the bar times."""
        to = time.time() if to is None else float(to)
        end = (int(to // tf) + 1) * tf
        start = end - n * tf
        r = self.rows(pair, since=start)
        out: dict[str, list] = {}
        if len(r):
            r = r[r["t"] < end]
            k = ((r["t"] - start) // tf).astype(int)
            usd = r["px"] * r["sz"]
            for v in np.unique(r["v"]):
                name = self.venues[int(v)] if int(v) < len(self.venues) else str(v)
                m = r["v"] == v
                num = np.bincount(k[m], weights=usd[m], minlength=n)[:n]
                den = np.bincount(k[m], weights=r["sz"][m], minlength=n)[:n]
                out[name] = [round(float(a / b), 8) if b > 0 else None for a, b in zip(num, den)]
        return {"pair": pair, "t0": start, "tf": tf, "n": n, "venues": out}

    def status(self) -> dict:
        with self._lock:
            held = int(sum(self.size.values()))
            oldest = min((float(self.rings[p]["t"][:self.size[p]].min()) for p in self.rings if self.size[p]), default=None)
        return {"pairs": len(self.rings), "cap_per_pair": self.cap, "prints": held, "total": self.count, "mb": round(held * DT.itemsize / 1e6, 1),
                "budget_mb": self.mb, "oldest": oldest, "venues": list(self.venues)}
