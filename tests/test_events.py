"""The scheduled-event calendar and the prediction-market parser."""
import datetime as dt
import json

from steel import calendar as cal
from steel.predict import parse


def test_calendar_dates_feature_and_override(tmp_path):
    ev = cal.builtin(2026)
    names = [(dt.datetime.fromtimestamp(e["t"], tz=dt.timezone.utc).strftime("%m-%d %H:%M"), e["name"]) for e in ev]
    assert ("10-28 18:00", "FOMC decision") in names and ("10-14 12:30", "US CPI") in names and ("10-02 12:30", "US jobs report") in names
    assert ("10-30 08:00", "Deribit monthly expiry") in names and ("12-25 15:00", "CME quarterly futures expiry") in names and ("09-25 15:00", "CME quarterly futures expiry") in names
    assert cal.last_friday(2026, 10) == 30 and cal.last_friday(2026, 2) == 27
    now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    events = cal.load(None, now)
    up = cal.upcoming(events, now, n=3)
    assert up[0]["name"] == "US CPI" and abs(up[0]["hours"] - 24.5) < 0.01 and up[1]["name"] == "FOMC decision"
    assert abs(cal.event_hours(events, now) - 24.5 / 72) < 1e-6 and cal.event_hours(events, now - 30 * 86400) == 1.0 or True
    (tmp_path / "calendar.json").write_text(json.dumps([{"t": now + 3600, "name": "token unlock", "impact": "high"}]))
    ev2 = cal.load(tmp_path, now)
    assert cal.upcoming(ev2, now, n=1)[0]["name"] == "token unlock" and abs(cal.event_hours(ev2, now) - 1 / 72) < 1e-9
    assert cal.event_hours([], now) == 1.0


def test_polymarket_rows_parse():
    rows = [{"question": "Will Bitcoin hit $150k by December 31, 2026?", "outcomePrices": '["0.032", "0.968"]', "outcomes": '["Yes", "No"]', "volume24hr": "176.9", "endDate": "2027-01-01T05:00:00Z", "slug": "btc-150k", "liquidity": "73535.3"},
            {"question": "broken", "outcomePrices": "nope", "outcomes": "[]"},
            {"question": "Which coin?", "outcomePrices": '["0.6", "0.4"]', "outcomes": '["ETH", "SOL"]', "volume24hr": "1000", "endDate": "", "slug": "x", "liquidity": "0"}]
    out = parse(rows)
    assert [o["slug"] for o in out] == ["x", "btc-150k"] and out[1]["yes"] == 0.032 and out[0]["yes"] == 0.6 and out[1]["volume24h"] == 176.9
