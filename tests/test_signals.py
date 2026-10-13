"""Signal providers and the hub: parsing, bounded features, neutral when off. No network."""
import asyncio
import math
import types

from steel.config import Config
from steel.signals import DerivativesProvider, FearGreedProvider, SignalHub, SocialProvider, VolProvider, clip


def test_social_for_symbol():
    s = SocialProvider()
    s.last = {"BTC": {"mentions": 100, "mention_chg": 0.5, "attention": 1.0}, "ETH": {"mentions": 10, "mention_chg": -0.9, "attention": 0.5}}
    b = s.for_symbol("BTC-USD")
    assert b["social_attn"] == 1.0 and b["mentions"] == 100 and 0 < b["social_sent"] < 1
    e = s.for_symbol("ETH-USD")
    assert e["social_sent"] < 0 and e["social_attn"] == 0.5
    assert s.for_symbol("SOL-USD") == {"social_sent": 0.0, "social_attn": 0.0, "mentions": 0, "mention_chg": 0.0}


def test_derivatives_parse_and_rates(monkeypatch):
    d = DerivativesProvider(ratio_names=1)
    rows = [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "markPrice": "83010", "indexPrice": "83000", "openInterestValue": "4.6e9"},
            {"symbol": "ETHUSDT", "fundingRate": "-0.0005", "markPrice": "4000", "indexPrice": "4002", "openInterestValue": "1e9"},
            {"symbol": "1000PEPEUSDT", "fundingRate": "0.0002", "markPrice": "0.01", "indexPrice": "0.01", "openInterestValue": "5e7"}]

    class R:
        def __init__(self, payload, code=200):
            self.payload, self.status_code = payload, code
        def json(self):
            return self.payload
        def raise_for_status(self):
            pass

    class C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            if url.endswith("/tickers"):
                return R({"result": {"list": rows}})
            return R({"result": {"list": [{"buyRatio": "0.75", "sellRatio": "0.25"}]}})
        async def post(self, url, json=None):
            assert json == {"type": "metaAndAssetCtxs"}
            return R([{"universe": [{"name": "HYPE"}, {"name": "ETH"}, {"name": "kPEPE"}, {"name": "OLD", "isDelisted": True}]},
                      [{"funding": "0.0000125", "markPx": "40", "oraclePx": "39.96", "openInterest": "1000000"},
                       {"funding": "-0.0000625", "markPx": "4000", "oraclePx": "4001", "openInterest": "250000"},
                       {"funding": "0.000025", "markPx": "10", "oraclePx": "10", "openInterest": "1000"},
                       {"funding": "0.01", "markPx": "1", "oraclePx": "1", "openInterest": "1"}]])
    monkeypatch.setattr("steel.signals.httpx.AsyncClient", lambda *a, **k: C())
    out = asyncio.run(d.gather(["BTC-USD", "ETH-USD", "PEPE-USD", "SOL-USD", "HYPE-USD", "OLD-USD"]))
    assert set(out) == {"BTC-USD", "ETH-USD", "PEPE-USD", "HYPE-USD"}      # SOL is on neither venue; PEPE maps to both 1000x contracts; OLD is delisted
    assert out["BTC-USD"]["funding"] == 0.0001 and out["BTC-USD"]["funding_sig"] > 0 and out["ETH-USD"]["funding_sig"] < 0
    assert abs(out["BTC-USD"]["basis"] - (83010 / 83000 - 1)) < 1e-9 and out["ETH-USD"]["basis_sig"] < 0
    assert out["HYPE-USD"]["venues"] == 1 and abs(out["HYPE-USD"]["funding"] - 0.0001) < 1e-12     # hourly 0.00125% x 8 = Bybit's 8-hour unit
    assert abs(out["HYPE-USD"]["basis"] - (40 / 39.96 - 1)) < 1e-9 and out["HYPE-USD"]["oi"] == 40_000_000.0 and out["HYPE-USD"]["longshort"] == 1.0
    assert out["ETH-USD"]["venues"] == 2 and abs(out["ETH-USD"]["funding"] - (-0.0005 + -0.0005) / 2) < 1e-12   # both: the mean rate
    assert abs(out["ETH-USD"]["basis"] - ((4000 / 4002 - 1) + (4000 / 4001 - 1)) / 2) < 1e-9 and out["ETH-USD"]["oi"] == 1e9 + 1e9
    assert abs(out["PEPE-USD"]["funding"] - (0.0002 + 0.0002) / 2) < 1e-12 and "Hyperliquid" in d.status
    assert out["BTC-USD"]["longshort"] == 3.0 and out["BTC-USD"]["longshort_sig"] > 0    # only the first name gets the ratio call
    assert out["ETH-USD"]["longshort"] == 1.0 and out["ETH-USD"]["longshort_sig"] == 0.0
    assert out["BTC-USD"]["oi_chg"] == 0.0                                  # first sight: no change yet
    assert d.rates() == {"BTC-USD": 0.0001, "ETH-USD": -0.0005, "PEPE-USD": 0.0002, "HYPE-USD": 0.0001}
    rows[0]["openInterestValue"] = "5.06e9"
    out = asyncio.run(d.gather(["BTC-USD"]))
    assert abs(out["BTC-USD"]["oi_chg"] - 0.1) < 1e-9 and 0 < out["BTC-USD"]["oi_chg_sig"] < 1


def test_vol_sig_and_lookup():
    v = VolProvider()
    v.dvol = {"BTC": 37.0, "ETH": 60.0}
    assert v.for_symbol("BTC-USD") == 37.0 and v.for_symbol("ETH-USD") == 60.0 and v.for_symbol("SOL-USD") == 37.0
    assert v.for_symbol("STETH-USD") == 60.0
    assert VolProvider.sig(55.0) == 0.0 and VolProvider.sig(90.0) == 1.0 and VolProvider.sig(20.0) == -1.0 and VolProvider.sig(None) == 0.0
    assert VolProvider.sig(37.0) < 0


def test_fear_greed_norm():
    assert clip((75 - 50) / 50.0) == 0.5 and clip((0 - 50) / 50.0) == -1.0
    f = FearGreedProvider()
    assert f.value == 50 and f.id == "feargreed"


def test_hub_neutral_when_everything_is_off():
    cfg = Config()
    cfg.signals_off = "social,feargreed,derivatives,vol,market"
    cfg.symbols = ["BTC-USD", "ETH-USD"]
    hub = SignalHub(cfg, cfg.symbols)
    assert not any(hub.enabled.values())
    st = asyncio.run(hub.gather())
    for sym in cfg.symbols:
        f = st["per_asset"][sym]["feat"]
        assert set(f) == {"social_sent", "social_attn", "fng", "funding", "oi_chg", "longshort", "basis", "dvol", "btc_dom", "breadth", "stable_flow"}
        assert all(v == 0.0 for v in f.values())
        assert st["crowding"][sym] == 0.0
    assert st["funding"] == {} and st["dvol"] == {}
    assert {p["id"] for p in hub.status()} == {"social", "feargreed", "derivatives", "vol", "market"}
    assert hub.toggle("vol", True) == {"ok": True} and hub.toggle("nope", True)["error"]


def test_hub_features_match_the_feature_builder_slots():
    from steel.features import EXOGENOUS
    cfg = Config()
    cfg.signals_off = "social,feargreed,derivatives,vol,market"
    cfg.symbols = ["BTC-USD"]
    st = asyncio.run(SignalHub(cfg, cfg.symbols).gather())
    assert set(st["per_asset"]["BTC-USD"]["feat"]) == set(EXOGENOUS[2:])   # every exogenous slot after news is fed


def test_crowding_is_bounded_and_leans_with_euphoria():
    cfg = Config()
    cfg.signals_off = "social,feargreed,market,vol"
    cfg.symbols = ["BTC-USD"]
    hub = SignalHub(cfg, cfg.symbols)
    d = hub.providers["derivatives"]
    d.last = {"BTC-USD": {"funding": 0.003, "funding_sig": 1.0, "oi": 1.0, "oi_chg": 0.5, "oi_chg_sig": 0.99, "basis": 0.01, "basis_sig": 1.0,
                          "longshort": 4.0, "longshort_sig": 0.88}}

    async def no_gather(symbols, say=None):
        return d.last
    d.gather = no_gather
    st = asyncio.run(hub.gather())
    assert 0.5 < st["crowding"]["BTC-USD"] <= 1.0
    assert st["funding"] == {"BTC-USD": 0.003}
