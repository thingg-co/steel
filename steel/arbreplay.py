"""What the arbitrage rules would have captured, replayed over the dislocation log.

Each line of `state/dislocations.jsonl` is one episode: a crossed book between two venues on
one name, how long it lasted, its peak and mean net edge after two taker fees, and the size
the top of book showed. The replay applies the executor's rules with a latency assumption:
an episode counts only when it outlasted the round trip (`latency_ms`), at its mean net edge
(never the peak), at the smaller of the book's size and the order cap, after the cooldown per
name. Inventory is the honest part: each capture moves base to the buy venue and cash to the
sell venue, and once the one-way flow on a pair passes `inventory_usd` it has to be rebalanced
on-chain, two stablecoin-rail transfers' worth (`fees.transfer_cost`), charged to the pair.
A ladder of latencies shows how fast the hands would have to be.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .sources.fees import transfer_cost

LADDER = (50.0, 100.0, 300.0, 1000.0, 3000.0)


def load(path: str | Path, since: float | None = None, limit: int = 0) -> list[dict]:
    """The episodes, oldest first; `since` is a unix time, `limit` keeps the newest N."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if since is not None and float(r.get("start") or 0.0) < since:
                    continue
                out.append(r)
    except OSError:
        return []
    out.sort(key=lambda r: float(r.get("start") or 0.0))
    return out[-limit:] if limit else out


def replay(eps: list[dict], *, latency_ms: float = 300.0, min_edge_bps: float = 5.0, max_order_usd: float = 200.0,
           cooldown_s: float = 60.0, inventory_usd: float | None = None, stable: str = "USDC", max_edge_bps: float = 100.0) -> dict:
    """`max_edge_bps`: a crossed book wider than this is a data problem (a stale or mis-mapped
    quote, two coins with one ticker), not an arbitrage; those episodes are counted as suspect
    and never captured."""
    inv = float(inventory_usd if inventory_usd is not None else 2.0 * max_order_usd)
    rebalance_fee = 2.0 * transfer_cost(stable, inv, 1.0).fee_usd      # cash one way, base the other, both on a cheap rail
    last: dict[str, float] = {}
    flow: dict[tuple, float] = defaultdict(float)
    by_pair: dict[tuple, dict] = {}
    n = n_fast = n_thin = n_cool = n_took = n_suspect = 0
    usd = transfers = 0.0
    bps_sum = 0.0
    suspect_venues: dict[str, int] = defaultdict(int)
    for r in eps:
        n += 1
        if float(r.get("gross_peak_bps") or r.get("peak_bps") or 0.0) > max_edge_bps:
            n_suspect += 1
            suspect_venues[r.get("buy", "?")] += 1
            suspect_venues[r.get("sell", "?")] += 1
            continue
        if float(r.get("window_ms") or 0.0) < latency_ms:
            n_fast += 1
            continue
        edge = float(r.get("mean_bps") or 0.0)
        if r.get("kind") == "perp":
            edge -= float(r.get("fees_bps") or 0.0)       # a perp pair is closed again when the gap shuts: a second round of fees
        if edge < min_edge_bps:
            n_thin += 1
            continue
        t0 = float(r.get("start") or 0.0)
        if t0 < last.get(r["sym"], -1e18) + cooldown_s:
            n_cool += 1
            continue
        last[r["sym"]] = t0
        size = min(max_order_usd, float(r["size_usd"])) if r.get("size_usd") else max_order_usd
        gain = size * edge / 1e4
        key = (r["sym"], *sorted((r["buy"], r["sell"])))
        signed = size if r["buy"] == key[1] else -size
        flow[key] += signed
        cost = 0.0
        if abs(flow[key]) >= inv:                           # inventory piled up on one side: move it back
            cost = rebalance_fee
            flow[key] = 0.0
            transfers += cost
        n_took += 1
        usd += gain - cost
        bps_sum += edge
        p = by_pair.setdefault((r["sym"], r["buy"], r["sell"], r.get("kind", "spot")), {"sym": r["sym"], "buy": r["buy"], "sell": r["sell"], "kind": r.get("kind", "spot"),
                                                                                       "n": 0, "usd": 0.0, "bps": 0.0, "transfers": 0.0})
        p["n"] += 1
        p["usd"] += gain - cost
        p["bps"] += edge
        p["transfers"] += cost
    pairs = sorted(by_pair.values(), key=lambda p: -p["usd"])
    for p in pairs:
        p["mean_bps"] = round(p["bps"] / p["n"], 2)
        p["usd"] = round(p["usd"], 2)
        p["transfers"] = round(p["transfers"], 2)
        del p["bps"]
    return {"latency_ms": latency_ms, "min_edge_bps": min_edge_bps, "max_order_usd": max_order_usd, "inventory_usd": inv,
            "rebalance_fee_usd": round(rebalance_fee, 2), "max_edge_bps": max_edge_bps, "episodes": n, "suspect": n_suspect,
            "suspect_venues": dict(sorted(suspect_venues.items(), key=lambda kv: -kv[1])[:6]),
            "too_fast": n_fast, "too_thin": n_thin, "cooling": n_cool, "captured": n_took, "usd": round(usd, 2), "transfers_usd": round(transfers, 2),
            "mean_bps": round(bps_sum / n_took, 2) if n_took else 0.0, "pairs": pairs[:15]}


def summary(path: str | Path, *, days: float = 7.0, ladder=LADDER, now: float | None = None, **kw) -> dict:
    now = time.time() if now is None else now
    eps = load(path, since=now - days * 86400.0)
    windows = sorted(float(r.get("window_ms") or 0.0) for r in eps)
    out = {"path": str(path), "days": days, "episodes": len(eps), "computed": now,
           "window_ms": {"median": statistics.median(windows) if windows else 0.0,
                         "p90": windows[int(0.9 * (len(windows) - 1))] if windows else 0.0,
                         "over_300": sum(1 for w in windows if w >= 300.0), "over_1000": sum(1 for w in windows if w >= 1000.0)},
           "first": float(eps[0]["start"]) if eps else None, "last": float(eps[-1]["start"]) if eps else None,
           "ladder": [replay(eps, latency_ms=L, **kw) for L in ladder]}
    out["pairs"] = out["ladder"][2]["pairs"] if len(out["ladder"]) > 2 else []   # the 300 ms rung's pairs: the honest default
    return out


def fmt(s: dict) -> str:
    sus = s["ladder"][0] if s["ladder"] else {}
    lines = [f"{s['episodes']} episodes over {s['days']:g} days from {s['path']}",
             (f"{sus.get('suspect', 0)} wider than {sus.get('max_edge_bps', 0):g} bps are data problems, not trades (by venue: "
              + ", ".join(f"{v} {n}" for v, n in (sus.get("suspect_venues") or {}).items()) + ")") if sus.get("suspect") else "no suspect episodes",
             f"window: median {s['window_ms']['median']:.0f} ms, p90 {s['window_ms']['p90']:.0f} ms, "
             f"{s['window_ms']['over_300']} lasted 300 ms+, {s['window_ms']['over_1000']} lasted 1 s+", "",
             f"{'latency':>8} {'captured':>9} {'too fast':>9} {'too thin':>9} {'cooling':>8} {'mean bps':>9} {'net USD':>9} {'transfers':>10}"]
    for r in s["ladder"]:
        lines.append(f"{r['latency_ms']:>6.0f}ms {r['captured']:>9} {r['too_fast']:>9} {r['too_thin']:>9} {r['cooling']:>8} {r['mean_bps']:>9.2f} {r['usd']:>9.2f} {r['transfers_usd']:>10.2f}")
    if s["pairs"]:
        lines += ["", "best pairs at 300 ms:"]
        for p in s["pairs"][:10]:
            lines.append(f"  {p['sym']:<10} buy {p['buy']:<12} sell {p['sell']:<12} {p['kind']:<4} {p['n']:>5} x {p['mean_bps']:>6.2f} bps = {p['usd']:>8.2f} USD (transfers {p['transfers']:.2f})")
    return "\n".join(lines)
