"""HTX, Crypto.com, Bitfinex, Gemini and Bitstamp sources: symbol mapping both ways, trade and
quote messages, candle paging with fake clients, product lists. No network."""
import asyncio
import gzip
import json
import time

from steel.config import Config
from steel.sources.bitfinex import BitfinexSource
from steel.sources.bitstamp import BitstampSource
from steel.sources.cryptocom import CryptoComSource
from steel.sources.gemini import GeminiSource
from steel.sources.htx import HTXSource

T0 = 1791676800                                   # 2026-10-11T00:00:00Z


def cfg(**kw):
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def collect():
    out = []
    return out, out.append


class R:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeClient:
    """`respond(url, params) -> R`; every call is recorded."""
    def __init__(self, respond):
        self.respond, self.calls = respond, []

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.respond(url, dict(params or {}))


# HTX ------------------------------------------------------------------------------

def test_htx_pairs_decode_and_messages():
    src = HTXSource(cfg(), ["BTC-USD", "ETH-USD"])
    assert HTXSource.pair("BTC-USD") == "btcusdt" and src.priority == 60 and src.venue == "spot" and not src.default_on
    assert HTXSource.decode(gzip.compress(b'{"ping": 1791676801000}')) == {"ping": 1791676801000}
    assert HTXSource.reply({"ping": 1791676801000}) == '{"pong": 1791676801000}' and HTXSource.reply({"ch": "x"}) is None
    assert HTXSource.decode(b"not gzip") is None
    src._pairs = {"btcusdt": "BTC-USD", "ethusdt": "ETH-USD"}
    ticks, emit = collect()
    src._handle({"ch": "market.btcusdt.trade.detail", "ts": 1791676801432, "tick": {"id": 1, "ts": 1791676801430, "data": [
        {"id": 1, "ts": 1791676801430, "tradeId": 7, "amount": 0.0025, "price": 83045.07, "direction": "buy"},
        {"id": 2, "ts": 1791676801431, "tradeId": 8, "amount": 0.5, "price": 83045.0, "direction": "sell"}]}}, emit)
    src._handle({"ch": "market.ethusdt.bbo", "ts": 1791676802150, "tick": {"seqId": 9, "ask": 2506.6, "askSize": 1.0, "bid": 2506.4,
                                                                           "bidSize": 2.0, "quoteTime": 1791676802148, "symbol": "ethusdt"}}, emit)
    src._handle({"ch": "market.solusdt.trade.detail", "tick": {"data": [{"ts": 1, "amount": 1, "price": 150, "direction": "buy"}]}}, emit)   # not subscribed
    src._handle({"id": "t-x", "status": "error", "err-code": "bad-request", "err-msg": "invalid topic"}, emit)
    assert len(ticks) == 3
    t0, t1, t2 = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83045.07 and t0.size == 0.0025 and t0.taker_buy is True and not t0.quote
    assert abs(t0.ts - 1791676801.43) < 1e-6 and t1.taker_buy is False and t1.size == 0.5
    assert t2.symbol == "ETH-USD" and t2.quote and t2.bid == 2506.4 and t2.ask == 2506.6 and t2.price == 2506.5 and t2.size == 0.0
    assert "invalid topic" in src.note
    src._replay = {"ethusdt"}                                                                 # after a (re)subscribe
    old = {"ch": "market.ethusdt.trade.detail", "tick": {"data": [{"ts": 1791590400000, "amount": 1, "price": 2500, "direction": "buy"}]}}
    src._handle(old, emit)                                                                    # the replay of stale trades is dropped
    assert len(ticks) == 3 and src._replay == set()
    src._handle(old, emit)                                                                    # the next message is live data
    assert len(ticks) == 4
    src._replay = {"ethusdt"}
    src._handle({"ch": "market.ethusdt.trade.detail", "tick": {"data": [{"ts": int(time.time() * 1000), "amount": 1, "price": 2500, "direction": "buy"}]}}, emit)
    assert len(ticks) == 5                                                                    # a replay of fresh trades is kept


def test_htx_klines_newest_first_sized_to_the_cutoff():
    src = HTXSource(cfg(), ["BTC-USD"])
    rows = [{"id": T0 + 300 * k, "open": 100 + k, "close": 105 + k, "low": 99 + k, "high": 110 + k, "amount": 2.5, "vol": 250.0, "count": 4}
            for k in range(6)][::-1]
    c = FakeClient(lambda url, p: R(200, {"ch": "market.btcusdt.kline.5min", "status": "ok", "data": rows}))
    out = asyncio.run(src._klines(c, "BTC-USD", T0 - 7200, T0 + 300 * 4))
    assert c.calls[0][1] == {"symbol": "btcusdt", "period": "5min", "size": 26 + 4}      # (2 h + 4 bars) / 5 min + 2
    assert [t.ts for t in out] == [T0, T0 + 300, T0 + 600, T0 + 900]                      # oldest first, [cutoff, until)
    assert out[1].o == 101 and out[1].h == 111 and out[1].l == 100 and out[1].price == 106 and out[1].size == 2.5
    far = asyncio.run(src._klines(FakeClient(lambda u, p: R(200, {"status": "ok", "data": []})), "BTC-USD", 0, T0))
    assert far == []
    assert c.calls and src._klines                                                        # the size never passes the venue's cap
    big = FakeClient(lambda u, p: R(200, {"status": "ok", "data": []}))
    asyncio.run(src._klines(big, "BTC-USD", 0, T0))
    assert big.calls[0][1]["size"] == 2000


def test_htx_unknown_symbol_and_product_list():
    src = HTXSource(cfg(), ["BTC-USD"])
    bad = FakeClient(lambda u, p: R(200, {"ts": 1, "status": "error", "err-code": "invalid-parameter", "err-msg": "invalid symbol"}))
    assert asyncio.run(src._klines(bad, "NOPE-USD", 0, T0)) == [] and not src.supports("NOPE-USD")
    listing = [{"sc": "btcusdt", "bc": "btc", "qc": "usdt", "state": "online", "te": True},
               {"sc": "ethbtc", "bc": "eth", "qc": "btc", "state": "online", "te": True},
               {"sc": "xyzusdt", "bc": "xyz", "qc": "usdt", "state": "offline", "te": False},
               {"sc": "solusdt", "bc": "sol", "qc": "usdt", "state": "online", "te": True}]
    products = asyncio.run(src.list_products(FakeClient(lambda u, p: R(200, {"status": "ok", "data": listing}))))
    assert products == {"BTC-USD", "SOL-USD"} and src.knows("BTC-USD") and not src.knows("ETH-USD") and not src.knows("XYZ-USD")


# Crypto.com -----------------------------------------------------------------------

def test_cryptocom_instruments_prefer_usd_over_usdt():
    src = CryptoComSource(cfg(), ["BTC-USD", "SOL-USD", "ETH-USD"])
    assert CryptoComSource.inst("BTC-USD") == "BTC_USD" and src.priority == 62
    rows = [{"symbol": "BTC_USDT", "inst_type": "CCY_PAIR", "tradable": True, "quote_ccy": "USDT", "base_ccy": "BTC"},
            {"symbol": "BTC_USD", "inst_type": "CCY_PAIR", "tradable": True, "quote_ccy": "USD", "base_ccy": "BTC"},
            {"symbol": "SOL_USDT", "inst_type": "CCY_PAIR", "tradable": True, "quote_ccy": "USDT", "base_ccy": "SOL"},
            {"symbol": "SOL_EUR", "inst_type": "CCY_PAIR", "tradable": True, "quote_ccy": "EUR", "base_ccy": "SOL"},
            {"symbol": "ETHUSD-PERP", "inst_type": "PERPETUAL_SWAP", "tradable": True, "quote_ccy": "USD", "base_ccy": "ETH"},
            {"symbol": "DOGE_USD", "inst_type": "CCY_PAIR", "tradable": False, "quote_ccy": "USD", "base_ccy": "DOGE"}]
    names = asyncio.run(src.list_instruments(FakeClient(lambda u, p: R(200, {"code": 0, "result": {"data": rows}}))))
    assert names == {"BTC-USD": "BTC_USD", "SOL-USD": "SOL_USDT"}
    assert src.inst_for("SOL-USD") == "SOL_USDT" and src.knows("BTC-USD") and not src.knows("ETH-USD") and not src.knows("DOGE-USD")


def test_cryptocom_messages_heartbeat_and_the_ack_snapshot():
    src = CryptoComSource(cfg(), ["BTC-USD"])
    src._insts = {"BTC_USD": "BTC-USD"}
    ticks, emit = collect()
    trade = {"d": "1791676801564737591", "t": 1791676801564, "p": "82992.49", "q": "0.00237", "s": "BUY", "i": "BTC_USD"}
    src._handle({"id": 1, "method": "subscribe", "code": 0, "result": {"instrument_name": "BTC_USD", "subscription": "trade.BTC_USD",
                                                                      "channel": "trade", "data": [trade]}}, emit)                    # the ack replays history
    assert ticks == []
    src._handle({"id": -1, "method": "subscribe", "code": 0, "result": {"instrument_name": "BTC_USD", "subscription": "trade.BTC_USD",
                                                                       "channel": "trade", "data": [trade, dict(trade, s="SELL", q="1.5")]}}, emit)
    src._handle({"id": -1, "method": "subscribe", "code": 0, "result": {"instrument_name": "BTC_USD", "subscription": "ticker.BTC_USD",
                                                                       "channel": "ticker", "data": [{"a": "82992.49", "b": "82992.48", "k": "82992.50",
                                                                                                      "i": "BTC_USD", "t": 1791676802040}]}}, emit)
    src._handle({"id": 2, "method": "subscribe", "code": 40003, "message": "INVALID_REQUEST"}, emit)
    assert len(ticks) == 3
    t0, t1, t2 = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 82992.49 and t0.size == 0.00237 and t0.taker_buy is True and abs(t0.ts - 1791676801.564) < 1e-6
    assert t1.taker_buy is False and t1.size == 1.5
    assert t2.quote and t2.price == 82992.49 and t2.bid == 82992.48 and t2.ask == 82992.50 and abs(t2.ts - 1791676802.04) < 1e-6
    assert "INVALID_REQUEST" in src.note
    assert json.loads(CryptoComSource.reply({"id": 1791676803000, "method": "public/heartbeat", "code": 0})) == \
        {"id": 1791676803000, "method": "public/respond-heartbeat"}
    assert CryptoComSource.reply({"method": "subscribe"}) is None


def test_cryptocom_candles_page_backwards_inside_the_window():
    src = CryptoComSource(cfg(), ["BTC-USD"])
    n = 400
    allrows = [{"o": str(100 + k), "h": str(110 + k), "l": str(99 + k), "c": str(105 + k), "v": "3.1", "t": (T0 + 300 * k) * 1000} for k in range(n)]

    def respond(url, p):                                  # the newest `count` inside [start_ts, end_ts], oldest first
        assert p["instrument_name"] == "BTC_USD" and p["timeframe"] == "M5"
        inside = [r for r in allrows if p["start_ts"] <= r["t"] <= p["end_ts"]]
        return R(200, {"code": 0, "result": {"interval": "M5", "data": inside[-int(p["count"]):]}})
    c = FakeClient(respond)
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 300 * (n - 1)))
    assert len(c.calls) == 2 and c.calls[1][1]["end_ts"] < c.calls[0][1]["end_ts"]
    assert [t.ts for t in out] == [T0 + 300 * k for k in range(1, n - 1)]                 # oldest first, no gaps, no repeats, [cutoff, until)
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 100 and out[0].price == 106 and out[0].size == 3.1
    bad = FakeClient(lambda u, p: R(400, {"code": 40004, "message": "Invalid instrument_name"}))
    assert asyncio.run(src._candles(bad, "NOPE-USD", 0, T0)) == [] and not src.supports("NOPE-USD")


# Bitfinex -------------------------------------------------------------------------

def test_bitfinex_pairs_aliases_and_test_pairs():
    src = BitfinexSource(cfg(), ["BTC-USD", "AAVE-USD"])
    assert BitfinexSource.pair("BTC-USD") == "tBTCUSD" and BitfinexSource.pair("AAVE-USD") == "tAAVE:USD" and src.priority == 64
    pairs = [["BTCUSD", "BTCUST", "AAVE:USD", "SOLUST", "TESTBTC:TESTUSD", "ETHBTC", "DSHUSD", "USTUSD"]]
    symmap = [[["DSH", "DASH"], ["UST", "USDt"], ["LES", "LEO-Vaulta"]]]
    c = FakeClient(lambda u, p: R(200, symmap if "map:currency" in u else pairs))
    names = asyncio.run(src.list_pairs(c))
    assert names == {"BTC-USD": "tBTCUSD", "AAVE-USD": "tAAVE:USD", "SOL-USD": "tSOLUST", "DASH-USD": "tDSHUSD", "USDT-USD": "tUSTUSD"}
    assert src.pair_for("SOL-USD") == "tSOLUST" and src.knows("DASH-USD") and not src.knows("ETH-USD") and not src.knows("TESTBTC-USD")


def test_bitfinex_array_messages_by_channel_id():
    src = BitfinexSource(cfg(), ["BTC-USD"])
    src._pairs = {"tBTCUSD": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"event": "info", "version": 2}, emit)
    src._handle({"event": "subscribed", "channel": "trades", "chanId": 225006, "symbol": "tBTCUSD", "pair": "BTCUSD"}, emit)
    src._handle({"event": "subscribed", "channel": "ticker", "chanId": 479186, "symbol": "tBTCUSD", "pair": "BTCUSD"}, emit)
    src._handle([225006, [[1991662365, 1791676801000, 0.00247957, 83107], [1991662364, 1791676800000, -0.00018921, 83089]]], emit)   # snapshot: history
    src._handle([225006, "te", [1991662368, 1791676802686, -0.00256199, 83107]], emit)
    src._handle([225006, "tu", [1991662368, 1791676802686, -0.00256199, 83107]], emit)   # the same trade, id settled
    src._handle([225006, "te", [1991662369, 1791676803000, 0.5, 83108]], emit)
    src._handle([225006, "hb"], emit)
    src._handle([479186, [83089, 1.40109985, 83107, 1.54496604, 399, 0.0048242, 83107, 84.0283989, 83202, 82548]], emit)
    src._handle([999, "te", [1, 1791676803000, 1, 1]], emit)                              # unknown channel
    src._handle({"event": "error", "msg": "subscribe: limit", "code": 10305}, emit)
    assert len(ticks) == 3
    t0, t1, t2 = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83107 and t0.size == 0.00256199 and t0.taker_buy is False and abs(t0.ts - 1791676802.686) < 1e-6
    assert t1.taker_buy is True and t1.size == 0.5
    assert t2.quote and t2.bid == 83089 and t2.ask == 83107 and t2.price == 83107
    assert "limit" in src.note
    assert src._chans == {225006: ("trades", "BTC-USD"), 479186: ("ticker", "BTC-USD")}


def test_bitfinex_candle_rows_are_open_close_high_low():
    src = BitfinexSource(cfg(), ["BTC-USD"])
    src.REQUEST_GAP = 0.0
    rows = [[(T0 + 300 * k) * 1000, 100 + k, 105 + k, 110 + k, 99 + k, 0.94] for k in range(6)]     # [MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]
    c = FakeClient(lambda u, p: R(200, [r for r in rows if p["start"] <= r[0] <= p["end"]]))
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 300 * 4))
    assert "candles/trade:5m:tBTCUSD/hist" in c.calls[0][0] and c.calls[0][1]["sort"] == 1 and c.calls[0][1]["limit"] == 10000
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900]
    assert out[0].o == 101 and out[0].price == 106 and out[0].h == 111 and out[0].l == 100 and out[0].size == 0.94
    bad = FakeClient(lambda u, p: R(200, ["error", 10020, "symbol: invalid"]))
    assert asyncio.run(src._candles(bad, "NOPE-USD", 0, T0)) == [] and not src.supports("NOPE-USD")


# Gemini ---------------------------------------------------------------------------

def test_gemini_symbol_list_keeps_only_plain_usd_pairs():
    assert GeminiSource.pair("BTC-USD") == "btcusd"
    syms = ["btcusd", "btcgusd", "btcrlusd", "btcusdc", "paxgusd", "2zusd", "2zgusd", "2zrlusd", "ethgusdperp", "gusdusd", "usdgusd", "ethbtc"]
    assert GeminiSource.usd_pairs(syms) == {"BTC-USD", "PAXG-USD", "2Z-USD", "GUSD-USD", "USDG-USD"}
    src = GeminiSource(cfg(), ["BTC-USD", "ETH-USD"])
    assert src.priority == 66
    asyncio.run(src.list_products(FakeClient(lambda u, p: R(200, syms))))
    assert src.knows("BTC-USD") and not src.knows("ETH-USD")


def test_gemini_trades_and_a_top_of_book_from_l2():
    src = GeminiSource(cfg(), ["BTC-USD"])
    src._pairs = {"BTCUSD": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"type": "l2_updates", "symbol": "BTCUSD", "trades": [{"price": "1", "quantity": "1", "side": "buy", "timestamp": 1}], "auction_events": [],
                 "changes": [["buy", "82993.77", "0.24"], ["buy", "82994.10", "0.06"], ["buy", "82990.00", "1.0"],
                             ["sell", "83001.00", "0.5"], ["sell", "82999.50", "0.1"]]}, emit)
    assert len(ticks) == 1 and ticks[0].quote and ticks[0].bid == 82994.10 and ticks[0].ask == 82999.50     # best of each side; the backlog is not a trade
    src._handle({"type": "l2_updates", "symbol": "BTCUSD", "changes": [["buy", "82990.00", "0.5"]]}, emit)    # a deeper level: no new quote
    assert len(ticks) == 1
    src._handle({"type": "l2_updates", "symbol": "BTCUSD", "changes": [["buy", "82994.10", "0"]]}, emit)      # the best bid is gone
    assert len(ticks) == 2 and ticks[1].bid == 82993.77 and ticks[1].ask == 82999.50
    src._handle({"type": "trade", "symbol": "BTCUSD", "event_id": 1, "timestamp": 1791676801015, "price": "82993.78", "quantity": "0.00001181", "side": "buy", "tid": 2}, emit)
    src._handle({"type": "trade", "symbol": "BTCUSD", "timestamp": 1791676802000, "price": "82993.00", "quantity": "0.2", "side": "sell"}, emit)
    src._handle({"type": "heartbeat", "timestamp": 1791676803000}, emit)
    src._handle({"type": "trade", "symbol": "ETHUSD", "timestamp": 1, "price": "1", "quantity": "1", "side": "buy"}, emit)   # not subscribed
    assert len(ticks) == 4
    assert ticks[2].price == 82993.78 and ticks[2].size == 0.00001181 and ticks[2].taker_buy is True and abs(ticks[2].ts - 1791676801.015) < 1e-6
    assert ticks[3].taker_buy is False and not ticks[3].quote


def test_gemini_candles_newest_first_no_range():
    src = GeminiSource(cfg(), ["BTC-USD"])
    rows = [[(T0 + 300 * k) * 1000, 100 + k, 110 + k, 99 + k, 105 + k, 0.027] for k in range(6)][::-1]   # [time, open, high, low, close, volume]
    c = FakeClient(lambda u, p: R(200, rows))
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 300 * 4))
    assert c.calls[0][0].endswith("/v2/candles/btcusd/5m") and not c.calls[0][1]
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900]
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 100 and out[0].price == 106 and out[0].size == 0.027
    bad = FakeClient(lambda u, p: R(400, "Supplied value 'nopeusd' is not a valid symbol"))
    assert asyncio.run(src._candles(bad, "NOPE-USD", 0, T0)) == [] and not src.supports("NOPE-USD")


# Bitstamp -------------------------------------------------------------------------

def test_bitstamp_pairs_trades_and_the_ticker_poll():
    src = BitstampSource(cfg(), ["BTC-USD", "ETH-USD"])
    assert BitstampSource.pair("BTC-USD") == "btcusd" and src.priority == 68 and src.poll_interval >= 5
    listing = [{"name": "BTC/USD", "url_symbol": "btcusd", "trading": "Enabled"}, {"name": "ETH/EUR", "url_symbol": "etheur", "trading": "Enabled"},
               {"name": "XRP/USD", "url_symbol": "xrpusd", "trading": "Disabled"}]
    assert asyncio.run(src.list_pairs(FakeClient(lambda u, p: R(200, listing)))) == {"BTC-USD": "btcusd"}
    assert src.knows("BTC-USD") and not src.knows("ETH-USD") and not src.knows("XRP-USD")
    src._pairs = {"btcusd": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"data": {"id": 652735541, "timestamp": "1791676801", "amount": 6.083e-05, "amount_str": "0.00006083", "price": 82991.55,
                          "price_str": "82991.55", "type": 0, "microtimestamp": "1791676801639000"}, "channel": "live_trades_btcusd", "event": "trade"}, emit)
    src._handle({"data": {"id": 652735542, "timestamp": "1791676802", "amount": 0.5, "price": 82990.0, "type": 1, "microtimestamp": "1791676802000000"},
                 "channel": "live_trades_btcusd", "event": "trade"}, emit)
    src._handle({"data": {"id": 1, "timestamp": "1", "amount": 1, "price": 1, "type": 0}, "channel": "live_trades_ethusd", "event": "trade"}, emit)   # not subscribed
    src._handle({"event": "bts:subscription_succeeded", "channel": "live_trades_btcusd", "data": {}}, emit)
    src._handle({"event": "bts:request_reconnect", "channel": "", "data": ""}, emit)
    assert len(ticks) == 2
    assert ticks[0].price == 82991.55 and ticks[0].size == 6.083e-05 and ticks[0].taker_buy is True and abs(ticks[0].ts - 1791676801.639) < 1e-6
    assert ticks[1].taker_buy is False and ticks[1].size == 0.5 and not ticks[1].quote
    assert "reconnect" in src.note
    src._quotes([{"timestamp": "1791676803", "last": "82976.27", "bid": "82976.26", "ask": "82976.28", "pair": "BTC/USD", "market": "BTC/USD"},
                 {"timestamp": "1791676803", "last": "1", "bid": "1", "ask": "1", "pair": "BTC/EUR"},
                 {"timestamp": "1791676803", "last": "2500", "bid": "2499", "ask": "2501", "pair": "ETH/USD"}], emit)     # ETH is not in the pair map
    assert len(ticks) == 3 and ticks[2].quote and ticks[2].bid == 82976.26 and ticks[2].ask == 82976.28 and ticks[2].price == 82976.27 and ticks[2].ts == 1791676803.0


def test_bitstamp_ohlc_pages_forward_from_start():
    src = BitstampSource(cfg(), ["BTC-USD"])
    n = 1200
    allrows = [{"timestamp": str(T0 + 300 * k), "open": str(100 + k), "high": str(110 + k), "low": str(99 + k), "close": str(105 + k), "volume": "0.41"} for k in range(n)]

    def respond(url, p):
        assert p["step"] == 300 and "end" not in p and url.endswith("/ohlc/btcusd/")
        return R(200, {"data": {"pair": "BTC/USD", "ohlc": [r for r in allrows if int(r["timestamp"]) >= p["start"]][:int(p["limit"])]}})
    c = FakeClient(respond)
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 300 * (n - 1)))
    assert len(c.calls) == 2 and c.calls[1][1]["start"] == T0 + 300 * 1000 + 300
    assert [t.ts for t in out] == [T0 + 300 * k for k in range(1, n - 1)]
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 100 and out[0].price == 106 and out[0].size == 0.41
    bad = FakeClient(lambda u, p: R(404, {}))
    assert asyncio.run(src._candles(bad, "NOPE-USD", 0, T0)) == [] and not src.supports("NOPE-USD")
