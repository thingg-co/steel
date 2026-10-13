"""The go-live warmup: a backfill measured in days, a warmup measured in passes over it with the
held-out guard and a time cap, and the plan the dashboard shows."""
import asyncio
import types

from steel.config import Config
from steel.engine import Engine


def test_backfill_days_set_the_seconds_unless_seconds_are_given():
    assert Config(backfill_days=30.0, backfill_seconds=0.0).backfill_seconds == 30 * 86400.0
    assert Config(backfill_days=7.0, backfill_seconds=0.0).backfill_seconds == 7 * 86400.0
    assert Config(backfill_days=7.0, backfill_seconds=7200.0).backfill_seconds == 7200.0       # seconds win when set


def fake_engine(n_labeled=8000, epochs=1.0, steps=60, batch=16, minutes=30.0, patience=3, holdouts=None):
    events = []
    calls = {"train": 0, "validate": 0}
    hold = list(holdouts or [])

    def train_steps(n):
        calls["train"] += n
        return {"loss": 1.0, "pinball": 5.0, "bce": 0.6, "steps": calls["train"]}

    def validate():
        calls["validate"] += 1
        return {"pinball": hold.pop(0) if hold else 4.0 - 0.01 * calls["validate"], "bce": 0.5, "n": 10}   # improving unless a script says otherwise
    cfg = types.SimpleNamespace(warmup_epochs=epochs, warmup_steps=steps, batch_size=batch, max_warmup_minutes=minutes, overfit_patience=patience)
    fake = types.SimpleNamespace(cfg=cfg, learner=types.SimpleNamespace(n_labeled=n_labeled, train_steps=train_steps, validate=validate, release_cache=lambda: None),
                                 metrics=types.SimpleNamespace(holdout_pinball=None, holdout_bce=None, as_dict=lambda: {}),
                                 trace=types.SimpleNamespace(emit=lambda ch, text, level="info", data=None: events.append(text)),
                                 _publish=lambda m: None, _drain_history=lambda: {}, status="backfilling",
                                 status_dict=lambda: {"phase": fake.status, "warmup": fake.warmup}, warmup=None)
    fake.warmup_plan = lambda: Engine.warmup_plan(fake)
    fake._absorb_train = lambda res: None
    return fake, events, calls


def test_warmup_runs_passes_over_the_history_not_a_fixed_step_count():
    fake, events, calls = fake_engine(n_labeled=8000, epochs=1.0, batch=16)
    assert fake.warmup_plan() == 500                                         # one pass: 8,000 labeled bars / 16
    asyncio.run(Engine._warmup(fake))
    assert calls["train"] == 500 and fake.warmup["done"] == 500 and fake.warmup["total"] == 500 and fake.warmup["stopped"] == ""
    assert fake.status == "training" and calls["validate"] >= 12            # a held-out check every 40 steps
    small, _, calls = fake_engine(n_labeled=200, epochs=1.0, steps=60)
    assert small.warmup_plan() == 60                                         # never fewer than warmup_steps
    off, _, _ = fake_engine(n_labeled=8000, epochs=0.0, steps=60)
    assert off.warmup_plan() == 60                                           # epochs off: the old fixed count


def test_warmup_stops_when_the_held_out_loss_stops_improving():
    fake, events, calls = fake_engine(n_labeled=8000, epochs=2.0, patience=3, holdouts=[5.0, 4.0, 4.1, 4.2, 4.3, 3.0, 2.0])
    asyncio.run(Engine._warmup(fake))
    assert fake.warmup["stopped"] == "held-out loss stopped improving"
    assert fake.warmup["done"] == 200 and calls["train"] == 200             # best at the 2nd check, three checks without improvement: 5 checks = 200 steps
    assert any("the history is learned" in e for e in events)


def test_warmup_respects_the_time_cap(monkeypatch):
    import steel.engine as eng
    clock = {"t": 1000.0}
    monkeypatch.setattr(eng.time, "time", lambda: clock["t"])
    fake, events, calls = fake_engine(n_labeled=80000, epochs=1.0, minutes=1.0)

    def slow(n):
        clock["t"] += 10.0                                                   # ten seconds a chunk
        calls["train"] += n
        return {"loss": 1.0, "pinball": 5.0, "bce": 0.6, "steps": calls["train"]}
    fake.learner.train_steps = slow
    asyncio.run(Engine._warmup(fake))
    assert fake.warmup["stopped"] == "1-minute cap" and 100 <= fake.warmup["done"] <= 140 and fake.warmup["total"] == 5000


def test_tiny_history_skips_warmup():
    fake, events, calls = fake_engine(n_labeled=10)
    asyncio.run(Engine._warmup(fake))
    assert calls["train"] == 0 and fake.warmup is None and any("skipping warmup" in e for e in events)
