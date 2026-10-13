"""Bitget, KuCoin, Gate.io and MEXC market-data sources: symbol mapping both ways, trade and
quote messages, candle paging (each venue's own row order), the unknown-name path, the product
lists, and MEXC's hand-decoded protobuf frames. No network."""
import asyncio
import json
import time

import pytest

from steel.config import Config
from steel.sources import bitget as bitget_mod, gateio as gateio_mod, kucoin as kucoin_mod, mexc as mexc_mod
from steel.sources.bitget import BitgetSource
from steel.sources.gateio import GateIOSource
from steel.sources.kucoin import KuCoinSource
from steel.sources.mexc import MEXCSource, unwrap

T0 = 1791676800                                    # 2026-10-11 00:00:00 UTC, a bucket start


def cfg(**kw):
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def collect():
    out = []
    return out, out.append


class FakeClient:
    """Answers each GET from `routes` ({url substring: (status, body) or [(status, body), ...] consumed in
    order}) and records the calls; usable as an async context manager in place of `client()`."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        for key, resp in self.routes.items():
            if key in url:
                status, body = resp.pop(0) if isinstance(resp, list) and len(resp) > 1 else (resp[0] if isinstance(resp, list) else resp)
                break
        else:
            status, body = 404, {}

        class R:
            status_code = status
            text = json.dumps(body)
            headers = {}
            def json(self):
                return body
        return R()

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


def test_ids_priorities_and_pairs_both_ways():
    for cls, pid, prio, pair in ((BitgetSource, "bitget", 52, "DOGEUSDT"), (KuCoinSource, "kucoin", 54, "DOGE-USDT"),
                                 (GateIOSource, "gateio", 56, "DOGE_USDT"), (MEXCSource, "mexc", 58, "DOGEUSDT")):
        assert cls.id == pid and cls.priority == prio and cls.venue == "spot" and cls.mechanism == "websocket"
        assert cls.fresh_after == 150.0 and not cls.default_on and cls.URL.startswith("https://")
        assert cls.pair("DOGE-USD") == pair and cls.pair("doge") == pair
        src = cls(cfg(), ["DOGE-USD", "BTC-USD"])
        assert src._pairs == {pair: "DOGE-USD", cls.pair("BTC-USD"): "BTC-USD"}      # the reverse map the stream handler uses
        assert src.knows("BTC-USD") and src.knows("NOPE-USD")                        # no list yet: anything canonical
        src.products = {"BTC-USD"}
        assert src.knows("BTC-USD") and not src.knows("NOPE-USD")
        src._unsupported.add("BTC-USD")
        assert not src.supports("BTC-USD")


# Bitget -------------------------------------------------------------------------------

def test_bitget_messages():
    src = BitgetSource(cfg(), ["BTC-USD", "ETH-USD"])
    ticks, emit = collect()
    arg = {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"}
    src._handle({"action": "snapshot", "arg": arg, "data": [{"ts": "1791676700000", "price": "83000", "size": "1", "side": "buy", "tradeId": "0"}]}, emit)
    src._handle({"action": "update", "arg": arg, "data": [{"ts": "1791676801500", "price": "83064.48", "size": "0.033491", "side": "buy", "tradeId": "1"},
                                                          {"ts": "1791676802000", "price": "83064.47", "size": "0.01", "side": "sell", "tradeId": "2"}]}, emit)
    src._handle({"action": "snapshot", "arg": {"instType": "SPOT", "channel": "ticker", "instId": "ETHUSDT"},
                 "data": [{"instId": "ETHUSDT", "lastPr": "2506.35", "bidPr": "2506.35", "askPr": "2506.36", "ts": "1791676803000"}]}, emit)
    src._handle({"event": "subscribe", "arg": arg}, emit)
    src._handle({"action": "update", "arg": {"instType": "SPOT", "channel": "trade", "instId": "SOLUSDT"}, "data": [{"price": "1", "size": "1", "side": "buy"}]}, emit)
    assert len(ticks) == 3                                                           # the trade snapshot and the unsubscribed name are dropped
    t0, t1, q = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83064.48 and t0.size == 0.033491 and t0.taker_buy is True and t0.ts == 1791676801.5 and not t0.quote
    assert t1.taker_buy is False and t1.ts == 1791676802.0
    assert q.symbol == "ETH-USD" and q.quote and q.price == 2506.35 and q.bid == 2506.35 and q.ask == 2506.36 and q.size == 0.0
    src._handle({"event": "error", "arg": {"instType": "SPOT", "channel": "trade", "instId": "ETHUSDT"}, "code": 30001,
                 "msg": "instType:SPOT,channel:trade,instId:ETHUSDT doesn't exist", "op": "subscribe"}, emit)
    assert not src.supports("ETH-USD") and "doesn't exist" in src.note


def test_bitget_candles_page_backwards_by_end_time():
    src = BitgetSource(cfg(), ["BTC-USD"])
    src.PER_REQUEST = 3

    def row(k):                                                                      # [ts(ms), o, h, l, c, baseVol, usdtVol, quoteVol], ascending
        return [str((T0 + 300 * k) * 1000), str(100 + k), str(110 + k), str(90 + k), str(105 + k), "2.5", "262", "262"]
    ok = {"code": "00000", "msg": "success"}
    c = FakeClient({"history-candles": [(200, ok | {"data": [row(3), row(4), row(5)]}), (200, ok | {"data": [row(0), row(1), row(2)]}), (200, ok | {"data": []})]})
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 1500))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900, T0 + 1200]        # [start, end), oldest first
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 91 and out[0].price == 106 and out[0].size == 2.5 and not out[0].quote
    assert [p["endTime"] for _, p in c.calls] == [(T0 + 1500) * 1000, (T0 + 900) * 1000 - 1]
    assert c.calls[0][1]["symbol"] == "BTCUSDT" and c.calls[0][1]["granularity"] == "5min" and c.calls[0][1]["limit"] == 3
    bad = FakeClient({"history-candles": (400, {"code": "40034", "msg": "Parameter NOPEUSDT does not exist", "data": None})})
    assert asyncio.run(src._candles(bad, "NOPE-USD", T0, T0 + 300)) == [] and not src.supports("NOPE-USD")


# KuCoin -------------------------------------------------------------------------------

def test_kucoin_messages():
    src = KuCoinSource(cfg(), ["BTC-USD", "ETH-USD"])
    ticks, emit = collect()
    src._handle({"id": "x", "type": "welcome"}, emit)
    src._handle({"id": "1", "type": "ack"}, emit)
    src._handle({"topic": "/market/match:BTC-USDT", "type": "message", "subject": "trade.l3match",
                 "data": {"makerOrderId": "m", "price": "83070.3", "sequence": "1", "side": "sell", "size": "0.00042026", "symbol": "BTC-USDT",
                          "takerOrderId": "t", "time": "1791676801500000000", "tradeId": "1", "type": "match"}}, emit)
    src._handle({"topic": "/market/match:ETH-USDT", "type": "message", "subject": "trade.l3match",
                 "data": {"price": "2508.7", "side": "buy", "size": "0.0479543", "symbol": "ETH-USDT", "time": "1791676802000000000"}}, emit)
    src._handle({"topic": "/market/ticker:ETH-USDT", "type": "message", "subject": "trade.ticker",
                 "data": {"bestAsk": "2508.71", "bestAskSize": "4.4", "bestBid": "2508.7", "bestBidSize": "1.5", "price": "2508.71", "sequence": "2", "size": "0.0001", "time": 1791676803000}}, emit)
    src._handle({"topic": "/market/match:SOL-USDT", "type": "message", "data": {"price": "1", "side": "buy", "size": "1", "symbol": "SOL-USDT", "time": "1"}}, emit)
    src._handle({"id": "9", "type": "error", "code": 404, "data": "topic /market/nope is not found"}, emit)
    assert len(ticks) == 3
    t0, t1, q = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83070.3 and t0.size == 0.00042026 and t0.taker_buy is False and t0.ts == 1791676801.5   # nanoseconds in
    assert t1.symbol == "ETH-USD" and t1.taker_buy is True and t1.ts == 1791676802.0 and not t1.quote
    assert q.symbol == "ETH-USD" and q.quote and q.price == 2508.71 and q.bid == 2508.7 and q.ask == 2508.71
    assert "not found" in src.note


def test_kucoin_candles_close_before_high_and_low():
    src = KuCoinSource(cfg(), ["BTC-USD"])
    src.PER_REQUEST = 3

    def row(k):                                                                      # [time(s), open, CLOSE, HIGH, LOW, volume, turnover]
        return [str(T0 + 300 * k), str(100 + k), str(105 + k), str(110 + k), str(90 + k), "6.1", "500000"]
    c = FakeClient({"market/candles": [(200, {"code": "200000", "data": [row(5), row(4), row(3)]}), (200, {"code": "200000", "data": [row(2), row(1), row(0)]})]})
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 1500))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900, T0 + 1200]
    assert out[0].o == 101 and out[0].price == 106 and out[0].h == 111 and out[0].l == 91 and out[0].size == 6.1
    assert [(p["startAt"], p["endAt"]) for _, p in c.calls] == [(T0 + 300, T0 + 1500), (T0 + 300, T0 + 900 - 1)]
    assert c.calls[0][1]["symbol"] == "BTC-USDT" and c.calls[0][1]["type"] == "5min"
    bad = FakeClient({"market/candles": (200, {"msg": "Unsupported trading pair.", "code": "400100"})})   # HTTP 200, the code says no
    assert asyncio.run(src._candles(bad, "NOPE-USD", T0, T0 + 300)) == [] and not src.supports("NOPE-USD")


# Gate.io ------------------------------------------------------------------------------

def test_gateio_messages():
    src = GateIOSource(cfg(), ["BTC-USD", "ETH-USD"])
    ticks, emit = collect()
    src._handle({"time": 1791676800, "channel": "spot.trades", "event": "subscribe", "payload": ["BTC_USDT"], "result": {"status": "success"}}, emit)
    src._handle({"time": 1791676800, "channel": "spot.pong", "event": "", "result": None}, emit)
    src._handle({"time": 1791676801, "time_ms": 1791676801233, "channel": "spot.trades", "event": "update",
                 "result": {"id": 222467776, "create_time": 1791676801, "create_time_ms": "1791676801232.984000", "side": "sell", "currency_pair": "BTC_USDT",
                            "amount": "0.041025", "price": "83060", "range": "1-1"}}, emit)
    src._handle({"channel": "spot.trades", "event": "update", "result": [{"create_time_ms": "1791676802000", "side": "buy", "currency_pair": "ETH_USDT", "amount": "0.5", "price": "2506"}]}, emit)
    src._handle({"time": 1791676803, "channel": "spot.book_ticker", "event": "update",
                 "result": {"t": 1791676803839, "u": 1, "s": "BTC_USDT", "b": "83060", "B": "0.1", "a": "83060.1", "A": "0.06"}}, emit)
    src._handle({"channel": "spot.trades", "event": "update", "result": {"side": "buy", "currency_pair": "SOL_USDT", "amount": "1", "price": "1", "create_time_ms": "1"}}, emit)
    assert len(ticks) == 3
    t0, t1, q = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83060.0 and t0.size == 0.041025 and t0.taker_buy is False and abs(t0.ts - 1791676801.233) < 1e-3
    assert t1.symbol == "ETH-USD" and t1.taker_buy is True and t1.ts == 1791676802.0            # a list result works too
    assert q.quote and q.bid == 83060.0 and q.ask == 83060.1 and q.price == pytest.approx(83060.05)
    src._handle({"channel": "spot.trades", "event": "subscribe", "error": {"code": 2, "message": "invalid argument: NOPE_USDT"}, "result": None}, emit)
    assert "NOPE_USDT" in src.note and len(ticks) == 3


def test_gateio_candles_row_order_and_forward_windows():
    src = GateIOSource(cfg(), ["BTC-USD"])
    src.PER_REQUEST = 3

    def row(k, closed="true"):                                                       # [t, QUOTE_VOL, CLOSE, high, low, OPEN, base_vol, closed]
        return [str(T0 + 300 * k), "272162.46", str(105 + k), str(110 + k), str(90 + k), str(100 + k), "3.2764", closed]
    c = FakeClient({"candlesticks": [(200, [row(1), row(2), row(3)]), (200, [row(4), row(5, "false")])]})
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 1800))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900, T0 + 1200, T0 + 1500]
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 91 and out[0].price == 106 and out[0].size == 3.2764
    assert [(p["from"], p["to"]) for _, p in c.calls] == [(T0 + 300, T0 + 1200 - 1), (T0 + 1200, T0 + 1800 - 1)]   # 3-bar windows, no `limit`
    assert c.calls[0][1]["currency_pair"] == "BTC_USDT" and c.calls[0][1]["interval"] == "5m" and "limit" not in c.calls[0][1]
    bad = FakeClient({"candlesticks": (400, {"label": "INVALID_CURRENCY_PAIR", "message": "Invalid currency pair NOPE_USDT"})})
    assert asyncio.run(src._candles(bad, "NOPE-USD", T0, T0 + 300)) == [] and not src.supports("NOPE-USD")


# MEXC ---------------------------------------------------------------------------------

def pb_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_bytes(field, payload):
    return pb_varint(field << 3 | 2) + pb_varint(len(payload)) + payload


def pb_int(field, n):
    return pb_varint(field << 3) + pb_varint(n)


def deals_frame(symbol, deals):
    items = b"".join(pb_bytes(1, pb_bytes(1, p.encode()) + pb_bytes(2, q.encode()) + pb_int(3, side) + pb_int(4, ms) + pb_bytes(5, b"id")) for p, q, side, ms in deals)
    return pb_bytes(1, f"spot@public.aggre.deals.v3.api.pb@100ms@{symbol}".encode()) + pb_bytes(3, symbol.encode()) + pb_int(6, 1791676805000) \
        + pb_bytes(314, items + pb_bytes(2, b"spot@public.aggre.deals.v3.api.pb@100ms"))


def book_frame(symbol, bid, ask):
    body = pb_bytes(1, bid.encode()) + pb_bytes(2, b"1.5") + pb_bytes(3, ask.encode()) + pb_bytes(4, b"2.5") + pb_bytes(5, b"7") + pb_int(6, 1791676805000)
    return pb_bytes(1, f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{symbol}".encode()) + pb_bytes(3, symbol.encode()) + pb_int(6, 1791676805000) + pb_bytes(315, body)


def test_mexc_protobuf_frames_decode_by_hand():
    m = unwrap(deals_frame("BTCUSDT", [("83049.86", "0.0004593", 1, 1791676801500), ("83049.85", "0.2", 2, 1791676802000)]))
    assert m["symbol"] == "BTCUSDT" and m["channel"].endswith("@BTCUSDT")
    assert m["deals"] == [{"price": "83049.86", "qty": "0.0004593", "buy": True, "ts": 1791676801.5}, {"price": "83049.85", "qty": "0.2", "buy": False, "ts": 1791676802.0}]
    b = unwrap(book_frame("ETHUSDT", "2506.43", "2506.44"))
    assert b["symbol"] == "ETHUSDT" and b["bid"] == "2506.43" and b["ask"] == "2506.44" and "deals" not in b
    other = pb_bytes(1, b"spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDT") + pb_bytes(3, b"BTCUSDT") + pb_bytes(313, pb_bytes(1, b"x"))
    assert unwrap(other) is None and unwrap(b"\xff\xff") is None and unwrap(b"") is None          # another body, a truncated varint, nothing
    assert MEXCSource._decode('{"id":0,"code":0,"msg":"PONG"}') == {"id": 0, "code": 0, "msg": "PONG"}


def test_mexc_messages_and_quote_poll(monkeypatch):
    src = MEXCSource(cfg(), ["BTC-USD", "ETH-USD"])
    ticks, emit = collect()
    src._handle(src._decode(deals_frame("BTCUSDT", [("83049.86", "0.0004593", 1, 1791676801500), ("83049.85", "0.2", 2, 1791676802000)])), emit)
    src._handle(src._decode(book_frame("ETHUSDT", "2506.43", "2506.44")), emit)
    src._handle(src._decode(deals_frame("SOLUSDT", [("110", "1", 1, 1791676803000)])), emit)       # not subscribed
    src._handle({"id": 0, "code": 0, "msg": "PONG"}, emit)
    src._handle({"id": 0, "code": 0, "msg": "Not Subscribed successfully! [spot@public.deals.v3.api@BTCUSDT].  Reason： Blocked! "}, emit)
    assert len(ticks) == 3
    t0, t1, q = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 83049.86 and t0.size == 0.0004593 and t0.taker_buy is True and t0.ts == 1791676801.5 and not t0.quote
    assert t1.taker_buy is False and t1.price == 83049.85
    assert q.symbol == "ETH-USD" and q.quote and q.bid == 2506.43 and q.ask == 2506.44 and q.price == pytest.approx(2506.435)
    assert "Blocked" in src.note
    rows = [{"symbol": "BTCUSDT", "bidPrice": "83065.25", "bidQty": "5.5", "askPrice": "83065.26", "askQty": "4.3"},
            {"symbol": "NOPEUSDT", "bidPrice": "1", "bidQty": "1", "askPrice": "2", "askQty": "1"}]
    monkeypatch.setattr(mexc_mod, "client", lambda *a, **k: FakeClient({"bookTicker": (200, rows)}))
    ticks.clear()
    asyncio.run(src.poll_once(emit))
    assert len(ticks) == 1 and ticks[0].symbol == "BTC-USD" and ticks[0].quote and ticks[0].bid == 83065.25 and ticks[0].ask == 83065.26 and ticks[0].size == 0


def test_mexc_klines_are_binance_shaped():
    src = MEXCSource(cfg(), ["BTC-USD"])
    rows = [[(T0 + 300 * k) * 1000, str(100 + k), str(110 + k), str(90 + k), str(105 + k), "23.4", (T0 + 300 * k + 299) * 1000, "1945321.97"] for k in range(4)]
    c = FakeClient({"klines": (200, rows)})
    out = asyncio.run(src._klines(c, "BTC-USD", T0 + 300, T0 + 900))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600] and out[0].o == 101 and out[0].h == 111 and out[0].l == 91 and out[0].price == 106 and out[0].size == 23.4
    assert c.calls[0][1]["symbol"] == "BTCUSDT" and c.calls[0][1]["interval"] == "5m" and c.calls[0][1]["startTime"] == (T0 + 300) * 1000 and c.calls[0][1]["limit"] == 1000
    bad = FakeClient({"klines": (400, {"msg": "Invalid symbol.", "code": -1121, "_extend": None})})
    assert asyncio.run(src._klines(bad, "NOPE-USD", T0, T0 + 300)) == [] and not src.supports("NOPE-USD")


# Product lists and the backfill glue ------------------------------------------------------

@pytest.mark.parametrize("mod, cls, route, listing", [
    (bitget_mod, BitgetSource, "public/symbols", {"code": "00000", "data": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "online"},
                                                                          {"symbol": "ETHUSDT", "baseCoin": "ETH", "quoteCoin": "USDT", "status": "offline"},
                                                                          {"symbol": "ETHBTC", "baseCoin": "ETH", "quoteCoin": "BTC", "status": "online"}]}),
    (kucoin_mod, KuCoinSource, "v2/symbols", {"code": "200000", "data": [{"symbol": "BTC-USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT", "enableTrading": True},
                                                                       {"symbol": "ETH-USDT", "baseCurrency": "ETH", "quoteCurrency": "USDT", "enableTrading": False},
                                                                       {"symbol": "ETH-BTC", "baseCurrency": "ETH", "quoteCurrency": "BTC", "enableTrading": True}]}),
    (gateio_mod, GateIOSource, "currency_pairs", [{"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "trade_status": "tradable"},
                                                  {"id": "ETH_USDT", "base": "ETH", "quote": "USDT", "trade_status": "untradable"},
                                                  {"id": "ETH_BTC", "base": "ETH", "quote": "BTC", "trade_status": "tradable"}]),
    (mexc_mod, MEXCSource, "exchangeInfo", {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "1", "isSpotTradingAllowed": True},
                                                        {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "2", "isSpotTradingAllowed": True},
                                                        {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC", "status": "1", "isSpotTradingAllowed": True}]}),
])
def test_product_lists_gate_the_backfill(monkeypatch, mod, cls, route, listing):
    src = cls(cfg(), ["BTC-USD", "ETH-USD", "SOL-USD"])
    fake = FakeClient({route: (200, listing), "candle": (200, {"code": "00000", "data": []} if cls is BitgetSource else {"code": "200000", "data": []} if cls is KuCoinSource else []),
                       "klines": (200, [])})
    monkeypatch.setattr(mod, "client", lambda *a, **k: fake)
    assert asyncio.run(src.backfill(["BTC-USD", "ETH-USD", "SOL-USD"], time.time() - 600)) == []
    assert src.products == {"BTC-USD"} and src.knows("BTC-USD") and not src.knows("ETH-USD")       # only online USDT pairs
    assert "ETH" in src.note and "SOL" in src.note and cls.name in src.note                           # the names the venue does not list
    assert len([u for u, _ in fake.calls if route in u]) == 1 and len([u for u, _ in fake.calls if route not in u]) == 1   # one listing, one candle fetch (BTC)
    assert asyncio.run(src.backfill_range(["ETH-USD"], T0, T0 + 300)) == [] and len(fake.calls) == 2                       # not listed: not even asked
