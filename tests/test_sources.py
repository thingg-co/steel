"""Crypto market-data sources: symbol mapping, candle parsing per venue, websocket message
handling, and the manager's routing and failover. No network."""
import asyncio
import time
import types

import pytest

from steel.config import Config, canonical
from steel.feed import Tick
from steel.sources import (BinanceSource, BybitSource, CoinbaseSource, CoinGeckoSource, KrakenSource, OKXSource,
                           SimSource, SourceManager, asset_class, base_of)


def cfg(**kw):
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD", "DOGE-USD"]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def collect():
    out = []
    return out, out.append


def test_canonical_and_classes():
    assert canonical("btc") == "BTC-USD" and canonical("ETH/USD") == "ETH-USD" and canonical("SOLUSDT") == "SOL-USD"
    assert canonical("XRP-USDC") == "XRP-USD" and canonical("DOGE-USD") == "DOGE-USD" and canonical("") == ""
    assert canonical("BTC-EUR") == "BTC-EUR"                      # another quote is left alone, not pretended to be dollars
    assert asset_class("BTC-USD") == "crypto" and asset_class("USDT-USD") == "stable" and asset_class("USDC-USD") == "stable"
    assert asset_class("EUR-USD") == "fiat" and asset_class("GBP-USD") == "fiat" and asset_class("USDG-USD") == "stable" and asset_class("PAXG-USD") == "crypto"
    assert base_of("BTC-USD") == "BTC"
    assert BinanceSource.pair("DOGE-USD") == "DOGEUSDT" and BybitSource.pair("ETH-USD") == "ETHUSDT" and OKXSource.inst("BTC-USD") == "BTC-USDT"


def test_coinbase_messages_build_trades_and_quotes():
    src = CoinbaseSource(cfg(), ["BTC-USD", "ETH-USD"])
    ticks, emit = collect()
    src._handle({"type": "match", "product_id": "BTC-USD", "price": "83000.5", "size": "0.01", "side": "sell",
                 "time": "2026-10-11T00:00:01.500000Z"}, emit)
    src._handle({"type": "match", "product_id": "BTC-USD", "price": "83001", "size": "0.02", "side": "buy",
                 "time": "2026-10-11T00:00:02Z"}, emit)
    src._handle({"type": "ticker", "product_id": "ETH-USD", "price": "4000", "best_bid": "3999", "best_ask": "4001",
                 "time": "2026-10-11T00:00:03Z"}, emit)
    src._handle({"type": "match", "product_id": "SOL-USD", "price": "150", "size": "1", "side": "buy"}, emit)   # not subscribed
    src._handle({"type": "error", "message": "bad"}, emit)
    assert len(ticks) == 3
    t0, t1, t2 = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83000.5 and t0.size == 0.01 and t0.taker_buy is True    # a sell-side maker: the taker bought
    assert t1.taker_buy is False and abs(t1.ts - 1791676802.0) < 1e-6 and not t1.quote
    assert t2.quote is True and t2.bid == 3999.0 and t2.ask == 4001.0 and t2.size == 0.0
    assert "bad" in src.note


def test_coinbase_candles_page_oldest_first_and_bounded(monkeypatch):
    src = CoinbaseSource(cfg(), ["BTC-USD"])
    rows = [[1791676800 + 300 * k, 100 - k, 110 + k, 101, 105 + k, 2.5] for k in range(5)][::-1]   # newest first, [t, low, high, open, close, vol]

    class R:
        status_code = 200
        def json(self):
            return rows

    class C:
        async def get(self, url, params=None):
            assert params["granularity"] == 300 and "BTC-USD" in url
            return R()
    out = asyncio.run(src._candles(C(), "BTC-USD", 1791676800 + 300, 1791676800 + 300 * 4))
    assert [t.ts for t in out] == [1791676800 + 300, 1791676800 + 600, 1791676800 + 900]     # [start, end), oldest first
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 99 and out[0].price == 106 and out[0].size == 2.5


def test_coinbase_404_marks_a_name_unsupported():
    src = CoinbaseSource(cfg(), ["BTC-USD"])

    class R:
        status_code = 404
        def json(self):
            return {}

    class C:
        async def get(self, url, params=None):
            return R()
    assert asyncio.run(src._candles(C(), "NOPE-USD", 0, 300)) == []
    assert not src.supports("NOPE-USD")


def test_kraken_messages_and_pairs():
    src = KrakenSource(cfg(), ["BTC-USD", "DOGE-USD"])
    src.pairs = {"BTC-USD": ("XBTUSD", "BTC/USD"), "DOGE-USD": ("XDGUSD", "DOGE/USD")}
    src._wsn = {"BTC/USD": "BTC-USD", "DOGE/USD": "DOGE-USD"}
    ticks, emit = collect()
    src._handle({"channel": "trade", "data": [{"symbol": "BTC/USD", "side": "buy", "price": 83000.0, "qty": 0.5,
                                              "timestamp": "2026-10-11T00:00:01.000000Z"}]}, emit)
    src._handle({"channel": "ticker", "data": [{"symbol": "DOGE/USD", "bid": 0.19, "ask": 0.191, "last": 0.1905}]}, emit)
    src._handle({"channel": "heartbeat"}, emit)
    assert len(ticks) == 2 and ticks[0].taker_buy is True and ticks[0].size == 0.5 and ticks[1].quote and ticks[1].bid == 0.19
    assert src.knows("BTC-USD") and not src.knows("SOL-USD")


def test_binance_bybit_okx_messages():
    b = BinanceSource(cfg(), ["BTC-USD"])
    b._pairs = {"BTCUSDT": "BTC-USD"}
    ticks, emit = collect()
    b._handle({"stream": "btcusdt@trade", "data": {"e": "trade", "s": "BTCUSDT", "p": "83000", "q": "0.1", "T": 1791676801000, "m": True}}, emit)
    b._handle({"stream": "btcusdt@bookTicker", "data": {"s": "BTCUSDT", "b": "82999", "a": "83001"}}, emit)
    assert ticks[0].taker_buy is False and ticks[0].ts == 1791676801.0 and ticks[1].quote and ticks[1].bid == 82999.0
    y = BybitSource(cfg(), ["BTC-USD"])
    y._pairs = {"BTCUSDT": "BTC-USD"}
    ticks, emit = collect()
    y._handle({"topic": "publicTrade.BTCUSDT", "data": [{"s": "BTCUSDT", "p": "83000", "v": "0.2", "S": "Buy", "T": 1791676801000}]}, emit)
    assert ticks[0].taker_buy is True and ticks[0].size == 0.2
    o = OKXSource(cfg(), ["BTC-USD"])
    o._insts = {"BTC-USDT": "BTC-USD"}
    ticks, emit = collect()
    o._handle({"arg": {"channel": "trades", "instId": "BTC-USDT"}, "data": [{"instId": "BTC-USDT", "px": "83000", "sz": "0.3", "side": "sell", "ts": "1791676801000"}]}, emit)
    o._handle({"arg": {"channel": "tickers", "instId": "BTC-USDT"}, "data": [{"instId": "BTC-USDT", "last": "83000", "bidPx": "82999", "askPx": "83001"}]}, emit)
    assert ticks[0].taker_buy is False and ticks[1].quote and ticks[1].ask == 83001.0


def test_manager_defaults_and_toggle():
    c = cfg()
    m = SourceManager(c, c.symbols, on_tick=lambda t: None)
    assert m.enabled["coinbase"] and m.enabled["kraken"] and m.enabled["sim"]
    assert not m.enabled["binance"] and not m.enabled["bybit"] and not m.enabled["okx"] and not m.enabled["coingecko"]
    assert m.toggle("nope", True) == {"error": "unknown source"}
    forced = SourceManager(cfg(sources_on="kraken"), c.symbols, on_tick=lambda t: None)
    assert forced.enabled["kraken"] and forced.enabled["sim"] and not forced.enabled["coinbase"]


def test_manager_routes_the_highest_priority_fresh_source_and_fails_over():
    c = cfg()
    got = []
    quotes = []
    m = SourceManager(c, c.symbols, on_tick=got.append, on_quote=lambda s, b, a: quotes.append((s, b, a)))
    m.sources["coinbase"].fresh_after = 1.0
    now = time.time()
    m._route("kraken", Tick("BTC-USD", now, 1.0, 1.0, True))
    assert m.active["BTC-USD"] == "kraken" and len(got) == 1
    m._route("coinbase", Tick("BTC-USD", now, 2.0, 1.0, True, 1.9, 2.1))
    assert m.active["BTC-USD"] == "coinbase" and len(got) == 2
    m._route("kraken", Tick("BTC-USD", now, 3.0, 1.0, True, 2.9, 3.1))
    assert len(got) == 2 and quotes[-1] == ("BTC-USD", 2.9, 3.1)       # kraken's trade is dropped, its quote keeps the spread fresh
    m._route("sim", Tick("BTC-USD", now, 60000.0, 1.0, True, 59990.0, 60010.0))
    assert quotes[-1] == ("BTC-USD", 2.9, 3.1)                           # the simulator's quotes never touch the live spread
    m._route("binance", Tick("BTC-USD", now, 2.002, 1.0, True, 2.001, 2.003))
    assert quotes[-1] == ("BTC-USD", 2.9, 3.1)                           # a USDT book sits a USDT basis off Coinbase's USD book: not the spread
    m._route("gemini", Tick("BTC-USD", now, 2.0, 1.0, True, 1.95, 2.05))
    assert quotes[-1] == ("BTC-USD", 1.95, 2.05)                         # another USD venue may refresh it
    m.seen["BTC-USD"]["coinbase"] = now - 5.0                            # coinbase goes stale
    m._route("kraken", Tick("BTC-USD", now, 4.0, 1.0, True))
    assert m.active["BTC-USD"] == "kraken" and len(got) == 3
    m.toggle("kraken", False)
    assert m.active["BTC-USD"] is None                                   # never the simulator for a name a live venue lists: nobody, so the stall alarm fires
    m.seen["BTC-USD"].pop("sim", None)
    assert m.provider_for("BTC-USD") is None
    m.sources["coinbase"].products = {"BTC-USD"}
    m.sources["hyperliquid"].coins = {"BTC-USD": "BTC"}                  # the other default-on venue lists BTC only
    assert m.tradable("BTC-USD") and not m.tradable("SOL-USD")          # what the universe may promote: a listed name on an enabled venue


def test_manager_backfill_prefers_the_richest_history():
    c = cfg()
    m = SourceManager(c, c.symbols, on_tick=lambda t: None)
    for sid in list(m.enabled):
        m.enabled[sid] = sid in ("coinbase", "kraken")

    async def cb(symbols, cutoff):
        return [Tick("BTC-USD", 1.0, 1.0, 1.0, None), Tick("BTC-USD", 2.0, 1.0, 1.0, None)]

    async def kr(symbols, cutoff):
        return [Tick("BTC-USD", 3.0, 1.0, 1.0, None), Tick("ETH-USD", 1.0, 1.0, 1.0, None)] + \
               [Tick("ETH-USD", 2.0 + k, 1.0, 1.0, None) for k in range(3)]
    m.sources["coinbase"].backfill = cb
    m.sources["kraken"].backfill = kr
    ticks, chosen = asyncio.run(m.backfill(0.0))
    assert chosen == {"BTC-USD": "coinbase", "ETH-USD": "kraken"} and len(ticks) == 6
    assert [t.ts for t in ticks] == sorted(t.ts for t in ticks)
    # richness is bars covered, not ticks: five days of candles beat six hours of dense synthetic trades
    m.enabled["sim"] = True

    async def candles(symbols, cutoff):
        return [Tick("BTC-USD", 1_791_000_000.0 + 300.0 * k, 1.0, 1.0, None, o=1.0, h=1.0, l=1.0) for k in range(1440)]

    async def dense(symbols, cutoff):
        return [Tick("BTC-USD", 1_791_400_000.0 + 5.0 * k, 1.0, 1.0, True) for k in range(4320)]
    m.sources["coinbase"].backfill = candles
    m.sources["kraken"].backfill = lambda symbols, cutoff: cb(symbols, cutoff)
    m.sources["sim"].backfill = dense
    ticks, chosen = asyncio.run(m.backfill(0.0))
    assert chosen["BTC-USD"] == "coinbase" and len([t for t in ticks if t.symbol == "BTC-USD"]) == 1440


def test_perp_venues_and_the_simulator_only_seed_names_no_spot_venue_served():
    c = cfg()
    m = SourceManager(c, c.symbols, on_tick=lambda t: None)
    for sid in list(m.enabled):
        m.enabled[sid] = sid in ("coinbase", "hyperliquid", "sim")
    asked = {}

    async def cb(symbols, cutoff):
        asked["coinbase"] = list(symbols)
        return [Tick("BTC-USD", 1_791_000_000.0 + 300.0 * k, 1.0, 1.0, None, o=1.0, h=1.0, l=1.0) for k in range(10)]

    async def hl(symbols, cutoff):
        asked["hyperliquid"] = list(symbols)
        return [Tick(s, 1_791_000_000.0 + 300.0 * k, 1.0, 1.0, None, o=1.0, h=1.0, l=1.0) for s in symbols for k in range(20)]

    async def sim(symbols, cutoff):
        asked["sim"] = list(symbols)
        return [Tick(s, 1_791_000_000.0 + 5.0 * k, 1.0, 1.0, True) for s in symbols for k in range(100)]
    m.sources["coinbase"].backfill = cb
    m.sources["hyperliquid"].backfill = hl
    m.sources["sim"].backfill = sim
    ticks, chosen = asyncio.run(m.backfill(0.0))
    assert asked["coinbase"] == ["BTC-USD", "ETH-USD", "DOGE-USD"]
    assert asked["hyperliquid"] == ["ETH-USD", "DOGE-USD"] and asked["sim"] == ["ETH-USD", "DOGE-USD"]   # only the names Coinbase left empty
    assert chosen == {"BTC-USD": "coinbase", "ETH-USD": "hyperliquid", "DOGE-USD": "hyperliquid"}       # BTC keeps the spot history even though the perp's is longer
    asked.clear()

    async def cb_all(symbols, cutoff):
        asked["coinbase"] = list(symbols)
        return [Tick(s, 1_791_000_000.0 + 300.0 * k, 1.0, 1.0, None, o=1.0, h=1.0, l=1.0) for s in symbols for k in range(10)]
    m.sources["coinbase"].backfill = cb_all
    ticks, chosen = asyncio.run(m.backfill(0.0))
    assert set(asked) == {"coinbase"} and set(chosen.values()) == {"coinbase"}                          # nothing missing: the second wave never runs


def test_sim_source_covers_everything_and_backfills_only_a_little():
    s = SimSource(cfg(), ["BTC-USD", "ETH-USD"])
    assert s.symbols == ["BTC-USD", "ETH-USD"] and s.priority == 200
    ticks = asyncio.run(s.backfill(["BTC-USD"], time.time() - 600))
    assert ticks and all(t.symbol == "BTC-USD" for t in ticks)
    big = SimSource(cfg(), [f"C{k}-USD" for k in range(60)])
    t0 = time.time()
    ticks = asyncio.run(big.backfill(big.symbols, time.time() - 5 * 86400.0))     # five days asked, six hours served
    assert time.time() - t0 < 10.0
    assert ticks and min(t.ts for t in ticks) > time.time() - 7 * 3600.0
    assert len({t.symbol for t in ticks}) == 60


def test_coingecko_is_quote_only():
    g = CoinGeckoSource(cfg(), ["BTC-USD"])
    assert g.mechanism == "poll" and g.priority == 90 and g.poll_interval >= 30


def test_default_on_plus_syntax_and_persisted_toggles(tmp_path):
    from steel.sources import enabled_by_default
    assert enabled_by_default(CoinbaseSource, cfg()) and not enabled_by_default(BybitSource, cfg())
    assert not enabled_by_default(CoinbaseSource, cfg(sources_off="coinbase"))
    assert enabled_by_default(BybitSource, cfg(sources_on="+bybit")) and enabled_by_default(CoinbaseSource, cfg(sources_on="+bybit"))   # a plus adds to the defaults
    assert enabled_by_default(BybitSource, cfg(sources_on="bybit")) and not enabled_by_default(CoinbaseSource, cfg(sources_on="bybit"))  # a bare list is exact
    assert enabled_by_default(SimSource, cfg(sources_on="bybit"))                                                                        # the simulator is always there
    c = cfg()
    path = tmp_path / "sources.json"
    m = SourceManager(c, c.symbols, on_tick=lambda t: None, state_path=path)
    assert not m.enabled["bybit"] and m.enabled["kraken"]

    async def flip():
        m.toggle("bybit", True)
        m.toggle("kraken", False)
        await m.stop()
    asyncio.run(flip())
    assert path.exists()
    m2 = SourceManager(c, c.symbols, on_tick=lambda t: None, state_path=path)              # a restart keeps the panel's choices
    assert m2.enabled["bybit"] and not m2.enabled["kraken"] and m2.enabled["coinbase"] and m2.enabled["sim"]
    st = {s["id"]: s for s in m2.status()}
    assert st["coinbase"]["venue"] == "spot" and st["coinbase"]["default_on"] and st["coingecko"]["venue"] == "aggregate" and st["sim"]["venue"] == "simulated"
    assert st["coinbase"]["url"].startswith("https://")


def test_tick_helpers_and_unscale():
    from steel.sources import quote_tick, trade_tick, unscale
    assert unscale("1000PEPE") == ("PEPE", 1000.0) and unscale("kPEPE") == ("PEPE", 1000.0) and unscale("1000000MOG") == ("MOG", 1e6)
    assert unscale("BTC") == ("BTC", 1.0) and unscale("1INCH") == ("1INCH", 1.0) and unscale("KAS") == ("KAS", 1.0)   # a digit-led name is not a scale
    t = trade_tick("BTC-USD", "83000.5", "0.01", True, 1791676801.0)
    assert t.price == 83000.5 and t.size == 0.01 and t.taker_buy is True and t.ts == 1791676801.0 and not t.quote
    assert trade_tick("BTC-USD", "0", "1", True) is None and trade_tick("BTC-USD", "x", "1", True) is None
    q = quote_tick("BTC-USD", None, "82999", "83001")
    assert q.quote and q.price == 83000.0 and q.bid == 82999.0 and q.ask == 83001.0 and q.size == 0.0
    assert quote_tick("BTC-USD", "83000", None, None).bid is None and quote_tick("BTC-USD", None, None, None) is None


def test_ranged_backfill_takes_each_name_from_the_first_venue_that_lists_it():
    import asyncio
    c = cfg(sources_on="coinbase,kraken")
    m = SourceManager(c, ["BTC-USD", "ETH-USD", "ZZZ-USD"], on_tick=lambda t: None)
    calls = []

    class Fake:
        def __init__(self, sid, prio, lists, fail=False):
            self.id, self.name, self.priority, self.lists, self.fail = sid, sid, prio, lists, fail
        def supports(self, s): return True
        def knows(self, s): return s in self.lists
        async def backfill_range(self, symbols, cutoff, until):
            calls.append((self.id, tuple(symbols)))
            if self.fail:
                raise RuntimeError("down")
            return [Tick(s, cutoff + 1.0, 1.0, 1.0, True) for s in symbols]
    m.sources = {"coinbase": Fake("coinbase", 10, {"BTC-USD", "ETH-USD"}), "kraken": Fake("kraken", 20, {"BTC-USD", "ZZZ-USD"}),
                 "bybit": Fake("bybit", 30, {"ZZZ-USD"})}
    m.enabled = {"coinbase": True, "kraken": True, "bybit": False}
    ticks = asyncio.run(m.backfill_range(0.0, 100.0))
    assert calls == [("coinbase", ("BTC-USD", "ETH-USD")), ("kraken", ("ZZZ-USD",))]       # each name once, from the first venue that lists it
    assert {t.symbol for t in ticks} == {"BTC-USD", "ETH-USD", "ZZZ-USD"} and m.deep_venue == {"BTC-USD": "coinbase", "ETH-USD": "coinbase", "ZZZ-USD": "kraken"}
    # a venue that fails is skipped and the next one serves its names
    calls.clear()
    m.sources["coinbase"].fail = True
    ticks = asyncio.run(m.backfill_range(0.0, 100.0))
    assert [c[0] for c in calls] == ["coinbase", "kraken"] and {t.symbol for t in ticks} == {"BTC-USD", "ZZZ-USD"}


def test_the_simulator_never_speaks_for_a_name_a_live_venue_lists():
    """The first GPU run's first live bar for every name was the simulator's (BTC 60,000, SOL 150):
    the venues' sockets had not delivered yet and the sim was the only fresh source. A listed
    name is served by a live venue or by nobody, so the stall alarm fires instead of fake bars."""
    c = cfg()
    got, changes = [], []
    m = SourceManager(c, ["BTC-USD", "ZZZ-USD"], on_tick=got.append, on_provider_change=lambda s, p, a: changes.append((s, p, a)))
    now = time.time()
    # BTC is listed by the live venues, ZZZ by nobody: the sim may serve ZZZ, never BTC
    for sid, src in m.sources.items():
        if sid != "sim":
            src.knows = lambda s: s == "BTC-USD"
    assert m.tradable("BTC-USD") and not m.tradable("ZZZ-USD")
    m._route("sim", Tick("BTC-USD", now, 60000.0, 1.0, True))
    m._route("sim", Tick("ZZZ-USD", now, 10.0, 1.0, True))
    assert [t.symbol for t in got] == ["ZZZ-USD"] and m.provider_for("BTC-USD") is None and m.provider_for("ZZZ-USD") == "sim"
    # a live venue serves BTC; when it goes stale the provider is nobody, not the simulator
    m._route("coinbase", Tick("BTC-USD", now, 83000.0, 1.0, True))
    assert m.provider_for("BTC-USD") == "coinbase" and got[-1].price == 83000.0
    m.seen["BTC-USD"]["coinbase"] = now - 10_000
    m._route("sim", Tick("BTC-USD", now, 60100.0, 1.0, True))
    assert m.provider_for("BTC-USD") is None and got[-1].price == 83000.0
    # a pure simulator run (nothing else enabled) is unaffected
    solo = SourceManager(cfg(sources_on="sim"), ["BTC-USD"], on_tick=got.append)
    solo._route("sim", Tick("BTC-USD", now, 60000.0, 1.0, True))
    assert got[-1].price == 60000.0 and solo.provider_for("BTC-USD") == "sim"
