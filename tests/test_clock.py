"""The 24/7 calendar: UTC days, the weekly backtest window, the session label."""
from datetime import datetime, timezone

from steel import clock


def test_utc_day_and_day_start():
    ts = datetime(2026, 10, 11, 23, 59, 30, tzinfo=timezone.utc).timestamp()
    assert clock.utc_day(ts) == "2026-10-11"
    assert clock.utc_day(ts + 60) == "2026-10-12"                           # the day turns at 00:00 UTC, not in any local zone
    assert clock.day_start(ts) == datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()
    assert abs(clock.seconds_into_day(ts) - (23 * 3600 + 59 * 60 + 30)) < 1e-6


def test_weekend_is_informational_only():
    assert clock.is_weekend(datetime(2026, 10, 10, 12, tzinfo=timezone.utc))      # Saturday
    assert not clock.is_weekend(datetime(2026, 10, 9, 12, tzinfo=timezone.utc))   # Friday
    lab = clock.session_label(datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    assert lab["isOpen"] is True and lab["session"] == "24/7" and lab["day"] == "2026-10-10" and lab["weekend"] is True


def test_backtest_window():
    sat = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)
    assert clock.backtest_window(sat, weekday=5, hour=0)
    assert clock.backtest_window(datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc), weekday=5, hour=0)
    assert not clock.backtest_window(datetime(2026, 10, 11, 0, 30, tzinfo=timezone.utc), weekday=5, hour=0)   # Sunday: closed
    assert not clock.backtest_window(datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc), weekday=5, hour=0)   # a minute early
    assert clock.backtest_window(datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc), weekday=2, hour=6, length_hours=2)
    assert not clock.backtest_window(datetime(2026, 10, 7, 8, 1, tzinfo=timezone.utc), weekday=2, hour=6, length_hours=2)
