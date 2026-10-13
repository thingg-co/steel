"""The bar clock must say so when quotes tick but no candle forms. The market never closes,
so a gap is always a stall."""
import types

from steel.engine import Engine


def make_fake(now, last_row_at, stalled=False):
    events = []
    fake = types.SimpleNamespace(
        _last_row_at=last_row_at, _stalled=stalled,
        cfg=types.SimpleNamespace(bar_seconds=300.0),
        sources=types.SimpleNamespace(status=lambda: [{"id": "coinbase", "enabled": True, "ticks": 10, "note": "ticker only"},
                                                      {"id": "kraken", "enabled": True, "ticks": 5, "note": "ws InvalidStatus"},
                                                      {"id": "coingecko", "enabled": True, "ticks": 0}]),
        trace=types.SimpleNamespace(emit=lambda ch, text, level="info", data=None: events.append((ch, level, text))),
        alerts=types.SimpleNamespace(send=lambda *a, **k: False),
        STALL_BARS=Engine.STALL_BARS,
    )
    return fake, events


def test_no_alarm_before_three_bars():
    fake, events = make_fake(now=1000.0, last_row_at=1000.0 - 800.0)
    Engine._check_stall(fake, 1000.0)
    assert events == [] and fake._stalled is False


def test_alarm_once_when_stale():
    fake, events = make_fake(now=1000.0, last_row_at=1000.0 - 1000.0)
    Engine._check_stall(fake, 1000.0)
    Engine._check_stall(fake, 1300.0)
    assert fake._stalled is True
    assert len(events) == 1
    ch, level, text = events[0]
    assert (ch, level) == ("system", "warn")
    assert "no bar for 17 minutes" in text and "coinbase (ticker only)" in text and "kraken (ws InvalidStatus)" in text
    assert "coingecko" not in text          # sources that never ticked are noise here


def test_weekend_is_not_a_holiday():
    """There is no closed session to excuse a gap: a Sunday-morning stall is still a stall."""
    fake, events = make_fake(now=1_791_270_000.0, last_row_at=1_791_270_000.0 - 1000.0)   # 2026-10-04 (a Sunday) UTC
    Engine._check_stall(fake, 1_791_270_000.0)
    assert fake._stalled is True and len(events) == 1
