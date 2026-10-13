"""Trust before the first live label: the held-out scorecard over recent history, the trust
rule it feeds, the per-name skill gate it seeds, and the hand-over to live labels."""
import types

import numpy as np

from steel.engine import Engine
from test_gates import make_fake, make_pred
from tests.test_replay_dedupe import make_learner, window


def test_holdout_scorecard_scores_only_held_out_complete_windows():
    L = make_learner(cap=512)
    for i in range(300):
        L.add(*window(L, i), ts=1_700_000_000.0 + 300 * i)
    card = L.holdout_scorecard()
    held = np.flatnonzero(L.store.hold[:L.size] & L.store.lab[:L.size])
    complete = held[L.store.chains(held)[1]]
    assert card["n"] == len(complete) == 15 and card["t0"] < card["t1"]
    assert 0 <= card["calls"] <= card["n"] * 2 and (card["hit"] is None or 0.0 <= card["hit"] <= 1.0)
    assert card["pinball"] > 0 and 0.0 <= card["coverage"] <= 1.0
    assert set(card["per_name"]) == {0, 1} and all(len(v) <= 40 for v in card["per_name"].values())
    recent = L.holdout_scorecard(recent_bars=200)                          # only the last 200 bars' held-out windows
    assert 8 <= recent["n"] <= 11 and recent["t0"] >= card["t1"] - 200 * 300
    assert L.holdout_scorecard(recent_bars=40) is None                      # fewer than 8: nothing to say


def fake(live_labels=0, holdout=None, calls=0, hit_ema=None, min_labels=48, min_hit_rate=0.5):
    m = types.SimpleNamespace(live_labels=live_labels, holdout_hit=holdout, holdout_calls=calls, holdout_coverage=None,
                              holdout_pinball=None, hit_ema=hit_ema, trusted=False, trusted_by="")
    cfg = types.SimpleNamespace(min_labels=min_labels, min_hit_rate=min_hit_rate, trust_days=7.0, bars_per_day=288)
    f = types.SimpleNamespace(metrics=m, cfg=cfg, symbols=["A", "B"], holdout_outcomes={})
    f._update_trust = lambda: Engine._update_trust(f)
    f.rescore_holdout = lambda: Engine.rescore_holdout(f)
    return f


def test_trust_rule_live_or_held_out():
    f = fake(); f._update_trust()
    assert (f.metrics.trusted, f.metrics.trusted_by) == (False, "")
    f = fake(holdout=0.56, calls=120); f._update_trust()
    assert (f.metrics.trusted, f.metrics.trusted_by) == (True, "held-out")
    f = fake(holdout=0.56, calls=30); f._update_trust()                     # too few held-out calls
    assert not f.metrics.trusted
    f = fake(holdout=0.44, calls=300); f._update_trust()                    # enough calls, no skill
    assert not f.metrics.trusted
    f = fake(live_labels=48, holdout=0.44, calls=300); f._update_trust()    # live labels take over, whatever history said
    assert (f.metrics.trusted, f.metrics.trusted_by) == (True, "live")


def test_rescore_applies_the_card_and_seeds_the_hit_ema_once():
    f = fake()
    card = {"n": 40, "calls": 60, "hit": 0.6, "pinball": 3.3, "coverage": 0.81, "t0": 1.0, "t1": 2.0, "per_name": {0: [True, False, True], 1: [False]}}
    f.learner = types.SimpleNamespace(holdout_scorecard=lambda recent_bars: card)
    assert f.rescore_holdout() is card
    m = f.metrics
    assert (m.holdout_hit, m.holdout_calls, m.holdout_coverage, m.holdout_pinball) == (0.6, 60, 0.81, 3.3)
    assert f.holdout_outcomes == {"A": [True, False, True], "B": [False]}
    assert m.hit_ema == 0.6 and m.trusted and m.trusted_by == "held-out"
    m.hit_ema = 0.52                                                        # live labels have moved it: the card no longer touches it
    f.rescore_holdout()
    assert m.hit_ema == 0.52
    f.learner = types.SimpleNamespace(holdout_scorecard=lambda recent_bars: None)
    assert f.rescore_holdout() is None and m.trusted                        # nothing to score: the state stands


def test_skill_gate_takes_the_held_out_record_until_live_calls_fill_it():
    pred = make_pred([-10, 0, 40, 70, 100], 0.7, 0.2)
    nothing = make_fake(outcomes={"BTC-USD": []}, cfg={"confirm_bars": 1})
    r = Engine._suggest(nothing, 0, "BTC-USD", pred)
    assert r["action"] == "HOLD" and "no track record" in r["why"]
    seeded = make_fake(outcomes={"BTC-USD": []}, holdout_outcomes={"BTC-USD": [True] * 7 + [False] * 3}, cfg={"confirm_bars": 1})
    assert Engine._suggest(seeded, 0, "BTC-USD", pred)["action"] == "BUY"   # ten held-out calls at 70%: enough
    poor = make_fake(outcomes={"BTC-USD": []}, holdout_outcomes={"BTC-USD": [True] * 3 + [False] * 7}, cfg={"confirm_bars": 1})
    r = Engine._suggest(poor, 0, "BTC-USD", pred)
    assert r["action"] == "HOLD" and "held-out history" in r["why"]
    live = make_fake(outcomes={"BTC-USD": [{"hit": True}] * 40}, holdout_outcomes={"BTC-USD": [False] * 40}, cfg={"confirm_bars": 1})
    assert Engine._suggest(live, 0, "BTC-USD", pred)["action"] == "BUY"     # a full live record: history no longer counts
