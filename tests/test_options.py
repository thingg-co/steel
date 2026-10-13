"""Options: instrument names, Black-Scholes gamma, GEX by strike, the smile, max pain, zero gamma."""
import datetime as dt
import math

from steel.options import OptionsHub, analyze, bs_gamma, parse_name


def test_names_and_gamma():
    base, exp, strike, kind = parse_name("BTC-27DEC26-100000-C")
    assert base == "BTC" and strike == 100000.0 and kind == "C" and dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc).strftime("%Y-%m-%d %H") == "2026-12-27 08"
    assert parse_name("ETH-PERPETUAL") is None and parse_name("BTC-3JAN27-95d5-P")[2] == 95.5
    g = bs_gamma(100.0, 100.0, 30 / 365, 0.6)
    assert abs(g - math.exp(-0.5 * (0.5 * 0.6 * 0.6 * 30 / 365 / (0.6 * math.sqrt(30 / 365))) ** 2) / math.sqrt(2 * math.pi) / (100 * 0.6 * math.sqrt(30 / 365))) < 1e-12
    assert bs_gamma(100, 100, 0, 0.6) == 0.0 and bs_gamma(100, 100, 1, 0) == 0.0


def test_gex_smile_and_pain():
    now = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc).timestamp()
    rows = [{"instrument_name": "BTC-31OCT26-80000-C", "open_interest": 100, "mark_iv": 55, "underlying_price": 83000, "mark_price": 0.05, "volume": 1},
            {"instrument_name": "BTC-31OCT26-80000-P", "open_interest": 300, "mark_iv": 60, "underlying_price": 83000, "mark_price": 0.02, "volume": 1},
            {"instrument_name": "BTC-31OCT26-90000-C", "open_interest": 500, "mark_iv": 58, "underlying_price": 83000, "mark_price": 0.01, "volume": 1},
            {"instrument_name": "BTC-25DEC26-100000-C", "open_interest": 200, "mark_iv": 62, "underlying_price": 83000, "mark_price": 0.03, "volume": 1},
            {"instrument_name": "BTC-10OCT26-85000-C", "open_interest": 999, "mark_iv": 50, "underlying_price": 83000, "mark_price": 0.0, "volume": 0},   # expired: ignored
            {"instrument_name": "BTC-PERPETUAL", "open_interest": 1}]
    a = analyze(rows, now=now)
    assert a["spot"] == 83000 and a["n"] == 4
    by = {s["strike"]: s["gex"] for s in a["by_strike"]}
    assert by[90000.0] > 0 and by[80000.0] < 0                                     # calls positive, the put-heavy 80k negative
    assert by[100000.0] > 0 and abs(a["total"] - sum(by.values())) < 1e-6
    assert a["zero_gamma"] in (90000.0, 100000.0) and a["max_pain"] in (80000.0, 90000.0)
    assert [e["days"] > 0 for e in a["by_expiry"]] == [True, True] and a["by_expiry"][0]["calls"] == 600 and a["by_expiry"][0]["puts"] == 300
    sm = a["smile"][str(int(a["by_expiry"][0]["exp"]))]
    assert [x["strike"] for x in sm] == [80000.0, 90000.0] and sm[0]["iv"] == 0.575          # the 80k call and put IVs averaged
    assert a["term"][0]["atm_iv"] in (0.55, 0.60, 0.58) and a["walls"][0][1] in by
    h = OptionsHub(); h.latest["BTC"] = {**a, "asset": "BTC"}
    f = h.features("BTC")
    assert set(f) == {"gex_norm", "iv_atm"} and -1 <= f["gex_norm"] <= 1 and f["iv_atm"] == a["term"][0]["atm_iv"]
    assert analyze([], now=now)["by_strike"] == [] and h.features("ETH") is None
