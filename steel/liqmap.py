"""A modeled liquidation map: where leveraged positions opened in the past would be forced
to close, by leverage band, as MMT's learn page describes it and as every such map that does
not see real positions must. The model: when open interest rose across a bar, that much new
notional opened near the bar's close, split long against short by the bar's taker share; at
each leverage L a long liquidates near entry x (1 - (1 - m) / L) and a short near entry x (1 +
(1 - m) / L); the notional is spread evenly over the bands; when open interest fell, every
cluster shrinks in proportion; a cluster is spent once price trades through it. Hyperliquid
publishes real levels (M26); everything here is an estimate and says so."""
from __future__ import annotations

import math

import numpy as np

BANDS = [5, 10, 25, 50, 75, 100]


def nice(mid: float, bps: float) -> float:
    raw = max(mid * bps / 1e4, 1e-9)
    k = 10 ** math.floor(math.log10(raw))
    return min((m * k for m in (1, 2, 5, 10)), key=lambda v: abs(math.log(v / raw)))


def liquidation_map(bars: list[dict], oi: list[tuple[float, float]], *, bands=BANDS, maintenance: float = 0.005, bucket_bps: float = 10.0,
                    keep_states: int = 288, bar_s: float = 300.0) -> dict:
    """bars: [{t, o, h, l, c, buy, sell}] oldest first; oi: [(t, oi_usd)] samples. Returns the
    current clusters and the per-bar states (the last `keep_states`) for a heatmap."""
    if not bars:
        return {"bucket": None, "bands": list(bands), "now": [], "states": [], "note": "no bars"}
    mid = float(np.median([b["c"] for b in bars if b.get("c")]))
    bk = nice(mid, bucket_bps)
    oi_t = np.array([t for t, _ in oi], dtype=float) if oi else np.zeros(0)
    oi_v = np.array([v for _, v in oi], dtype=float) if oi else np.zeros(0)
    def oi_at(t):
        if not len(oi_t):
            return None
        i = int(np.searchsorted(oi_t, t, side="right")) - 1
        return float(oi_v[i]) if i >= 0 else None
    clusters: dict[tuple[int, int, int], float] = {}          # (bucket, band, side) -> USD; side -1 longs (below), +1 shorts (above)
    states = []
    prev = oi_at(bars[0]["t"])                                   # the open interest as the first bar opened
    for b in bars:
        c = float(b.get("c") or 0)
        if c <= 0:
            continue
        cur = oi_at(b["t"] + bar_s - 1)                          # as the bar closed
        if prev is not None and cur is not None:
            d = cur - prev
            if d > 0:
                buy, sell = float(b.get("buy") or 0), float(b.get("sell") or 0)
                share = buy / (buy + sell) if buy + sell > 0 else 0.5
                per = d / len(bands)
                for L in bands:
                    lp = c * (1 - (1 - maintenance) / L)
                    sp = c * (1 + (1 - maintenance) / L)
                    kl, ks = int(math.floor(lp / bk + 1e-9)), int(math.floor(sp / bk + 1e-9))
                    clusters[(kl, L, -1)] = clusters.get((kl, L, -1), 0.0) + per * share
                    clusters[(ks, L, 1)] = clusters.get((ks, L, 1), 0.0) + per * (1 - share)
            elif d < 0 and prev > 0:
                f = max(0.0, cur / prev)
                for k in list(clusters):
                    clusters[k] *= f
        if cur is not None:
            prev = cur
        # spent: price traded through the level inside this bar
        lo, hi = float(b.get("l") or c), float(b.get("h") or c)
        for (bucket, L, side) in list(clusters):
            px = (bucket + 0.5) * bk
            if (side < 0 and lo <= px) or (side > 0 and hi >= px):
                del clusters[(bucket, L, side)]
        for k in [k for k, v in clusters.items() if v < 1.0]:
            del clusters[k]
        states.append({"t": b["t"], "cells": [[bucket, L, side, round(v, 2)] for (bucket, L, side), v in clusters.items()]})
    now = [[bucket, L, side, round(v, 2)] for (bucket, L, side), v in sorted(clusters.items())]
    total = {"long": round(sum(v for (_, _, s), v in clusters.items() if s < 0), 2), "short": round(sum(v for (_, _, s), v in clusters.items() if s > 0), 2)}
    return {"bucket": bk, "bands": list(bands), "maintenance": maintenance, "now": now, "total": total, "states": states[-keep_states:],
            "oi_samples": int(len(oi_t)), "note": "modeled from open-interest changes and leverage bands; not real positions"}


def calibrate(states: list[dict], events: list[dict], bucket: float | None, *, mid: float | None = None, tol_buckets: int = 3) -> dict:
    """How honest the map has been: of the real liquidations the perp venues reported (t, px,
    usd, side: -1 a long), the share (by USD) that fell within `tol_buckets` price buckets of a
    modeled cluster of the same side as the map stood just before the event. 1.0 means every
    real liquidation landed on a modeled cluster; a low share means the map's clusters are not
    where the venues' liquidations happen. The map is modeled from OI changes and leverage
    bands, never from real positions, so this is the only check it has."""
    if not bucket or not states:
        return {"events": 0, "usd": 0.0, "explained": 0, "explained_usd": 0.0, "share_usd": None, "tol_bps": None,
                "method": "modeled from open-interest changes and leverage bands; not real positions"}
    st_t = [s["t"] for s in states]
    n = usd = exp_n = exp_usd = 0.0
    mids = [(s["t"], s) for s in states]
    for ev in events or []:
        t, px, amt, side = float(ev.get("t") or 0), float(ev.get("px") or 0), float(ev.get("usd") or 0), int(ev.get("side") or 0)
        if not px or not side or amt <= 0 or t < st_t[0]:
            continue
        i = int(np.searchsorted(np.asarray(st_t), t, side="right")) - 1
        if i < 0:
            continue
        cells = mids[i][1].get("cells") or []
        k = int(math.floor(px / bucket + 1e-9))
        hit = any(c[2] == side and abs(int(c[0]) - k) <= tol_buckets for c in cells)
        n += 1
        usd += amt
        if hit:
            exp_n += 1
            exp_usd += amt
    tol_bps = round(tol_buckets * bucket / mid * 1e4, 1) if mid else None
    return {"events": int(n), "usd": round(usd, 2), "explained": int(exp_n), "explained_usd": round(exp_usd, 2),
            "share_usd": round(exp_usd / usd, 3) if usd > 0 else None, "tol_buckets": tol_buckets, "tol_bps": tol_bps,
            "method": "modeled from open-interest changes and leverage bands; not real positions. The score is the share (USD) of the venues' real "
                      "liquidations that fell on a modeled cluster of the same side as the map stood before each"}
