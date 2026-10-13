"""The scheduled events that move crypto: FOMC decisions, US CPI and the jobs report (the
published 2026 schedules), the monthly Deribit expiry (last Friday, 08:00 UTC) and the CME
quarterly futures expiry (last Friday of March, June, September, December, 15:00 UTC), plus
anything in `state/calendar.json` ([{"t": unix, "name": ..., "impact": "high"|"medium"}]).
`event_hours` is the feature: hours to the next high-impact event, capped at 72 and scaled to
[0, 1] (1 = 72 hours or more away, 0 = now)."""
from __future__ import annotations

import calendar as _cal
import datetime as _dt
import json
import time
from pathlib import Path

UTC = _dt.timezone.utc


def _at(y, m, d, hh, mm=0):
    return _dt.datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


def last_friday(y: int, m: int) -> int:
    last = _cal.monthrange(y, m)[1]
    d = _dt.date(y, m, last)
    while d.weekday() != 4:
        d -= _dt.timedelta(days=1)
    return d.day


# 2026 US schedules as published (FOMC decision days at 14:00 ET = 18:00 UTC in Oct/Nov/Dec and Jan/Feb; 18:00 UTC is used throughout:
# the half-hour of daylight-time drift is inside the event's own window). CPI and the jobs report at 08:30 ET = 12:30 UTC.
FOMC_2026 = [(1, 28), (3, 18), (4, 29), (6, 17), (7, 29), (9, 16), (10, 28), (12, 9)]
CPI_2026 = [(1, 13), (2, 13), (3, 11), (4, 10), (5, 12), (6, 10), (7, 14), (8, 12), (9, 11), (10, 14), (11, 10), (12, 10)]
NFP_2026 = [(1, 9), (2, 6), (3, 6), (4, 3), (5, 8), (6, 5), (7, 2), (8, 7), (9, 4), (10, 2), (11, 6), (12, 4)]


def builtin(year: int = 2026) -> list[dict]:
    ev = []
    if year == 2026:
        ev += [{"t": _at(2026, m, d, 18), "name": "FOMC decision", "impact": "high", "kind": "macro"} for m, d in FOMC_2026]
        ev += [{"t": _at(2026, m, d, 12, 30), "name": "US CPI", "impact": "high", "kind": "macro"} for m, d in CPI_2026]
        ev += [{"t": _at(2026, m, d, 12, 30), "name": "US jobs report", "impact": "high", "kind": "macro"} for m, d in NFP_2026]
    for m in range(1, 13):
        lf = last_friday(year, m)
        ev.append({"t": _at(year, m, lf, 8), "name": "Deribit monthly expiry", "impact": "medium" if m % 3 else "high", "kind": "options"})
        if m % 3 == 0:
            ev.append({"t": _at(year, m, lf, 15), "name": "CME quarterly futures expiry", "impact": "medium", "kind": "futures"})
    return ev


def load(state_dir: str | Path | None = None, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    year = _dt.datetime.fromtimestamp(now, tz=UTC).year
    ev = builtin(year) + builtin(year + 1)
    if state_dir:
        try:
            extra = json.loads((Path(state_dir) / "calendar.json").read_text())
            ev += [{"t": float(x["t"]), "name": str(x.get("name") or "event"), "impact": str(x.get("impact") or "medium"), "kind": str(x.get("kind") or "custom")} for x in extra]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return sorted(ev, key=lambda e: e["t"])


def upcoming(events: list[dict], now: float | None = None, n: int = 12, hours_back: float = 2.0) -> list[dict]:
    now = time.time() if now is None else now
    out = [dict(e, hours=round((e["t"] - now) / 3600.0, 2)) for e in events if e["t"] >= now - hours_back * 3600]
    return out[:n]


def event_hours(events: list[dict], now: float | None = None, cap_h: float = 72.0) -> float:
    """Hours to the next high-impact event, as a share of `cap_h` (1 = far away)."""
    now = time.time() if now is None else now
    nxt = [e["t"] for e in events if e["impact"] == "high" and e["t"] >= now]
    if not nxt:
        return 1.0
    return max(0.0, min(1.0, (min(nxt) - now) / 3600.0 / cap_h))
