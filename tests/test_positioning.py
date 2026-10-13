"""Net positioning from OI-confirmed flow, and absorption."""
from steel.positioning import positioning


def test_positioning_rules_and_absorption():
    bars = [{"t": i * 300, "o": 100.0, "h": 100.1, "l": 99.9, "c": 100.05 if i != 7 else 100.0, "buy": 100, "sell": 100} for i in range(8)]
    bars[1].update(buy=300, sell=100)       # OI +1,000 with 75% buyers: 750 new longs, 250 new shorts
    bars[2].update(buy=100, sell=300)       # OI -400 with 75% sellers: 300 longs closed, 100 shorts covered
    bars[3].update(buy=500, sell=500)       # OI flat on heavy volume: nothing
    bars[7].update(buy=900, sell=100)       # OI +5,000, price flat: absorption
    oi_vals = [10_000, 10_000, 11_000, 10_600, 10_600, 10_600, 10_600, 10_600, 15_600]
    oi = [(i * 300 - 1, oi_vals[i]) for i in range(9)]       # the sample at a bar's close sits just before the next bar
    oi = [(t if t >= 0 else 0, v) for t, v in oi]
    out = positioning(bars, oi, bar_s=300.0, sensitivity=50.0, lookback=20)
    r = out["rows"]
    assert r[1]["opened_long"] == 750.0 and r[1]["opened_short"] == 250.0 and out["net_long"][1] == 750.0 and out["net_short"][1] == 250.0
    assert r[2]["closed_long"] == 300.0 and r[2]["closed_short"] == 100.0 and out["net_long"][2] == 450.0 and out["net_short"][2] == 150.0
    assert r[3]["d_oi"] == 0.0 and r[3]["opened_long"] == 0.0 and out["net_long"][3] == 450.0
    assert out["absorption"][7] is True and r[7]["opened_long"] == 4500.0 and not any(out["absorption"][:7])
    few = positioning(bars, oi, sensitivity=10.0)
    assert few["absorption"][7] in (True, False) and positioning([], [])["rows"] == []
