"""Order books: snapshots and deltas per venue, the bands and the aggregate ladder, the
features a bar carries, the decimated history, and each venue's depth message."""
import json
import time

from steel.book import BANDS_PCT, BookStore


def test_book_store_bands_ladder_features_and_history():
    st = BookStore(["BTC-USD"], mb=0.5, history_s=10.0, levels=3)
    now = time.time()
    # two venues: binance a snapshot, bybit a snapshot then a delta that removes a level and adds one
    st.apply("binance", "BTC-USD", [(100.0, 1.0), (99.5, 2.0), (91.0, 10.0), (80.0, 7.0)], [(100.2, 1.0), (101.0, 3.0), (109.0, 5.0), (125.0, 2.0)], True, now)
    st.apply("bybit", "BTC-USD", [(99.9, 1.0), (99.0, 1.0)], [(100.3, 2.0)], True, now)
    st.apply("bybit", "BTC-USD", [(99.0, 0.0), (99.8, 4.0)], [], False, now)
    assert not st.apply("binance", "ZZZ-USD", [(1, 1)], [(2, 1)], True, now)
    assert st.books[("bybit", "BTC-USD")].bids == {99.9: 1.0, 99.8: 4.0} and st.books[("bybit", "BTC-USD")].deltas == 1
    mid = st.mid("BTC-USD")
    assert abs(mid - 100.1) < 1e-9                                           # the median of the venues' mids (100.1 and 100.1)
    b = st.bands("BTC-USD")
    by = {x["pct"]: x for x in b["bands"]}
    assert by[0.1]["bid"] == 100.0 and by[0.1]["ask"] == 100.2 * 1 + 0          # within 0.1%: binance 100 bid; 100.2 ask (100.3 is 0.2% away)
    assert by[0.5]["bid"] == 100.0 + 99.9 + 99.8 * 4 and by[0.5]["ask"] == 100.2 + 100.3 * 2
    assert by[10.0]["bid"] == 100.0 + 99.5 * 2 + 99.9 + 99.8 * 4 + 91.0 * 10 and by[10.0]["ask"] == 100.2 + 101.0 * 3 + 100.3 * 2 + 109.0 * 5   # 80 and 125 sit outside 10%
    assert set(b["venues"]) == {"binance", "bybit"} and b["venues"]["bybit"]["levels"] == 3
    L = st.ladder("BTC-USD", n=3)
    assert [r["px"] for r in L["bids"]] == [100.0, 99.9, 99.8] and [r["px"] for r in L["asks"]] == [100.2, 100.3, 101.0]
    assert L["bids"][2]["cum"] == 100.0 + 99.9 + 99.8 * 4 and L["bids"][1]["by"] == {"bybit": 1.0}
    f = st.features("BTC-USD")
    assert set(f) == {"ob_imb_05", "ob_imb_2", "ob_imb_10", "ob_depth_1"} and -1 <= f["ob_imb_05"] <= 1 and f["ob_depth_1"] > 5
    st.mark("BTC-USD", now)
    assert len(st.imb["BTC-USD"]) == 1 and st.imb["BTC-USD"][0][1] == f["ob_imb_05"]
    # the history: a row per level per side per venue, at most `levels` each, decimated to history_s
    h = st.history("BTC-USD", since=0)
    assert len(h) == 6                                                       # the first apply recorded binance's top 3 each side; bybit's came within history_s
    st.apply("binance", "BTC-USD", [(100.0, 1.5)], [], False, now + 1)        # within history_s: no new row set
    st.apply("binance", "BTC-USD", [(100.0, 1.6)], [], False, now + 11)       # a new row set
    h2 = st.history("BTC-USD", since=0)
    assert len(h2) > len(h) and set(h2["side"]) == {1, -1} and st.status()["live"] == 2
    # a stale book drops out of the reads
    st.books[("bybit", "BTC-USD")].t = now - 1000
    assert set(st.bands("BTC-USD")["venues"]) == {"binance"}
    st.resize(["BTC-USD", "ETH-USD"])
    assert st.features("ETH-USD") is None and "ETH-USD" in st.hist


def test_each_venue_parses_its_depth_message():
    from tests.test_sources import cfg
    from steel.sources import BinanceSource, BybitSource, HyperliquidSource, KrakenSource, OKXSource
    got = []
    sink = lambda v, p, b, a, snap, ts, depth=None: got.append((v, p, b, a, snap, depth))
    hl = HyperliquidSource(cfg(), ["BTC-USD"]); hl._rev = {"BTC": "BTC-USD"}; hl.depth_sink = sink
    hl._handle({"channel": "l2Book", "data": {"coin": "BTC", "time": 1791676801000, "levels": [[{"px": "100", "sz": "2", "n": 3}], [{"px": "101", "sz": "1", "n": 1}]]}}, lambda t: None)
    bn = BinanceSource(cfg(), ["BTC-USD"]); bn._pairs = {"BTCUSDT": "BTC-USD"}; bn.depth_sink = sink
    bn._handle({"stream": "btcusdt@depth20@100ms", "data": {"lastUpdateId": 1, "bids": [["100.0", "2"]], "asks": [["101.0", "1"]]}}, lambda t: None)
    by = BybitSource(cfg(), ["BTC-USD"]); by._pairs = {"BTCUSDT": "BTC-USD"}; by.depth_sink = sink
    by._handle({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1791676801000, "data": {"s": "BTCUSDT", "b": [["100", "0"]], "a": [["101", "1"]]}}, lambda t: None)
    ok = OKXSource(cfg(), ["BTC-USD"]); ok._insts = {"BTC-USDT": "BTC-USD"}; ok.depth_sink = sink
    ok._handle({"arg": {"channel": "books", "instId": "BTC-USDT"}, "action": "snapshot", "data": [{"bids": [["100", "2", "0", "1"]], "asks": [["101", "1", "0", "1"]], "ts": "1791676801000"}]}, lambda t: None)
    kr = KrakenSource(cfg(), ["BTC-USD"]); kr._wsn = {"BTC/USD": "BTC-USD"}; kr.depth_sink = sink
    kr._handle({"channel": "book", "type": "update", "data": [{"symbol": "BTC/USD", "bids": [{"price": 100.0, "qty": 0.0}], "asks": [{"price": 101.0, "qty": 1.0}], "timestamp": "2026-10-11T00:00:01.000000Z"}]}, lambda t: None)
    assert [(g[0], g[4]) for g in got] == [("hyperliquid", True), ("binance", True), ("bybit", False), ("okx", True), ("kraken", False)]
    assert got[4][5] == 25 and all(g[5] is None for g in got[:4])                    # only Kraken asks for truncation
    assert got[0][2] == [(100.0, 2.0)] and got[0][3] == [(101.0, 1.0)] and got[2][2] == [(100.0, 0.0)] and got[4][2] == [(100.0, 0.0)]
    # the manager routes depth only for live, listed pairs, and never from the simulator
    from steel.sources.manager import SourceManager
    routed = []
    m = SourceManager(cfg(sources_on="coinbase,kraken"), ["BTC-USD"], on_tick=lambda t: None, on_depth=lambda *a: routed.append(a[0]))
    for src in m.sources.values():
        src.depth_sink = m._depth                                            # what start() wires before each venue runs
        if src.id in ("kraken", "sim"):
            src._depth("BTC-USD", [(1, 1)], [(2, 1)], True, None)
    assert routed == ["kraken"]


def test_a_fixed_depth_book_is_truncated_so_stale_levels_never_cross_it():
    from steel.book import Book
    b = Book()
    b.apply([(100.0, 1), (99.0, 1), (98.0, 1)], [(101.0, 1), (102.0, 1), (103.0, 1)], True, 0.0, depth=3)
    b.apply([(100.5, 1)], [], False, 1.0, depth=3)                              # a new best bid pushes 98 out: Kraken never says so
    assert sorted(b.bids) == [99.0, 100.0, 100.5]
    b.apply([], [(100.8, 1)], False, 2.0, depth=3)                              # the ask side likewise
    assert sorted(b.asks) == [100.8, 101.0, 102.0] and b.best() == (100.5, 100.8)
    b2 = Book(); b2.apply([(100.0, 1), (99.0, 1)], [(101.0, 1)], True, 0.0)      # no depth: nothing truncated (deltas carry deletes)
    b2.apply([(100.5, 1)], [], False, 1.0)
    assert sorted(b2.bids) == [99.0, 100.0, 100.5]


def test_heatmap_columns_average_the_samples_and_keep_sides_apart():
    st = BookStore(["BTC-USD"], mb=0.5, history_s=1.0, levels=5)
    base = (int(time.time() // 60) - 2) * 60.0
    st.apply("binance", "BTC-USD", [(100.0, 1.0), (99.0, 2.0)], [(101.0, 1.0)], True, base + 1, quote="USDT")
    st.apply("binance", "BTC-USD", [(100.0, 3.0), (99.0, 2.0)], [(101.0, 1.0)], True, base + 31, quote="USDT")     # a second sample in the same minute
    st.apply("kraken", "BTC-USD", [(100.0, 10.0)], [(101.0, 10.0)], True, base + 61, quote="USD")
    hm = st.heatmap("BTC-USD", 60.0, 5, bucket_bps=100.0, quote="USDT")
    assert hm["bucket"] == 1.0 and hm["venues"] == ["binance"] and len(hm["cols"]) == 2        # the kraken apply recorded binance's book again, a minute later
    cells = {(L, sd): u for L, sd, u in hm["cols"][0]["cells"]}
    assert cells[(100, 1)] == (100.0 * 1.0 + 100.0 * 3.0) / 2 and cells[(99, 1)] == 198.0 and cells[(101, -1)] == 101.0   # the mean over two samples
    usd = st.heatmap("BTC-USD", 60.0, 5, bucket_bps=100.0, quote="USD")
    assert usd["venues"] == ["kraken"] and usd["max"] == 1010.0
    assert st.heatmap("ETH-USD", 60.0, 5)["cols"] == []
