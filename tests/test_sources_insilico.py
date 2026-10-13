"""Hyperliquid, BloFin, BitMEX and ApeX Omni sources: symbol mapping both ways, one trade and one
quote through `_handle`, candle paging with a fake client, the thousand-token contracts and the
instrument lists. No network."""
import asyncio

from steel.config import Config
from steel.sources.apex import ApeXSource
from steel.sources.bitmex import BitMEXSource
from steel.sources.blofin import BloFinSource
from steel.sources.hyperliquid import HyperliquidSource

T0 = 1791676800                                     # 2026-10-11T00:00:00Z


def cfg(**kw):
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD", "PEPE-USD"]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def collect():
    out = []
    return out, out.append


class R:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.headers = body, status, {}

    def json(self):
        return self.body


# Hyperliquid ----------------------------------------------------------------------

def test_hyperliquid_symbols_and_universe():
    assert HyperliquidSource.coin("BTC-USD") == "BTC" and HyperliquidSource.coin("PEPE-USD") == "kPEPE"
    assert HyperliquidSource.canonical("kPEPE") == "PEPE-USD" and HyperliquidSource.canonical("BTC") == "BTC-USD"
    src = HyperliquidSource(cfg(), ["BTC-USD", "PEPE-USD", "SOL-USD"])
    assert src.knows("SOL-USD")                                                   # nothing listed yet: optimistic

    class C:
        async def post(self, url, json=None):
            assert json == {"type": "meta"}
            return R({"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "kPEPE", "szDecimals": 0},
                                   {"name": "MATIC", "szDecimals": 1, "isDelisted": True}]})
    assert asyncio.run(src.list_coins(C())) == {"BTC-USD": "BTC", "PEPE-USD": "kPEPE"}
    assert src.knows("BTC-USD") and src.knows("PEPE-USD") and not src.knows("SOL-USD") and not src.knows("MATIC-USD")
    assert src._coin("PEPE-USD") == "kPEPE" and src.priority == 70 and src.venue == "perp" and src.default_on


def test_hyperliquid_messages():
    src = HyperliquidSource(cfg(), ["BTC-USD", "PEPE-USD"])
    src._rev = {"BTC": "BTC-USD", "kPEPE": "PEPE-USD"}
    ticks, emit = collect()
    src._handle({"channel": "trades", "data": [{"coin": "BTC", "side": "B", "px": "83010.0", "sz": "0.553", "time": 1791674096968, "tid": 1},
                                               {"coin": "BTC", "side": "A", "px": "83009.0", "sz": "0.25", "time": 1791674096968, "tid": 2},
                                               {"coin": "ETH", "side": "B", "px": "2500", "sz": "1", "time": 1791674096968, "tid": 3}]}, emit)
    src._handle({"channel": "bbo", "data": {"coin": "BTC", "time": 1791674157306, "bbo": [{"px": "83009.0", "sz": "3.9", "n": 10}, {"px": "83010.0", "sz": "7.7", "n": 43}]}}, emit)
    src._handle({"channel": "trades", "data": [{"coin": "kPEPE", "side": "A", "px": "0.004035", "sz": "15600.0", "time": 1791673622276, "tid": 4}]}, emit)
    src._handle({"channel": "bbo", "data": {"coin": "BTC", "time": 1791674157306, "bbo": [None, {"px": "83010.0", "sz": "7.7", "n": 43}]}}, emit)  # one side empty: no quote
    src._handle({"channel": "pong"}, emit)
    src._handle({"channel": "error", "data": "Invalid subscription"}, emit)
    assert len(ticks) == 4
    t0, t1, q, p = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83010.0 and t0.size == 0.553 and t0.taker_buy is True and t0.ts == 1791674096.968 and not t0.quote
    assert t1.taker_buy is False
    assert q.quote and q.bid == 83009.0 and q.ask == 83010.0 and q.price == 83009.5 and q.ts == 1791674157.306
    assert p.symbol == "PEPE-USD" and abs(p.price - 4.035e-6) < 1e-12 and p.size == 15_600_000.0 and p.taker_buy is False
    assert "Invalid subscription" in src.note


def test_hyperliquid_candles_scale_and_bound():
    src = HyperliquidSource(cfg(), ["PEPE-USD"])
    src.coins = {"PEPE-USD": "kPEPE"}
    rows = [{"t": (T0 + 300 * k) * 1000, "T": (T0 + 300 * k) * 1000 + 299999, "s": "kPEPE", "i": "5m",
             "o": "0.004", "c": "0.0041", "h": "0.0042", "l": "0.0039", "v": "1000.0", "n": 3} for k in (3, 0, 1, 2)]
    calls = []

    class C:
        async def post(self, url, json=None):
            calls.append(json)
            return R(rows)
    out = asyncio.run(src._candles(C(), "PEPE-USD", T0, T0 + 900))
    assert calls[0]["type"] == "candleSnapshot" and calls[0]["req"] == {"coin": "kPEPE", "interval": "5m", "startTime": T0 * 1000, "endTime": (T0 + 900) * 1000}
    assert [t.ts for t in out] == [T0, T0 + 300, T0 + 600]                       # [start, end), oldest first
    assert abs(out[0].o - 4e-6) < 1e-15 and abs(out[0].h - 4.2e-6) < 1e-15 and abs(out[0].l - 3.9e-6) < 1e-15
    assert abs(out[0].price - 4.1e-6) < 1e-15 and out[0].size == 1_000_000.0 and out[0].symbol == "PEPE-USD"

    class Bad:
        async def post(self, url, json=None):
            return R(None, 500)
    assert asyncio.run(src._candles(Bad(), "PEPE-USD", T0, T0 + 900)) == [] and "500" in src.note


# BloFin ---------------------------------------------------------------------------

def test_blofin_instruments_and_symbols():
    assert BloFinSource.inst("BTC-USD") == "BTC-USDT"
    src = BloFinSource(cfg(), ["BTC-USD", "PEPE-USD", "SOL-USD"])
    assert src.priority == 72 and src.venue == "perp" and not src.default_on

    class C:
        async def get(self, url, params=None):
            assert params == {"instType": "SWAP"} and url.endswith("/market/instruments")
            return R({"code": "0", "data": [{"instId": "BTC-USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT", "contractValue": "0.001", "state": "live"},
                                            {"instId": "BTC-USDC", "baseCurrency": "BTC", "quoteCurrency": "USDC", "contractValue": "0.0001", "state": "live"},
                                            {"instId": "1000PEPE-USDT", "baseCurrency": "1000PEPE", "quoteCurrency": "USDT", "contractValue": "1", "state": "live"},
                                            {"instId": "SOL-USDT", "baseCurrency": "SOL", "quoteCurrency": "USDT", "contractValue": "1", "state": "suspend"}]})
    insts = asyncio.run(src.list_instruments(C()))
    assert insts == {"BTC-USD": ("BTC-USDT", 1.0, 0.001), "PEPE-USD": ("1000PEPE-USDT", 1000.0, 1.0)}
    assert src.knows("BTC-USD") and src.knows("PEPE-USD") and not src.knows("SOL-USD")
    assert src._inst("ETH-USD") == ("ETH-USDT", 1.0, 1.0)


def test_blofin_messages():
    src = BloFinSource(cfg(), ["BTC-USD", "PEPE-USD"])
    src.insts = {"BTC-USD": ("BTC-USDT", 1.0, 0.001), "PEPE-USD": ("1000PEPE-USDT", 1000.0, 1.0)}
    src._pairs = {"BTC-USDT": "BTC-USD", "1000PEPE-USDT": "PEPE-USD"}
    ticks, emit = collect()
    src._handle({"arg": {"channel": "trades", "instId": "BTC-USDT"}, "data": [{"instId": "BTC-USDT", "tradeId": "1", "price": "83015.1", "size": "11", "side": "buy", "ts": "1791674124258"}]}, emit)
    src._handle({"arg": {"channel": "tickers", "instId": "BTC-USDT"}, "data": [{"instId": "BTC-USDT", "last": "83015.1", "bidPrice": "83014.9", "askPrice": "83015.1", "ts": "1791674124258"}]}, emit)
    src._handle({"arg": {"channel": "trades", "instId": "1000PEPE-USDT"}, "data": [{"instId": "1000PEPE-USDT", "tradeId": "2", "price": "0.004035", "size": "5", "side": "sell", "ts": "1791674124258"}]}, emit)
    src._handle({"event": "subscribe", "arg": {"channel": "trades", "instId": "BTC-USDT"}}, emit)
    src._handle({"event": "error", "code": "60012", "msg": "Invalid request"}, emit)
    assert len(ticks) == 3
    t, q, p = ticks
    assert t.price == 83015.1 and t.size == 0.011 and t.taker_buy is True and t.ts == 1791674124.258 and not t.quote    # 11 contracts of 0.001 BTC
    assert q.quote and q.bid == 83014.9 and q.ask == 83015.1 and q.price == 83015.1
    assert p.symbol == "PEPE-USD" and abs(p.price - 4.035e-6) < 1e-12 and p.size == 5000.0 and p.taker_buy is False
    assert "Invalid request" in src.note


def test_blofin_candles_page_newest_first_and_unknown():
    src = BloFinSource(cfg(), ["BTC-USD"])
    rows = [[str((T0 + 300 * k) * 1000), "100", "110", "90", "105", "1337", "1.337", "140000", "1"] for k in (4, 3, 2, 1, 0)]   # newest first
    calls = []

    class C:
        async def get(self, url, params=None):
            calls.append(params)
            return R({"code": "0", "msg": "success", "data": rows})
    out = asyncio.run(src._candles(C(), "BTC-USD", T0 + 300, T0 + 1200))
    assert calls[0]["instId"] == "BTC-USDT" and calls[0]["bar"] == "5m" and calls[0]["after"] == (T0 + 1200) * 1000 and calls[0]["limit"] == 300
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900]                 # [start, end), oldest first
    assert out[0].o == 100 and out[0].h == 110 and out[0].l == 90 and out[0].price == 105 and out[0].size == 1.337    # volCurrency, not contracts
    assert len(calls) == 1                                                        # a short page ends the walk

    class Unknown:
        async def get(self, url, params=None):
            return R({"code": "152002", "msg": "Parameter instId error."})
    assert asyncio.run(src._candles(Unknown(), "NOPE-USD", T0, T0 + 900)) == [] and not src.supports("NOPE-USD")


# BitMEX ---------------------------------------------------------------------------

def test_bitmex_symbols_and_instruments():
    assert BitMEXSource.pair("BTC-USD") == "XBTUSDT" and BitMEXSource.pair("ETH-USD") == "ETHUSDT"
    assert BitMEXSource.canonical("XBTUSD") == "BTC-USD" and BitMEXSource.canonical("ETHUSDT") == "ETH-USD"
    assert BitMEXSource.canonical("1000000MOGUSDT") == "MOG-USD" and BitMEXSource.canonical("XBTEUR") is None
    src = BitMEXSource(cfg(), ["BTC-USD", "ETH-USD", "SOL-USD"])
    assert src.priority == 74 and src.venue == "perp" and not src.default_on

    class C:
        async def get(self, url, params=None):
            assert url.endswith("/instrument/active")
            return R([{"symbol": "XBTUSD", "typ": "FFWCSX", "state": "Open", "quoteCurrency": "USD"},
                      {"symbol": "XBTUSDT", "typ": "FFWCSX", "state": "Open", "quoteCurrency": "USDT"},
                      {"symbol": "ETHUSD", "typ": "FFWCSX", "state": "Open", "quoteCurrency": "USD"},
                      {"symbol": "SOLUSDT", "typ": "FFWCSX", "state": "Settled", "quoteCurrency": "USDT"},
                      {"symbol": ".BXBT", "typ": "IFXXXP", "state": "Unlisted", "quoteCurrency": "USD"}])
    assert asyncio.run(src.list_instruments(C())) == {"BTC-USD": ("XBTUSDT", 1.0), "ETH-USD": ("ETHUSD", 1.0)}    # the USDT-linear perp wins
    assert src.knows("BTC-USD") and src.knows("ETH-USD") and not src.knows("SOL-USD")
    closed = BitMEXSource(cfg(), ["BTC-USD"])

    class Closed:
        async def get(self, url, params=None):
            return R([{"symbol": ".BXBT", "typ": "IFXXXP", "state": "Unlisted", "quoteCurrency": "USD"}])
    assert asyncio.run(closed.list_instruments(Closed())) == {} and not closed.knows("BTC-USD")                  # nothing open is a true answer


def test_bitmex_buckets_restamped_to_their_start():
    src = BitMEXSource(cfg(), ["BTC-USD"])
    rows = [{"timestamp": "2026-10-11T00:05:00.000Z", "symbol": "XBTUSDT", "open": 100, "high": 110, "low": 90, "close": 105, "trades": 4, "volume": 300000, "homeNotional": 0.3},
            {"timestamp": "2026-10-11T00:10:00.000Z", "symbol": "XBTUSDT", "open": 105, "high": 105, "low": 105, "close": 105, "trades": 0, "volume": 0, "homeNotional": 0},
            {"timestamp": "2026-10-11T00:15:00.000Z", "symbol": "XBTUSDT", "open": 105, "high": 120, "low": 100, "close": 115, "trades": 2, "volume": 100000, "homeNotional": 0.1},
            {"timestamp": "2026-10-11T00:20:00.000Z", "symbol": "XBTUSDT", "open": 115, "high": 115, "low": 115, "close": 115, "trades": 1, "volume": 100, "homeNotional": 0.0001}]
    calls = []

    class C:
        async def get(self, url, params=None):
            calls.append((url, params))
            return R(rows)
    out = asyncio.run(src._buckets(C(), "BTC-USD", T0, T0 + 900, "XBTUSDT", 1.0))
    url, params = calls[0]
    assert url.endswith("/trade" + "/bucketed") and params["symbol"] == "XBTUSDT" and params["binSize"] == "5m" and params["partial"] == "false"
    assert params["startTime"] == "2026-10-11T00:05:00Z" and params["endTime"] == "2026-10-11T00:20:00Z"      # stamps are bucket ends
    assert [t.ts for t in out] == [T0, T0 + 600]                                  # the empty bucket is skipped, the 00:20 stamp is outside [start, end)
    assert out[0].o == 100 and out[0].h == 110 and out[0].l == 90 and out[0].price == 105 and out[0].size == 0.3   # homeNotional, not contracts
    assert len(calls) == 1


def test_bitmex_messages():
    src = BitMEXSource(cfg(), ["BTC-USD"])
    src.insts = {"BTC-USD": ("XBTUSDT", 1.0)}
    src._pairs = {"XBTUSDT": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"table": "trade", "action": "partial", "data": [{"timestamp": "2026-10-11T00:00:01.000Z", "symbol": "XBTUSDT", "side": "Buy", "size": 100000, "price": 83000, "homeNotional": 0.1}]}, emit)
    src._handle({"table": "trade", "action": "insert", "data": [{"timestamp": "2026-10-11T00:00:01.500Z", "symbol": "XBTUSDT", "side": "Sell", "size": 100000, "price": 83000.5, "homeNotional": 0.1}]}, emit)
    src._handle({"table": "quote", "action": "insert", "data": [{"timestamp": "2026-10-11T00:00:02.000Z", "symbol": "XBTUSDT", "bidSize": 1000, "bidPrice": 82999.5, "askPrice": 83000.5, "askSize": 2000}]}, emit)
    src._handle({"error": "Invalid subscription"}, emit)
    assert len(ticks) == 2                                                        # the partial replay is dropped
    t, q = ticks
    assert t.price == 83000.5 and t.size == 0.1 and t.taker_buy is False and t.ts == T0 + 1.5 and not t.quote
    assert q.quote and q.bid == 82999.5 and q.ask == 83000.5 and q.price == 83000.0 and q.ts == T0 + 2.0
    assert "Invalid subscription" in src.note


# ApeX Omni ------------------------------------------------------------------------

def test_apex_symbols_and_instruments():
    assert ApeXSource.pair("BTC-USD") == "BTCUSDT"
    src = ApeXSource(cfg(), ["BTC-USD", "PEPE-USD", "SOL-USD"])
    assert src.priority == 80 and src.venue == "perp" and not src.default_on

    class C:
        async def get(self, url, params=None):
            assert url.endswith("/symbols")
            return R({"data": {"contractConfig": {
                "perpetualContract": [{"symbol": "BTC-USDT", "crossSymbolName": "BTCUSDT", "enableTrade": True, "settleAssetId": "USDT"},
                                      {"symbol": "1000PEPE-USDT", "crossSymbolName": "1000PEPEUSDT", "enableTrade": True, "settleAssetId": "USDT"},
                                      {"symbol": "SOL-USDT", "crossSymbolName": "SOLUSDT", "enableTrade": False, "settleAssetId": "USDT"}],
                "stockContract": [{"symbol": "TSLA-USDT", "crossSymbolName": "TSLAUSDT", "enableTrade": True, "settleAssetId": "USDT"}],
                "predictionContract": [{"symbol": "BTC_hit_130k_Dec-USDT", "crossSymbolName": "BTC_hit_130k_DecUSDT", "enableTrade": True}]}}})
    assert asyncio.run(src.list_instruments(C())) == {"BTC-USD": ("BTCUSDT", 1.0), "PEPE-USD": ("1000PEPEUSDT", 1000.0)}
    assert src.knows("BTC-USD") and not src.knows("SOL-USD") and not src.knows("TSLA-USD")


def test_apex_klines_page_forward_in_seconds():
    src = ApeXSource(cfg(), ["BTC-USD"])
    first = [{"s": "BTCUSDT", "i": "5", "t": (T0 + 300 * k) * 1000, "o": "100", "h": "110", "l": "90", "c": "105", "v": "12.4", "tr": "1"} for k in range(200)]
    second = [{"s": "BTCUSDT", "i": "5", "t": (T0 + 300 * k) * 1000, "o": "105", "h": "106", "l": "104", "c": "106", "v": "1", "tr": "1"} for k in (200, 201)]
    calls = []

    class C:
        async def get(self, url, params=None):
            calls.append(params)
            return R({"data": {"BTCUSDT": first if len(calls) == 1 else second}})
    out = asyncio.run(src._klines(C(), "BTC-USD", T0, T0 + 300 * 201))
    assert calls[0] == {"symbol": "BTCUSDT", "interval": 5, "start": T0, "end": T0 + 300 * 201, "limit": 200}
    assert calls[1]["start"] == T0 + 300 * 200 and len(calls) == 2                # the next page starts after the last bucket
    assert [t.ts for t in out] == [T0 + 300 * k for k in range(201)]               # oldest first, [start, end)
    assert out[0].o == 100 and out[0].h == 110 and out[0].l == 90 and out[0].price == 105 and out[0].size == 12.4

    class Empty:
        async def get(self, url, params=None):
            return R({"data": {}})
    assert asyncio.run(src._klines(Empty(), "NOPE-USD", T0, T0 + 900)) == []


def test_apex_messages():
    src = ApeXSource(cfg(), ["BTC-USD", "PEPE-USD"])
    src.insts = {"BTC-USD": ("BTCUSDT", 1.0), "PEPE-USD": ("1000PEPEUSDT", 1000.0)}
    src._pairs = {"BTCUSDT": "BTC-USD", "1000PEPEUSDT": "PEPE-USD"}
    ticks, emit = collect()
    src._handle({"topic": "recentlyTrade.H.BTCUSDT", "type": "snapshot", "data": [{"T": 1791674146952, "s": "BTCUSDT", "S": "Buy", "v": "0.29", "p": "83006.8"}]}, emit)
    src._handle({"topic": "recentlyTrade.H.BTCUSDT", "type": "delta", "data": [{"T": 1791674155952, "s": "BTCUSDT", "S": "Buy", "v": "0.29", "p": "83004.9"}]}, emit)
    src._handle({"topic": "instrumentInfo.H.BTCUSDT", "type": "snapshot", "data": {"symbol": "BTCUSDT", "lastPrice": "83006.8", "markPrice": "83006.8"}}, emit)
    src._handle({"topic": "instrumentInfo.H.BTCUSDT", "type": "delta", "data": {"symbol": "BTCUSDT", "markPrice": "83005.41"}}, emit)
    src._handle({"topic": "recentlyTrade.H.1000PEPEUSDT", "type": "delta", "data": [{"T": 1791674155952, "s": "1000PEPEUSDT", "S": "Sell", "v": "3", "p": "0.004"}]}, emit)
    src._handle({"success": False, "ret_msg": "bad topic", "request": {"op": "subscribe", "args": ["recentlyTrade.H.NOPEUSDT"]}}, emit)
    src._handle({"op": "ping", "args": ["1791674168937"]}, emit)
    assert len(ticks) == 3                                                        # the snapshot replay and the mark-only delta are dropped
    t, q, p = ticks
    assert t.price == 83004.9 and t.size == 0.29 and t.taker_buy is True and t.ts == 1791674155.952 and not t.quote
    assert q.quote and q.price == 83006.8 and q.bid is None and q.ask is None
    assert p.symbol == "PEPE-USD" and abs(p.price - 4e-6) < 1e-15 and p.size == 3000.0 and p.taker_buy is False
    assert "bad topic" in src.note
