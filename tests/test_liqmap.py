"""The modeled liquidation map: clusters open where OI rose, split by the taker share, at each
band's liquidation distance; shrink when OI falls; are spent when price trades through."""
from steel.liqmap import liquidation_map


def test_liquidation_map_opens_shrinks_and_spends_clusters():
    bars = [{"t": 0, "o": 100, "h": 100.5, "l": 99.5, "c": 100.0, "buy": 300, "sell": 100},   # OI +1,000 here: 75% longs
            {"t": 300, "o": 100, "h": 100.2, "l": 99.8, "c": 100.0, "buy": 100, "sell": 100},  # OI flat
            {"t": 600, "o": 100, "h": 100.1, "l": 97.0, "c": 98.0, "buy": 50, "sell": 150}]     # trades down to 97: the 50x, 75x and 100x long clusters (98.0, 98.7, 99.0) are spent
    oi = [(-1, 10_000.0), (1, 11_000.0), (301, 11_000.0), (601, 11_000.0)]
    m = liquidation_map(bars, oi, bands=[5, 10, 50, 100], maintenance=0.0, bucket_bps=10.0, keep_states=10)
    assert m["bucket"] == 0.1 and m["oi_samples"] == 4 and len(m["states"]) == 3
    first = {(b, L, s): v for b, L, s, v in m["states"][0]["cells"]}
    assert abs(first[(int(80.0 / 0.1 + 1e-9), 5, -1)] - 1000 / 4 * 0.75) < 1e-6              # 5x long liquidates at 80: 250 x 75%
    assert abs(first[(int(round(120.0 / 0.1)) if False else int(120.0 / 0.1 + 1e-9), 5, 1)] - 1000 / 4 * 0.25) < 1e-6   # 5x short at 120
    assert len(first) == 8
    last = {(b, L, s): v for b, L, s, v in m["states"][-1]["cells"]}
    assert (int(98.0 / 0.1 + 1e-9), 50, -1) not in last and (int(99.0 / 0.1 + 1e-9), 100, -1) not in last    # spent on the way down
    assert (int(90.0 / 0.1 + 1e-9), 10, -1) in last and (int(80.0 / 0.1 + 1e-9), 5, -1) in last                # still there below
    assert m["total"]["long"] < m["total"]["short"] + 1e-9 or m["total"]["short"] == round(1000 / 4 * 0.25 * 4, 2)
    # OI falling shrinks every cluster
    bars2 = bars[:2] + [{"t": 600, "o": 100, "h": 100.1, "l": 99.9, "c": 100.0, "buy": 100, "sell": 100}]
    oi2 = oi[:3] + [(601, 10_500.0)]
    m2 = liquidation_map(bars2, oi2, bands=[5, 10, 50, 100], maintenance=0.0, bucket_bps=10.0)
    half = {(b, L, s): v for b, L, s, v in m2["now"]}
    assert abs(half[(int(80.0 / 0.1 + 1e-9), 5, -1)] - 1000 / 4 * 0.75 * (10_500 / 11_000)) < 0.01
    assert liquidation_map([], [])["now"] == [] and "modeled" in m["note"]


def test_calibration_scores_real_liquidations_against_the_map():
    from steel.liqmap import calibrate
    states = [{"t": 100.0, "cells": [[90, 10, -1, 5000.0], [110, 10, 1, 3000.0]]},        # bucket 10: longs modeled at 905, shorts at 1105
              {"t": 400.0, "cells": [[90, 10, -1, 5000.0]]}]
    events = [{"t": 150.0, "px": 915.0, "usd": 100.0, "side": -1},    # a long liquidated 1 bucket from the modeled cluster: explained
              {"t": 150.0, "px": 1105.0, "usd": 50.0, "side": 1},     # a short on the cluster: explained
              {"t": 450.0, "px": 1105.0, "usd": 30.0, "side": 1},     # the short cluster was gone by then: not explained
              {"t": 450.0, "px": 700.0, "usd": 20.0, "side": -1},     # far away: not explained
              {"t": 50.0, "px": 905.0, "usd": 999.0, "side": -1}]     # before the first state: not scored
    c = calibrate(states, events, 10.0, mid=1000.0)
    assert c["events"] == 4 and c["explained"] == 2 and c["usd"] == 200.0 and c["explained_usd"] == 150.0 and c["share_usd"] == 0.75
    assert c["tol_bps"] == 300.0 and "not real positions" in c["method"]
    assert calibrate([], events, 10.0)["share_usd"] is None and calibrate(states, [], 10.0)["events"] == 0
