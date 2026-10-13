"""Tests for the risk gates in Engine._suggest"""
import collections
import pytest
import types
import numpy as np

from steel.bars import Bar
from steel.learner import Prediction
from steel.config import Config
from steel.engine import Engine

KNOBS = dict(cost_bps=8.0, confirm_bars=2, skill_min_n=8, min_hit_rate=0.5, max_spread_bps=25.0, min_dollar_volume=0.0, long_mode="spot", spot_fee_bps=0.0,
             size_by_coverage=True, move_floor_bps=8.0, score_threshold=0.35, prob_margin=0.06, kelly_fraction=0.15,
             max_size=1.0, perp_fee_bps=0.0, funding_in_edge=True, funding_interval_hours=8.0, short_leverage=2.0,
             impact_eta=0.1, max_impact_bps=10.0, horizon_agree=False, vol_target=0.0)   # pinned so the tests ignore STEEL_* env


def make_fake(**overrides):
    """Create a fake Engine instance with all attributes _suggest touches."""
    cfg = Config()
    for k, v in KNOBS.items():
        setattr(cfg, k, v)
    defaults = {
        "cfg": cfg,
        "metrics": types.SimpleNamespace(
            trusted=True, hit_ema=0.6, coverage_ema=0.8, band_scale=1.0, p_scale=1.0
        ),
        "prices": {"BTC-USD": {"price": 200.0, "bid": 199.9, "ask": 200.1, "ts": 0.0}},
        "bars": {"BTC-USD": collections.deque([Bar(
            ts=1_800_000_000.0, open=200.0, high=200.5, low=199.8, close=200.0,
            volume=1000.0, buy_volume=600.0, sell_volume=400.0, trades=15, spread_bps=5.0
        )])},
        "outcomes": {"BTC-USD": [{"hit": True}] * 7 + [{"hit": False}] * 3},
        "_streak": {},
        "muted": set(),
        "overlay_enabled": False,
        "crowding": {},
        "funding": {},
        "_calibrate": lambda q, p_raw: ([float(v) for v in q], p_raw),
        "_temper": lambda p: p,
    }

    # Apply cfg overrides: merge dict onto Config with setattr
    if "cfg" in overrides:
        cfg_overrides = overrides.pop("cfg")
        for k, v in cfg_overrides.items():
            setattr(defaults["cfg"], k, v)

    # Apply non-cfg overrides
    for k, v in overrides.items():
        defaults[k] = v

    # Return SimpleNamespace with merged defaults; the overlay is a method on the engine
    ns = types.SimpleNamespace(**defaults)
    ns._crowd_overlay = lambda *a: Engine._crowd_overlay(ns, *a)
    return ns


def make_pred(q, p_up, p_down):
    """Create a Prediction with the given quantiles and probabilities."""
    return Prediction(
        q=np.array([q]),  # (1, 5)
        p_up=np.array([p_up]),
        p_down=np.array([p_down]),
        gate=np.zeros(1),
        attn=np.zeros((1, 1))
    )


class TestSuggestGates:
    """Tests for the policy risk gates in Engine._suggest."""

    def test_buy_needs_confirmation(self):
        """First call returns HOLD waiting for confirmation; second call BUYs."""
        fake = make_fake()

        # First call - should HOLD because of confirmation
        result1 = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result1["action"] == "HOLD"
        assert "waiting for the signal to persist (1/2 bars)" in result1["why"]

        # Second call - should BUY after confirmation
        result2 = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result2["action"] == "BUY"
        assert result2["size"] > 0
        assert result2["liq"] is None                      # a long has no liquidation price

    def test_no_track_record_holds(self):
        """No outcomes -> HOLD with no track record message."""
        fake = make_fake(outcomes={"BTC-USD": []})

        # First call
        result1 = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result1["action"] == "HOLD"

        # Second call - still HOLD due to no track record
        result2 = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result2["action"] == "HOLD"
        assert "no track record" in result2["why"]

    def test_low_hit_rate_holds(self):
        """Low hit rate (30%) -> HOLD."""
        fake = make_fake(outcomes={"BTC-USD": [{"hit": True}] * 3 + [{"hit": False}] * 7})

        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result["action"] == "HOLD"
        assert "its record on this name is 30%" in result["why"]

    def test_wide_spread_holds(self):
        """Wide spread (500 bps) -> HOLD."""
        fake = make_fake(prices={"BTC-USD": {"price": 200.0, "bid": 195.0, "ask": 205.0, "ts": 0.0}})

        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result["action"] == "HOLD"
        assert "wider than the 25 bps limit" in result["why"]

    def test_thin_volume_holds(self):
        """A name trading under the daily dollar-volume floor -> HOLD; a flat-seeded one is not judged."""
        fake = make_fake(cfg={"min_dollar_volume": 1e12})
        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result["action"] == "HOLD"
        assert "thin:" in result["why"] and "$1000000M floor" in result["why"]
        seeded = make_fake(cfg={"min_dollar_volume": 1e12}, bars={"BTC-USD": collections.deque([
            Bar(ts=1_800_000_000.0, open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0)])})
        result = Engine._suggest(seeded, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert "thin:" not in result["why"]                # no volume to judge: the flat-bar gate speaks instead

    def test_flat_bar_holds(self):
        """Flat bar (volume 0) -> HOLD."""
        bar = Bar(ts=1_800_000_000.0, open=200.0, high=200.0, low=200.0, close=200.0, volume=0.0)
        fake = make_fake(bars={"BTC-USD": collections.deque([bar])})

        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result["action"] == "HOLD"
        assert "no live trade this bar" in result["why"]

    def test_short_sells_after_confirmation_with_a_liquidation_price(self):
        """Bearish forecast -> SELL after confirmation; the call carries where a 2x short would be liquidated."""
        fake = make_fake()

        # First call - holds due to confirmation
        Engine._suggest(fake, 0, "BTC-USD", make_pred([-100, -70, -40, -10, 20], 0.3, 0.7))

        # Second call - SELL after confirmation
        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-100, -70, -40, -10, 20], 0.3, 0.7))
        assert result["action"] == "SELL"
        assert result["side"] == -1
        assert result["liq"] == pytest.approx(300.0)       # 200 x (1 + 1/2)

    def test_perp_fees_are_charged_against_a_short(self):
        """A bearish edge has to pay the perp's taker fee twice; a long pays none of it."""
        q = [-40, -24, -12, 0, 10]
        fake = make_fake(cfg={"confirm_bars": 1, "perp_fee_bps": 5.5})
        result = Engine._suggest(fake, 0, "BTC-USD", make_pred(q, 0.2, 0.7))
        assert result["action"] == "HOLD" and "perp fees" in result["why"], result["why"]
        long_ = make_fake(cfg={"confirm_bars": 1, "perp_fee_bps": 5.5})
        result = Engine._suggest(long_, 0, "BTC-USD", make_pred([-10, 0, 12, 24, 40], 0.7, 0.2))
        assert result["action"] == "BUY", result["why"]

    def test_size_scales_with_coverage(self):
        """Size scales with coverage_ema: 0.8 -> 0.4 -> 0.1 (floor)."""
        fake_high = make_fake(metrics=types.SimpleNamespace(
            trusted=True, hit_ema=0.6, coverage_ema=0.8, band_scale=1.0, p_scale=1.0
        ))

        fake_mid = make_fake(metrics=types.SimpleNamespace(
            trusted=True, hit_ema=0.6, coverage_ema=0.4, band_scale=1.0, p_scale=1.0
        ))

        fake_low = make_fake(metrics=types.SimpleNamespace(
            trusted=True, hit_ema=0.6, coverage_ema=0.1, band_scale=1.0, p_scale=1.0
        ))

        # Second calls (after confirmation)
        result_high = Engine._suggest(fake_high, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        result_mid = Engine._suggest(fake_mid, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        result_low = Engine._suggest(fake_low, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))

        size_high = result_high["size"]
        size_mid = result_mid["size"]
        size_low = result_low["size"]

        # 0.4 / 0.8 = 0.5
        assert size_mid == pytest.approx(size_high * 0.5, rel=1e-6)
        # 0.1 / 0.8 = 0.125, but floor is 0.25, so 0.25 / 0.8 = 0.3125
        assert size_low == pytest.approx(size_high * 0.25, rel=1e-6)

    def test_muted_holds(self):
        """Muted symbol -> HOLD with muted flag."""
        fake = make_fake(muted={"BTC-USD"})

        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert result["action"] == "HOLD"
        assert result["muted"] is True

    def test_fat_tails_are_flagged_not_traded(self):
        """Both tails fat (P(up) and P(down) over a half) is reported on the call; there is no option to buy."""
        fake = make_fake()
        result = Engine._suggest(fake, 0, "BTC-USD", make_pred([-150, -60, 0, 60, 150], 0.6, 0.6))
        assert result["fat_tails"] is True and result["action"] == "HOLD" and "both tails fat" in result["why"]

    def test_crowd_overlay_fades_a_crowded_long(self):
        """With the overlay on, a long into a crowded name is trimmed and says so."""
        fake = make_fake(overlay_enabled=True, crowding={"BTC-USD": 0.9}, cfg={"confirm_bars": 1})
        r = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert r["base_action"] == "BUY" and "fade the crowd" in r["why"]
        plain = make_fake(cfg={"confirm_bars": 1})
        p = Engine._suggest(plain, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert r["side"] != p["side"] or r["size"] < p["size"]      # trimmed, held or flipped: never the raw call
        assert r["side"] == -1 and "contrarian flip" in r["why"]     # thin edge (score 0.67) into extreme crowding: flipped
        cold = make_fake(overlay_enabled=True, crowding={"BTC-USD": -0.9}, cfg={"confirm_bars": 1})
        c = Engine._suggest(cold, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert c["action"] == "BUY" and c["overlay"] == []   # a long against the crowd is left alone


class TestHorizonAndVol:
    def test_the_longer_view_must_not_disagree(self):
        fake = make_fake(cfg={"horizon_agree": True, "confirm_bars": 1})
        agree = make_pred([-20, 10, 40, 70, 100], 0.7, 0.2)
        agree.horizons = {48: {"q": np.array([[-30, 0, 25, 60, 90]]), "p_up": np.array([0.6]), "p_down": np.array([0.3])}}
        assert Engine._suggest(fake, 0, "BTC-USD", agree)["action"] == "BUY"
        against = make_pred([-20, 10, 40, 70, 100], 0.7, 0.2)
        against.horizons = {48: {"q": np.array([[-90, -60, -25, 0, 30]]), "p_up": np.array([0.3]), "p_down": np.array([0.6])}}
        fake2 = make_fake(cfg={"horizon_agree": True, "confirm_bars": 1})
        r = Engine._suggest(fake2, 0, "BTC-USD", against)
        assert r["action"] == "HOLD" and any("4-hour view disagrees (-25 bps)" in x for x in r["reasons"])
        fake3 = make_fake(cfg={"horizon_agree": False, "confirm_bars": 1})
        assert Engine._suggest(fake3, 0, "BTC-USD", against)["action"] == "BUY"

    def test_size_scales_with_realized_vol(self):
        import math
        closes = [200.0 * math.exp(0.01 * ((-1) ** k)) for k in range(40)]                # ~1% a bar: far above a 5% daily target
        bars = collections.deque(Bar(ts=1_800_000_000.0 + 300 * k, open=c, high=c, low=c, close=c, volume=1e6, buy_volume=6e5,
                                     sell_volume=4e5, trades=1500, spread_bps=5.0) for k, c in enumerate(closes))   # deep: impact stays out of it
        wild = make_fake(cfg={"vol_target": 0.05, "confirm_bars": 1}, bars={"BTC-USD": bars})
        calm = make_fake(cfg={"vol_target": 0.0, "confirm_bars": 1}, bars={"BTC-USD": bars})
        rw = Engine._suggest(wild, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        rc = Engine._suggest(calm, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
        assert rw["action"] == rc["action"] == "BUY" and rw["sigma_daily"] > 0.05
        assert rw["vol_scale"] == 0.25 and rc["vol_scale"] == 1.0 and abs(rw["size"] - 0.25 * rc["size"]) < 1e-6
        assert "sized to 0.25x" in rw["why"]
