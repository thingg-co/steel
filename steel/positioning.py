"""Net positioning and absorption, from open-interest changes and the taker side per bar.

MMT's rule, which is the only honest one without position-level data: open interest rises
only when a position is created and falls only when one is closed, so a bar's aggressive flow
counts toward positioning only as far as OI confirms it. OI up with buyers taking: new longs.
OI up with sellers taking: new shorts. OI down with sellers taking: longs closing. OI down
with buyers taking: shorts covering. Heavy volume with flat OI is positions changing hands
and adds nothing. Net longs and net shorts accumulate from zero at the window's start.

Absorption flags a bar where new positioning was large next to the bars before it and price
moved less than usual for that much positioning: resting size on the other side soaked up
the flow. Sensitivity (10 to 90) sets how many bars are flagged."""
from __future__ import annotations

import math

import numpy as np


def positioning(bars: list[dict], oi: list[tuple[float, float]], *, bar_s: float = 300.0, sensitivity: float = 50.0, lookback: int = 20) -> dict:
    """bars: [{t, o, h, l, c, buy, sell}] oldest first (buy/sell in USD); oi: [(t, oi_usd)].
    Returns per-bar rows and the cumulative series."""
    if not bars:
        return {"rows": [], "net_long": [], "net_short": [], "absorption": [], "oi_samples": int(len(oi))}
    oi_t = np.array([t for t, _ in oi], dtype=float) if oi else np.zeros(0)
    oi_v = np.array([v for _, v in oi], dtype=float) if oi else np.zeros(0)
    def oi_at(t):
        if not len(oi_t):
            return None
        i = int(np.searchsorted(oi_t, t, side="right")) - 1
        return float(oi_v[i]) if i >= 0 else None
    rows = []
    prev = oi_at(bars[0]["t"])
    nl = ns = 0.0
    net_long, net_short, absorption = [], [], []
    sizes, moves = [], []
    thresh = 2.2 - 1.6 * max(0.0, min(1.0, (sensitivity - 10) / 80))          # 10 -> z 2.2 (few flags), 90 -> z 0.6 (many)
    for b in bars:
        cur = oi_at(b["t"] + bar_s - 1)
        buy, sell = float(b.get("buy") or 0.0), float(b.get("sell") or 0.0)
        tot = buy + sell
        d = (cur - prev) if (cur is not None and prev is not None) else 0.0
        share = buy / tot if tot > 0 else 0.5
        opened_long = opened_short = closed_long = closed_short = 0.0
        if d > 0:
            opened_long, opened_short = d * share, d * (1 - share)
        elif d < 0:
            closed_long, closed_short = -d * (1 - share), -d * share                 # sellers closing longs, buyers covering shorts
        nl += opened_long - closed_long
        ns += opened_short - closed_short
        c, o = float(b.get("c") or 0), float(b.get("o") or 0)
        move = abs(math.log(c / o)) * 1e4 if c > 0 and o > 0 else 0.0
        new = opened_long + opened_short
        flag = False
        if d > 0 and len(sizes) >= 5:
            mu, sd = float(np.mean(sizes)), float(np.std(sizes)) or 1.0
            z = (new - mu) / sd
            typical = float(np.mean([m for m, s_ in zip(moves, sizes) if s_ > 0] or [0.0]))
            flag = z >= thresh and move <= 0.6 * typical and new > 0
        sizes.append(new); moves.append(move)
        del sizes[:-lookback]; del moves[:-lookback]
        rows.append({"t": b["t"], "oi": cur, "d_oi": round(d, 2), "opened_long": round(opened_long, 2), "opened_short": round(opened_short, 2),
                     "closed_long": round(closed_long, 2), "closed_short": round(closed_short, 2), "absorption": flag, "move_bps": round(move, 1)})
        net_long.append(round(nl, 2)); net_short.append(round(ns, 2)); absorption.append(flag)
        if cur is not None:
            prev = cur
    return {"rows": rows, "net_long": net_long, "net_short": net_short, "absorption": absorption, "oi_samples": int(len(oi_t)),
            "sensitivity": sensitivity, "note": "OI-confirmed flow only: heavy volume on flat OI is positions changing hands"}
