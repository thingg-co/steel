"""The GPU handoff: the schedule, the decision, and what each mode does to the config."""
from datetime import datetime
from types import SimpleNamespace

from steel import handoff


def test_schedule_parsing_and_next_start():
    assert handoff.parse_schedule("Mon..Fri 19:45") == ({0, 1, 2, 3, 4}, 19, 45)
    assert handoff.parse_schedule("Sat,Sun 03:00") == ({5, 6}, 3, 0)
    assert handoff.parse_schedule("daily 02:30") == (set(range(7)), 2, 30)
    assert handoff.parse_schedule("Fri..Mon 10:00") == ({4, 5, 6, 0}, 10, 0)
    assert handoff.parse_schedule("") is None and handoff.parse_schedule("Mon 25:00") is None and handoff.parse_schedule("Funday 10:00") is None
    sat = datetime(2026, 10, 10, 12, 0)                                     # a Saturday
    assert handoff.next_start("Mon..Fri 19:45", sat) == datetime(2026, 10, 12, 19, 45)
    fri_evening = datetime(2026, 10, 9, 19, 50)
    assert handoff.next_start("Mon..Fri 19:45", fri_evening) == datetime(2026, 10, 12, 19, 45)
    assert handoff.next_start("Mon..Fri 19:45", datetime(2026, 10, 9, 19, 45)) == datetime(2026, 10, 9, 19, 45)   # now counts
    assert handoff.next_start("", sat) is None


def test_decision_rules():
    nxt = datetime(2026, 10, 12, 19, 45)
    assert handoff.decide(True, nxt, 15)[0] == "shared"
    assert handoff.decide(False, nxt, 15, now=datetime(2026, 10, 12, 19, 35)) == ("shared", "the peer starts at Mon 19:45 (releasing the GPU)")
    assert handoff.decide(False, nxt, 15, now=datetime(2026, 10, 12, 19, 20))[0] == "solo"
    assert handoff.decide(False, None, 15) == ("solo", "no peer")
    assert handoff.decide(False, nxt, 15, pinned="shared") == ("shared", "pinned by the operator")
    assert handoff.decide(True, nxt, 15, pinned="solo")[0] == "solo"


def test_modes_scale_the_budget_and_floor(monkeypatch):
    cfg = SimpleNamespace(handoff=True, handoff_peer="flint-run", handoff_peer_process="python -m flint", handoff_peer_port=0,
                          handoff_schedule="Mon..Fri 19:45", handoff_lead_minutes=15, solo_train_multiplier=3.0, solo_memory_floor_gb=2.0,
                          train_epochs=1.0, memory_floor_gb=12.0)
    h = handoff.Handoff(cfg)
    up = {"v": False}
    monkeypatch.setattr(handoff, "peer_running", lambda *a, **k: (up["v"], "flint-run.service is active" if up["v"] else ""))
    mode, why, changed = h.evaluate(datetime(2026, 10, 10, 12, 0))           # Saturday noon: solo
    assert (mode, changed) == ("solo", True) and cfg.train_epochs == 3.0 and cfg.memory_floor_gb == 2.0 and not h.released
    assert h.evaluate(datetime(2026, 10, 10, 13, 0))[2] is False               # same mode: nothing to apply
    mode, why, changed = h.evaluate(datetime(2026, 10, 12, 19, 35))          # Monday, ten minutes before Flint: shared, released
    assert (mode, changed, h.released) == ("shared", True, True) and cfg.train_epochs == 1.0 and cfg.memory_floor_gb == 12.0
    up["v"] = True
    h.evaluate(datetime(2026, 10, 12, 20, 0))                                 # Flint running: shared, nothing more to release
    assert h.mode == "shared" and not h.released and h.peer == "flint-run.service is active"
    st = h.status()
    assert st["mode"] == "shared" and st["next_start"] == "2026-10-13T19:45" and st["train_epochs"] == 1.0
    # the operator raises the base budget while solo: the multiplier applies to the new base
    up["v"] = False
    h.evaluate(datetime(2026, 10, 10, 12, 0))
    cfg.train_epochs = 6.0
    h.rebase()
    assert h.base_epochs == 2.0
    h.pinned = "shared"
    assert h.evaluate(datetime(2026, 10, 10, 12, 0))[0] == "shared" and cfg.train_epochs == 2.0
