"""Wall-clock arithmetic for a market that never closes.

Crypto trades around the clock, so there is no session to gate trading on. What remains is the
calendar: the UTC day is the unit of the daily loss limit, the background training budget and
the universe refresh; the week is the unit of the walk-forward backtest, which wants the
quietest stretch (early Saturday UTC, when volumes are lowest) but can start any time the
operator asks.
"""
from __future__ import annotations

import calendar as _calendar
import math
from datetime import datetime, timezone

UTC = timezone.utc
DAY = 86400.0

# The rhythm of a market that never closes, as model features: the hour of day and the day of
# the week as sine/cosine pairs (so 23:55 sits next to 00:00), how close the next perp funding
# settlement is (00, 08 and 16 UTC on the big venues; funding-time flows are real), whether the
# CME's bitcoin futures are trading (Sunday 22:00 to Friday 21:00 UTC with a daily hour's break
# at 21:00; approximate across the US daylight-saving shift) and the Friday 08:00 UTC options
# expiry on Deribit (the morning before it, with the monthly one marked stronger).
CALENDAR = ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "to_funding", "cme_open", "expiry"]
FUNDING_HOURS = (0, 8, 16)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_day(ts: float | None = None) -> str:
    """The UTC calendar date of a unix timestamp (now when None), as YYYY-MM-DD."""
    dt = datetime.fromtimestamp(float(ts), UTC) if ts is not None else utc_now()
    return dt.date().isoformat()


def day_start(ts: float) -> float:
    """The unix time at 00:00 UTC of the day holding `ts`."""
    return float(int(ts // DAY) * DAY)


def seconds_into_day(ts: float | None = None) -> float:
    ts = utc_now().timestamp() if ts is None else float(ts)
    return ts - day_start(ts)


def is_weekend(now: datetime | None = None) -> bool:
    """Saturday or Sunday UTC: still trading, just thinner. Nothing is gated on it."""
    now = now or utc_now()
    return now.astimezone(UTC).weekday() >= 5


def backtest_window(now: datetime | None = None, *, weekday: int = 5, hour: int = 0, length_hours: float = 24.0) -> bool:
    """Whether `now` falls inside the weekly window a scheduled backtest may start in: from
    `hour` UTC on `weekday` (0 = Monday) for `length_hours`. The run itself may outlast the
    window; this only decides when one may begin."""
    now = (now or utc_now()).astimezone(UTC)
    start_offset = ((now.weekday() - int(weekday)) % 7) * DAY + (now.hour - int(hour)) * 3600.0 + now.minute * 60.0 + now.second
    return 0.0 <= start_offset < float(length_hours) * 3600.0


def session_label(now: datetime | None = None) -> dict:
    """What the dashboard shows where an equity app shows the session: always open, with the UTC
    day and a note for the thin weekend tape."""
    now = (now or utc_now()).astimezone(UTC)
    return {"isOpen": True, "session": "24/7", "day": now.date().isoformat(),
            "weekend": now.weekday() >= 5, "t": now.timestamp()}


def _last_friday(year: int, month: int) -> int:
    last = _calendar.monthrange(year, month)[1]
    d = datetime(year, month, last, tzinfo=UTC)
    return last - ((d.weekday() - 4) % 7)


def cme_open(dt: datetime) -> bool:
    """Whether the CME's bitcoin futures trade at `dt` (UTC): Sunday 22:00 through Friday 21:00,
    closed one hour a day from 21:00 to 22:00."""
    dt = dt.astimezone(UTC)
    wd, h = dt.weekday(), dt.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if wd == 4:
        return h < 21
    return not (21 <= h < 22)


def calendar_features(ts: float) -> list[float]:
    """The CALENDAR feature values for a bar at unix time `ts`, every one in [-1, 1] or [0, 1],
    so they pass through standardization untouched like the exogenous ones."""
    dt = datetime.fromtimestamp(float(ts), UTC)
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    day_frac = secs / DAY
    week_frac = (dt.weekday() + day_frac) / 7.0
    hours_to = min(((h - dt.hour - dt.minute / 60.0 - dt.second / 3600.0) % 24.0) for h in FUNDING_HOURS)
    to_funding = 1.0 - min(hours_to, 8.0) / 8.0                     # 1 at the settlement, 0 eight hours before it
    expiry = 0.0
    if dt.weekday() == 4 and dt.hour < 8:
        expiry = 1.0 if dt.day == _last_friday(dt.year, dt.month) else 0.5
    return [math.sin(2 * math.pi * day_frac), math.cos(2 * math.pi * day_frac),
            math.sin(2 * math.pi * week_frac), math.cos(2 * math.pi * week_frac),
            to_funding, 1.0 if cme_open(dt) else 0.0, expiry]
