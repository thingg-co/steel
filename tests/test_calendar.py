"""Crypto's calendar as features: the hour and the day of the week as angles, the funding
clock, the CME session and the options expiry; and a feature-set change that keeps the replay."""
from datetime import datetime, timezone

import numpy as np
import pytest
import torch

from steel.clock import CALENDAR, calendar_features, cme_open
from steel.features import FEATURES, N_FEATURES, NORMALIZE
from steel.learner import OnlineLearner, read_replay
from tests.test_universe import fill_windows, make

UTC = timezone.utc


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


def test_calendar_features_are_bounded_and_mean_what_they_say():
    assert FEATURES[-len(CALENDAR):] == list(CALENDAR) and N_FEATURES == 9 + 13 + 7 and not NORMALIZE[-len(CALENDAR):].any()
    v = dict(zip(CALENDAR, calendar_features(at(2026, 10, 11, 0, 0))))        # a Sunday midnight UTC
    assert v["hour_sin"] == pytest.approx(0.0) and v["hour_cos"] == pytest.approx(1.0)
    assert v["to_funding"] == pytest.approx(1.0) and v["cme_open"] == 0.0 and v["expiry"] == 0.0
    v = dict(zip(CALENDAR, calendar_features(at(2026, 10, 11, 23, 0))))       # Sunday 23:00: the CME is back, funding an hour away
    assert v["cme_open"] == 1.0 and v["to_funding"] == pytest.approx(1 - 1 / 8)
    v = dict(zip(CALENDAR, calendar_features(at(2026, 10, 14, 12, 0))))       # Wednesday noon: 4 h to the 16:00 settlement
    assert v["to_funding"] == pytest.approx(0.5) and v["hour_cos"] == pytest.approx(-1.0) and v["cme_open"] == 1.0
    v = dict(zip(CALENDAR, calendar_features(at(2026, 10, 14, 21, 30))))      # the CME's daily hour off
    assert v["cme_open"] == 0.0
    weekly = dict(zip(CALENDAR, calendar_features(at(2026, 10, 9, 6, 0))))     # a Friday morning before the 08:00 expiry
    monthly = dict(zip(CALENDAR, calendar_features(at(2026, 10, 30, 6, 0))))   # the month's last Friday
    after = dict(zip(CALENDAR, calendar_features(at(2026, 10, 30, 9, 0))))
    assert weekly["expiry"] == 0.5 and monthly["expiry"] == 1.0 and after["expiry"] == 0.0
    assert all(-1.0 <= x <= 1.0 for x in calendar_features(at(2026, 3, 1, 13, 37)))
    assert not cme_open(datetime(2026, 10, 10, 12, tzinfo=UTC)) and cme_open(datetime(2026, 10, 12, 3, tzinfo=UTC)) and not cme_open(datetime(2026, 10, 16, 21, 30, tzinfo=UTC))
    # the week angle is continuous across midnight Sunday
    a = calendar_features(at(2026, 10, 11, 23, 59)); b = calendar_features(at(2026, 10, 12, 0, 0))
    assert abs(a[2] - b[2]) < 0.01 and abs(a[3] - b[3]) < 0.01


def test_a_feature_set_change_keeps_the_replay_and_recomputes_the_calendar(tmp_path):
    A = make(["AAA", "BBB"])
    A.features = ["ret", "range", "logvol"]
    fill_windows(A, 20)
    A.train_steps(2)
    A.save(tmp_path, force_replay=True, extra={"norm": {"k": 1}})
    assert read_replay(tmp_path)["features"] == ["ret", "range", "logvol"]
    cfg = A.cfg
    B = OnlineLearner(cfg, n_features=5, features=["ret", "hour_sin", "range", "logvol", "cme_open"])   # two new columns, one of them calendar
    extra = B.load(tmp_path)
    assert extra == {"norm": {"k": 1}} and B.steps == 0 and B.size == A.size and B.n_labeled == 20     # fresh net, the whole replay
    assert (tmp_path / "model.pt.bak").exists() and (tmp_path / "replay").exists()
    s = B.store.slot[1_700_000_000 + 300 * 5]
    old = A.store.x[A.store.slot[1_700_000_000 + 300 * 5]]
    assert np.allclose(B.store.x[s][:, [0, 2, 3]], old) and B.store.has[s].all()
    cal = calendar_features(1_700_000_000.0 + 300 * 5)
    assert np.allclose(B.store.x[s][:, 1], cal[0]) and np.allclose(B.store.x[s][:, 4], cal[5])
    B.train_steps(2)
    B.add(np.zeros((2, 8, 5), dtype=np.float32), np.zeros(2, dtype=np.float32), ts=1_700_000_000.0 + 300 * 20)
    B.save(tmp_path, force_replay=True)                                                                  # the replay on disk now carries B's five names
    assert read_replay(tmp_path)["features"] == B.features
    D = OnlineLearner(cfg, n_features=4, features=["ret", "range", "logvol", "nope"])
    assert D.load(tmp_path) is None and D.size == 0                                                      # not a superset of the saved set: the old rule, backup and fresh
    assert (tmp_path / "replay.bak").exists()
