"""Liquidations and perp statistics: the stores' arithmetic, each venue's messages, the
manager's venue selection, and the two flow features."""
import time

from steel.derivs import LiqStore, PerpStats
from steel.sources.derivs import DERIVS, BinanceFuturesDerivs, BybitLinearDerivs, DerivManager, HyperliquidDerivs, OKXSwapDerivs


def test_liq_store_and_perp_stats():
    now = time.time()
    L = LiqStore(["BTC-USD"], mb=0.1)
    assert L.add("binance-perp", "BTC-USD", now - 100, 100.0, 5000.0, -1) and L.add("bybit-perp", "BTC-USD", now - 50, 101.0, 2000.0, 1)
    assert not L.add("binance-perp", "ETH-USD", now, 1, 1, -1) and not L.add("binance-perp", "BTC-USD", now, 100.0, 0.0, -1)
    r = L.recent("BTC-USD")
    assert [x["usd"] for x in r] == [2000.0, 5000.0] and r[1]["side"] == -1 and r[0]["venue"] == "bybit-perp"
    pb = L.per_bar("BTC-USD", 300.0, 3, now=now)
    assert pb["total_longs"] == 5000.0 and pb["total_shorts"] == 2000.0 and pb["n"] == 2
    end = int((now - 300) // 300) * 300 + 300                                  # the bar that closed before `now`
    L.add("okx-perp", "BTC-USD", end - 10, 99.0, 700.0, -1)
    assert L.last_bar("BTC-USD", 300.0, now=end + 1) == (700.0 + (5000.0 if end - 300 <= now - 100 < end else 0.0), 2000.0 if end - 300 <= now - 50 < end else 0.0)
    L.resize(["BTC-USD", "ETH-USD"])
    assert L.status()["events"] == 3 and L.recent("ETH-USD") == []
    P = PerpStats()
    P.apply("binance-perp", "BTC-USD", now, oi=100.0, mark=50.0, funding=0.0001)
    assert P.stats[("binance-perp", "BTC-USD")]["oi_usd"] == 5000.0                 # OI priced at the mark when no USD figure came
    P.apply("bybit-perp", "BTC-USD", now, oi_usd=15000.0, funding=-0.0002, mark=52.0)
    P.apply("okx-perp", "BTC-USD", now - 1000, oi_usd=1e9, funding=0.01, mark=40.0)     # stale: ignored
    a = P.aggregate("BTC-USD", now)
    assert a["oi_usd"] == 20000.0 and abs(a["funding"] - (5000 * 0.0001 + 15000 * -0.0002) / 20000) < 1e-12 and a["mark"] == 51.0 and a["n"] == 2
    P.mark("BTC-USD", now)
    assert len(P.history["BTC-USD"]) == 1 and P.history["BTC-USD"][0][1] == 20000.0
    assert P.aggregate("ETH-USD") is None and P.status()["live"] == 2


def test_each_venue_parses_liquidations_and_stats():
    from tests.test_sources import cfg
    liqs, stats = [], {}
    def liq_sink(v, p, ts, px, usd, side): liqs.append((v, p, round(px, 8), round(usd, 2), side))
    def stats_sink(v, p, ts, **f): stats.setdefault((v, p), {}).update({k: x for k, x in f.items() if x is not None})
    getter = lambda v, p: stats.get((v, p))
    bn = BinanceFuturesDerivs(cfg(), ["BTC-USD", "PEPE-USD"]); bn._pairs = {"BTCUSDT": "BTC-USD", "1000PEPEUSDT": "PEPE-USD"}
    bn.liq_sink, bn.stats_sink, bn.stats_getter = liq_sink, stats_sink, getter
    bn._handle({"data": {"e": "forceOrder", "o": {"s": "BTCUSDT", "S": "SELL", "p": "100", "ap": "99.5", "q": "2", "T": 1791676801000}}}, None)
    bn._handle({"data": {"e": "forceOrder", "o": {"s": "1000PEPEUSDT", "S": "BUY", "p": "0.01", "ap": "0.0100", "q": "1000000", "T": 1791676801000}}}, None)
    bn._handle({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "100.5", "i": "100.4", "r": "0.0001", "T": 1791705600000, "E": 1791676801000}}, None)
    stats[("binance-perp", "BTC-USD")]["oi"] = 10.0
    bn._handle({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "101", "i": "100.9", "r": "0.0001", "T": 1791705600000, "E": 1791676802000}}, None)
    assert liqs[0] == ("binance-perp", "BTC-USD", 99.5, 199.0, -1) and liqs[1] == ("binance-perp", "PEPE-USD", 0.00001, 10000.0, 1)
    assert stats[("binance-perp", "BTC-USD")]["mark"] == 101.0 and stats[("binance-perp", "BTC-USD")]["oi_usd"] == 1010.0 and stats[("binance-perp", "BTC-USD")]["funding"] == 0.0001
    by = BybitLinearDerivs(cfg(), ["BTC-USD"]); by._pairs = {"BTCUSDT": "BTC-USD"}; by.liq_sink, by.stats_sink = liq_sink, stats_sink
    by._handle({"topic": "allLiquidation.BTCUSDT", "data": [{"T": 1791676801000, "s": "BTCUSDT", "S": "Buy", "v": "3", "p": "100"}]}, None)
    by._handle({"topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 1791676801000, "data": {"symbol": "BTCUSDT", "openInterest": "500", "openInterestValue": "50000", "fundingRate": "-0.0003", "markPrice": "100.2", "indexPrice": "100.1", "nextFundingTime": "1791705600000"}}, None)
    assert liqs[2] == ("bybit-perp", "BTC-USD", 100.0, 300.0, -1)
    assert stats[("bybit-perp", "BTC-USD")] == {"oi": 500.0, "oi_usd": 50000.0, "funding": -0.0003, "mark": 100.2, "index": 100.1, "next_funding": 1791705600.0, "funding_hours": 8.0}
    ok = OKXSwapDerivs(cfg(), ["BTC-USD"]); ok._insts = {"BTC-USDT-SWAP": "BTC-USD"}; ok._ctval = {"BTC-USDT-SWAP": 0.01}
    ok.liq_sink, ok.stats_sink, ok.stats_getter = liq_sink, stats_sink, getter
    ok._handle({"arg": {"channel": "liquidation-orders", "instType": "SWAP"}, "data": [{"instId": "BTC-USDT-SWAP", "details": [{"side": "sell", "posSide": "long", "bkPx": "100", "sz": "50", "ts": "1791676801000"}]}]}, None)
    ok._handle({"arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"}, "data": [{"instId": "BTC-USDT-SWAP", "markPx": "100", "ts": "1791676801000"}]}, None)
    ok._handle({"arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"}, "data": [{"instId": "BTC-USDT-SWAP", "oi": "1000", "ts": "1791676801000"}]}, None)
    ok._handle({"arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}, "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0002", "nextFundingTime": "1791705600000"}]}, None)
    assert liqs[3] == ("okx-perp", "BTC-USD", 100.0, 50.0, -1)                   # 50 contracts x 0.01 BTC x 100
    assert stats[("okx-perp", "BTC-USD")]["oi"] == 10.0 and stats[("okx-perp", "BTC-USD")]["oi_usd"] == 1000.0 and stats[("okx-perp", "BTC-USD")]["funding"] == 0.0002
    # the manager picks the feeds by venue name, and the feeds never serve prices or count for the universe
    m = DerivManager(cfg(), ["BTC-USD"], liq_sink, stats_sink, getter)
    assert set(m.sources) == {c.id for c in DERIVS}
    only = DerivManager(type("C", (), {"derivs_venues": "bybit", "derivs": True})(), ["BTC-USD"], liq_sink, stats_sink, getter)
    assert set(only.sources) == {"bybit-perp"} and not only.sources["bybit-perp"].knows("BTC-USD") and only.sources["bybit-perp"].priority > 500


def test_flow_features_ride_in_every_bar():
    from steel.features import BOOK, FEATURES, FLOW, N_FEATURES
    assert FLOW == ["liq_long", "liq_short", "pos_long", "pos_short"] and FEATURES.index("liq_long") == FEATURES.index(BOOK[-1]) + 1 and N_FEATURES == 40


def test_hyperliquid_stats_from_meta_and_asset_ctxs():
    """One metaAndAssetCtxs answer: universe and contexts side by side; kPEPE unscaled; a
    delisted coin skipped; hourly funding; no liquidations ever."""
    from tests.test_sources import cfg
    stats = {}
    def stats_sink(v, p, ts, **f): stats.setdefault((v, p), {}).update({k: x for k, x in f.items() if x is not None})
    hl = HyperliquidDerivs(cfg(), ["BTC-USD", "PEPE-USD", "SOL-USD"])
    hl.stats_sink = stats_sink
    meta = {"universe": [{"name": "BTC"}, {"name": "kPEPE"}, {"name": "SOL", "isDelisted": True}, {"name": "ETH"}]}
    ctxs = [{"markPx": "100.5", "oraclePx": "100.4", "funding": "0.0000125", "openInterest": "10"},
            {"markPx": "10.0", "oraclePx": "9.9", "funding": "0.0001", "openInterest": "5000"},
            {"markPx": "1", "oraclePx": "1", "funding": "0", "openInterest": "1"},
            {"markPx": "2000", "oraclePx": "2000", "funding": "0", "openInterest": "1"}]
    assert hl.parse_ctxs(meta, ctxs) == 2
    b = stats[("hyperliquid-perp", "BTC-USD")]
    assert b["mark"] == 100.5 and b["index"] == 100.4 and b["funding"] == 0.0000125 and b["funding_hours"] == 1.0 and b["oi"] == 10.0 and b["oi_usd"] == 1005.0
    p = stats[("hyperliquid-perp", "PEPE-USD")]
    assert p["mark"] == 0.01 and p["oi"] == 5_000_000.0 and abs(p["oi_usd"] - 50000.0) < 1e-6
    assert ("hyperliquid-perp", "SOL-USD") not in stats and hl.liq_sink is None
    assert HyperliquidDerivs in DERIVS and "hyperliquid" in cfg().derivs_venues if hasattr(cfg(), "derivs_venues") else True
