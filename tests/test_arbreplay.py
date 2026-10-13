"""The arbitrage replay: latency, edge, cooldown and inventory rules over canned episodes."""
import json

from steel import arbreplay


def ep(sym, buy, sell, start, window_ms, mean_bps, kind="spot", fees=30.0, size=None):
    return {"sym": sym, "quote": "USD", "kind": kind, "buy": buy, "sell": sell, "start": start, "end": start + window_ms / 1000,
            "window_ms": window_ms, "peak_bps": mean_bps + 5, "mean_bps": mean_bps, "gross_peak_bps": mean_bps + 5 + fees,
            "fees_bps": fees, "samples": 3, "size_usd": size}


def test_replay_rules(tmp_path):
    eps = [ep("BTC-USD", "kraken", "coinbase", 1000, 8, 40.0),                  # 8 ms: too fast for anyone
           ep("BTC-USD", "kraken", "coinbase", 1100, 2000, 20.0, size=100.0),    # capturable, size-limited to 100 USD
           ep("BTC-USD", "kraken", "coinbase", 1120, 2000, 20.0),                # 20 s later: cooling down
           ep("ETH-USD", "okx", "bybit", 1200, 5000, 12.0, kind="perp", fees=9.5),   # a perp pair pays a second round: 2.5 bps net, thin
           ep("ETH-USD", "okx", "bybit", 1300, 5000, 25.0, kind="perp", fees=9.5),   # 15.5 bps net
           ep("SOL-USD", "coinbase", "kraken", 2000, 1000, 10.0),
           ep("SOL-USD", "coinbase", "kraken", 2100, 1000, 10.0),
           ep("SOL-USD", "coinbase", "kraken", 2200, 1000, 10.0)]
    r = arbreplay.replay(eps, latency_ms=300, min_edge_bps=5, max_order_usd=200, cooldown_s=60, inventory_usd=400)
    assert r["episodes"] == 8 and r["too_fast"] == 1 and r["cooling"] == 1 and r["too_thin"] == 1 and r["captured"] == 5
    # BTC: 100 USD x 20 bps = 0.20; ETH: 200 x 15.5 bps = 0.31; SOL: three of 200 x 10 bps = 0.20 each, and the second one
    # pushes the one-way flow to 400 = the inventory cap, so a rebalance (two USDC transfers, 1.00) is charged
    assert r["transfers_usd"] == 1.0 and abs(r["usd"] - (0.20 + 0.31 + 0.60 - 1.0)) < 1e-9
    pairs = {(p["sym"], p["kind"]): p for p in r["pairs"]}
    assert pairs[("SOL-USD", "spot")]["n"] == 3 and pairs[("SOL-USD", "spot")]["transfers"] == 1.0 and pairs[("ETH-USD", "perp")]["mean_bps"] == 15.5
    slow = arbreplay.replay(eps, latency_ms=3000, min_edge_bps=5, max_order_usd=200, cooldown_s=60)
    assert slow["captured"] == 1 and slow["pairs"][0]["sym"] == "ETH-USD"
    # opposite flows net out: no rebalance
    both = [ep("SOL-USD", "coinbase" if i % 2 == 0 else "kraken", "kraken" if i % 2 == 0 else "coinbase", 1 + 100 * i, 1000, 10.0) for i in range(6)]
    r2 = arbreplay.replay(both, latency_ms=300, cooldown_s=0, inventory_usd=400)
    assert r2["captured"] == 6 and r2["transfers_usd"] == 0.0
    # a crossed book 400 bps wide is a bad quote, not a trade
    wide = [ep("NIGHT-USD", "htx", "okx", 1, 5000, 400.0)]
    r3 = arbreplay.replay(wide, latency_ms=300)
    assert r3["suspect"] == 1 and r3["captured"] == 0 and r3["suspect_venues"] == {"htx": 1, "okx": 1}


def test_summary_reads_the_log_and_ladders(tmp_path):
    p = tmp_path / "d.jsonl"
    now = 10_000_000.0
    rows = [ep("BTC-USD", "kraken", "coinbase", now - 100, 500, 15.0), ep("BTC-USD", "kraken", "coinbase", now - 10 * 86400, 500, 15.0), "not json"]
    p.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n")
    s = arbreplay.summary(p, days=7, now=now, min_edge_bps=5, max_order_usd=200, cooldown_s=60)
    assert s["episodes"] == 1 and [r["latency_ms"] for r in s["ladder"]] == list(arbreplay.LADDER)
    assert s["ladder"][2]["captured"] == 1 and s["ladder"][3]["captured"] == 0 and s["pairs"][0]["sym"] == "BTC-USD"
    assert "500 ms" in arbreplay.fmt(s) or "median 500" in arbreplay.fmt(s)
    assert arbreplay.summary(tmp_path / "missing.jsonl", days=7, now=now)["episodes"] == 0
