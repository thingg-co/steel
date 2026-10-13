"""The venue fee table: every venue has a profile, the override file applies, and the
policy's edge charges the fees of the venue a position would trade on."""
import json

from steel.engine import Engine
from steel.sources import REGISTRY
from steel.sources.fees import DEFAULT, FEES, fee_profile, round_trip_bps
from test_gates import make_fake, make_pred


def test_every_venue_has_a_profile_in_plausible_ranges():
    for cls in REGISTRY:
        if cls.id == "coingecko":
            continue
        p = fee_profile(cls.id)
        assert p.venue == cls.id and p is FEES[cls.id], cls.id
        assert 0 <= p.spot_maker_bps <= p.spot_taker_bps <= 150 and 0 <= p.perp_maker_bps <= p.perp_taker_bps <= 20
        assert p.funding_hours in (1.0, 4.0, 8.0) and 0 <= p.perp_maintenance <= 0.05
        if cls.venue == "perp":
            assert p.perp_taker_bps > 0, f"{cls.id} is a perp venue without a perp fee"
        else:
            assert p.spot_taker_bps > 0, f"{cls.id} is a spot venue without a spot fee"
    assert fee_profile("nope").spot_taker_bps == DEFAULT.spot_taker_bps and fee_profile("nope").venue == "nope"
    assert round_trip_bps(fee_profile("hyperliquid"), perp=True) == 9.0 and round_trip_bps(fee_profile("coinbase"), perp=False) == 80.0


def test_override_file_changes_a_venue(tmp_path, monkeypatch):
    f = tmp_path / "fees.json"
    f.write_text(json.dumps({"coinbase": {"spot_taker_bps": 5, "bogus": 1}, "hyperliquid": {"funding_hours": 4}}))
    monkeypatch.setenv("STEEL_FEES", str(f))
    assert fee_profile("coinbase").spot_taker_bps == 5.0 and fee_profile("coinbase").spot_maker_bps == 25.0
    assert fee_profile("hyperliquid").funding_hours == 4.0 and fee_profile("bybit").perp_taker_bps == 5.5
    monkeypatch.setenv("STEEL_FEES", str(tmp_path / "missing.json"))
    assert fee_profile("coinbase").spot_taker_bps == 40.0


def test_edge_charges_the_trading_venues_fees():
    q = [-10, 0, 60, 90, 120]                                                # a 60 bps bullish median over a 10 bps spread
    pred = make_pred(q, 0.75, 0.15)
    fees = {"spot": fee_profile("coinbase"), "perp": fee_profile("hyperliquid")}
    spot = make_fake(fees=fees, cfg={"confirm_bars": 1, "long_mode": "spot", "spot_fee_bps": 0.0, "perp_fee_bps": 0.0})
    r = Engine._suggest(spot, 0, "BTC-USD", pred)
    assert r["action"] == "HOLD" and "80 bps of spot fees" in r["why"], r["why"]   # Coinbase retail: 40 a side kills a 60 bps hour
    perp = make_fake(fees=fees, cfg={"confirm_bars": 1, "long_mode": "perp", "spot_fee_bps": 0.0, "perp_fee_bps": 0.0})
    r = Engine._suggest(perp, 0, "BTC-USD", pred)
    assert r["action"] == "BUY" and r["venue"] == "hyperliquid", r["why"]            # 9 bps round trip on the perp venue
    paid = make_fake(fees=fees, funding={"BTC-USD": 0.01}, cfg={"confirm_bars": 1, "long_mode": "perp", "spot_fee_bps": 0.0, "perp_fee_bps": 0.0, "funding_interval_hours": 0.0})
    r = Engine._suggest(paid, 0, "BTC-USD", pred)
    assert r["action"] == "HOLD" and "funding" in r["why"], r["why"]                 # a long pays a positive rate: 1% an hour on Hyperliquid's hourly clock
    earned = make_fake(fees=fees, funding={"BTC-USD": -0.01}, cfg={"confirm_bars": 1, "long_mode": "perp", "spot_fee_bps": 0.0, "perp_fee_bps": 0.0, "funding_interval_hours": 0.0})
    assert Engine._suggest(earned, 0, "BTC-USD", pred)["action"] == "BUY"             # a negative rate pays the long: never charged, never credited
    override = make_fake(fees=fees, cfg={"confirm_bars": 1, "long_mode": "spot", "spot_fee_bps": 2.0, "perp_fee_bps": 0.0})
    assert Engine._suggest(override, 0, "BTC-USD", pred)["action"] == "BUY"           # the knob beats the profile
