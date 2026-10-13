"""Perp-venue market-data sources (Kraken Futures, dYdX, Deribit, Aster): symbol mapping both
ways, one trade and one quote through `_handle`, candle paging with a fake client, the scaled
contracts, `knows` and `_unsupported`. No network."""
import asyncio
import json

from steel.config import Config
from steel.sources.aster import AsterSource
from steel.sources.deribit import DeribitSource
from steel.sources.dydx import DydxSource
from steel.sources.krakenfutures import KrakenFuturesSource

T0 = 1791676800                                   # 2026-10-11T00:00:00Z, a bucket start


def cfg(**kw):
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def collect():
    out = []
    return out, out.append


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Client:
    """A fake httpx client: `pages` is a list of (body, status) served in order; every call is kept."""
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        body, status = self.pages.pop(0) if self.pages else ({}, 500)
        return Resp(body, status)


def test_the_four_are_perp_venues_off_by_default():
    for cls, prio in ((KrakenFuturesSource, 76), (DydxSource, 78), (DeribitSource, 82), (AsterSource, 84)):
        assert cls.venue == "perp" and cls.priority == prio and cls.mechanism == "websocket" and not cls.default_on
        assert cls.fresh_after == 150.0 and cls.URL.startswith("https://") and cls.id == cls.id.lower()


# Kraken Futures ---------------------------------------------------------------------

def test_krakenfutures_symbols_both_ways():
    assert KrakenFuturesSource.pair("BTC-USD") == "PF_XBTUSD" and KrakenFuturesSource.pair("DOGE-USD") == "PF_DOGEUSD"
    assert KrakenFuturesSource.pair("sol-usd") == "PF_SOLUSD"
    assert KrakenFuturesSource.unpair("PF_XBTUSD") == "BTC-USD" and KrakenFuturesSource.unpair("PF_SOLUSD") == "SOL-USD"
    assert KrakenFuturesSource.unpair("PI_XBTUSD") is None and KrakenFuturesSource.unpair("FI_ETHUSD_250926") is None
    src = KrakenFuturesSource(cfg(), ["BTC-USD", "SOL-USD"])
    assert src.knows("BTC-USD")                                   # no list yet: anything canonical
    src.products = {"PF_XBTUSD"}
    assert src.knows("BTC-USD") and not src.knows("SOL-USD")


def test_krakenfutures_messages():
    src = KrakenFuturesSource(cfg(), ["BTC-USD"])
    src._pairs = {"PF_XBTUSD": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"feed": "trade", "product_id": "PF_XBTUSD", "uid": "u", "side": "buy", "type": "fill", "seq": 1,
                 "time": 1791674075217, "qty": 0.0002, "price": 82981.0}, emit)
    src._handle({"feed": "trade", "product_id": "PF_XBTUSD", "side": "sell", "type": "liquidation", "time": 1791674075300, "qty": 1.5, "price": 82900.0}, emit)
    src._handle({"feed": "trade", "product_id": "PF_XBTUSD", "side": "sell", "type": "termination", "time": 1791674075400, "qty": 1, "price": 82900.0}, emit)
    src._handle({"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": [{"side": "buy", "qty": 1, "price": 1, "time": 1}]}, emit)
    src._handle({"feed": "ticker", "product_id": "PF_XBTUSD", "time": 1791674204002, "bid": 82978.0, "ask": 82979.0, "last": 82978.0,
                 "markPrice": 82978.8, "funding_rate": 0.0979, "openInterest": 2289.7}, emit)
    src._handle({"feed": "trade", "product_id": "PF_ETHUSD", "side": "buy", "type": "fill", "time": 1, "qty": 1, "price": 1}, emit)   # not ours
    src._handle({"event": "error", "message": "Invalid product id"}, emit)
    assert len(ticks) == 3
    t0, t1, q = ticks
    assert t0.symbol == "BTC-USD" and t0.price == 82981.0 and t0.size == 0.0002 and t0.taker_buy is True and not t0.quote
    assert abs(t0.ts - 1791674075.217) < 1e-6
    assert t1.taker_buy is False and t1.size == 1.5                     # a liquidation is a real trade; the termination was not
    assert q.quote and q.price == 82978.0 and q.bid == 82978.0 and q.ask == 82979.0 and q.size == 0.0 and abs(q.ts - 1791674204.002) < 1e-6
    assert "Invalid product id" in src.note


def test_krakenfutures_candles_page_forward_oldest_first_and_bounded():
    src = KrakenFuturesSource(cfg(), ["BTC-USD"])
    rows = lambda ks: [{"time": (T0 + 300 * k) * 1000, "open": str(100 + k), "high": str(110 + k), "low": str(90 + k), "close": str(105 + k), "volume": f"{0.5 + k}"} for k in ks]
    c = Client([({"candles": rows([0, 1, 2]), "more_candles": True}, 200), ({"candles": rows([3, 4]), "more_candles": False}, 200)])
    out = asyncio.run(src._candles(c, "BTC-USD", T0 + 300, T0 + 300 * 4))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900]           # [start, end), oldest first, across the page break
    assert out[0].o == 101 and out[0].h == 111 and out[0].l == 91 and out[0].price == 106 and out[0].size == 1.5
    assert out[0].symbol == "BTC-USD" and all(t.quote is False for t in out)
    url, params = c.calls[0]
    assert "/api/charts/v1/" in url and url.endswith("/PF_XBTUSD/5m") and params == {"from": T0 + 300, "to": T0 + 1200}
    assert c.calls[1][1]["from"] == T0 + 600 + 300                           # the next page starts one bar after the last candle
    assert not src._unsupported                                               # an empty page is not a 4xx: the list decides `knows`
    empty = Client([({"candles": [], "more_candles": False}, 200)])
    assert asyncio.run(src._candles(empty, "NOPE-USD", T0, T0 + 600)) == [] and src.supports("NOPE-USD")


def test_krakenfutures_instrument_list():
    src = KrakenFuturesSource(cfg(), ["BTC-USD"])
    c = Client([({"result": "success", "instruments": [{"symbol": "PF_XBTUSD", "type": "flexible_futures", "tradeable": True},
                                                        {"symbol": "PF_SOLUSD", "tradeable": False}, {"symbol": "PI_XBTUSD", "tradeable": True},
                                                        {"symbol": "FI_ETHUSD_250926", "tradeable": True}]}, 200)])
    assert asyncio.run(src.list_products(c)) == {"PF_XBTUSD"}
    assert src.knows("BTC-USD") and not src.knows("SOL-USD") and not src.knows("ETH-USD")


# dYdX -------------------------------------------------------------------------------

def test_dydx_symbols_and_messages():
    assert DydxSource.inst("BTC-USD") == "BTC-USD" and DydxSource.inst("pepe-usd") == "PEPE-USD"
    src = DydxSource(cfg(), ["BTC-USD"])
    src._insts = {"BTC-USD": "BTC-USD"}
    ticks, emit = collect()
    src._handle({"type": "connected", "connection_id": "c", "message_id": 0}, emit)
    src._handle({"type": "subscribed", "channel": "v4_trades", "id": "BTC-USD", "contents": {"trades": [
        {"id": "a", "side": "BUY", "size": "10", "price": "110.4", "type": "LIMIT", "createdAt": "2026-10-10T23:10:46.204Z"}]}}, emit)   # history: skipped
    src._handle({"type": "channel_data", "channel": "v4_trades", "id": "BTC-USD", "version": "2.1.0", "contents": {"trades": [
        {"id": "b", "size": "0.003", "price": "82502.6", "side": "SELL", "createdAt": "2026-10-10T23:15:03.022Z", "type": "LIMIT"},
        {"id": "c", "size": "0.5", "price": "82503", "side": "BUY", "createdAt": "2026-10-10T23:15:03.022Z", "type": "LIQUIDATED"}]}}, emit)
    src._handle({"type": "channel_data", "channel": "v4_trades", "id": "ETH-USD", "contents": {"trades": [{"size": "1", "price": "1", "side": "BUY"}]}}, emit)
    src._handle({"type": "error", "message": "Invalid channel"}, emit)
    assert len(ticks) == 2
    assert ticks[0].symbol == "BTC-USD" and ticks[0].price == 82502.6 and ticks[0].size == 0.003 and ticks[0].taker_buy is False
    assert abs(ticks[0].ts - 1791674103.022) < 1e-6 and not ticks[0].quote
    assert ticks[1].taker_buy is True and ticks[1].size == 0.5
    assert "Invalid channel" in src.note


def test_dydx_candles_page_backward_dedupe_and_unknown_market():
    src = DydxSource(cfg(), ["BTC-USD"])
    src.PER_REQUEST = 3
    row = lambda k: {"startedAt": f"2026-10-11T00:{5 * k:02d}:00.000Z", "ticker": "BTC-USD", "resolution": "5MINS", "low": str(90 + k), "high": str(110 + k),
                     "open": str(100 + k), "close": str(105 + k), "baseTokenVolume": str(2 + k), "usdVolume": "1", "trades": 3}
    c = Client([({"candles": [row(3), row(2), row(1)]}, 200), ({"candles": [row(1), row(0)]}, 200)])   # newest first; the boundary candle repeats
    out = asyncio.run(src._candles(c, "BTC-USD", T0, T0 + 300 * 3))
    assert [t.ts for t in out] == [T0, T0 + 300, T0 + 600]                                           # oldest first, [start, end), deduped
    assert out[1].o == 101 and out[1].h == 111 and out[1].l == 91 and out[1].price == 106 and out[1].size == 3
    url, params = c.calls[0]
    assert url.endswith("/candles/perpetualMarkets/BTC-USD") and params["resolution"] == "5MINS" and params["limit"] == 3
    assert params["fromISO"] == "2026-10-11T00:00:00Z" and params["toISO"] == "2026-10-11T00:15:00Z"
    assert c.calls[1][1]["toISO"] == "2026-10-11T00:05:00Z"                                            # back to the oldest seen
    bad = Client([({"errors": [{"msg": "ticker must be a valid ticker (BTC-USD, etc)"}]}, 400)])
    assert asyncio.run(src._candles(bad, "NOPE-USD", T0, T0 + 600)) == [] and not src.supports("NOPE-USD")


def test_dydx_market_list():
    src = DydxSource(cfg(), ["BTC-USD", "SOL-USD"])
    c = Client([({"markets": {"BTC-USD": {"status": "ACTIVE", "oraclePrice": "82986"}, "SOL-USD": {"status": "FINAL_SETTLEMENT"}}}, 200)])
    assert asyncio.run(src.list_products(c)) == {"BTC-USD"}
    assert src.knows("BTC-USD") and not src.knows("SOL-USD")


# Deribit ----------------------------------------------------------------------------

def test_deribit_symbols_and_instrument_list():
    assert DeribitSource.inst("BTC-USD") == "BTC-PERPETUAL" and DeribitSource.inst("ETH-USD") == "ETH-PERPETUAL"
    assert DeribitSource.inst("SOL-USD") == "SOL_USDC-PERPETUAL"
    assert DeribitSource.is_inverse("BTC-PERPETUAL") and not DeribitSource.is_inverse("SOL_USDC-PERPETUAL")
    src = DeribitSource(cfg(), ["BTC-USD", "SOL-USD", "PEPE-USD"])
    inst = lambda name, period="perpetual", active=True: {"instrument_name": name, "settlement_period": period, "is_active": active, "kind": "future"}
    c = Client([({"result": [inst("BTC_USDC-PERPETUAL"), inst("BTC-PERPETUAL"), inst("ETH-PERPETUAL"), inst("SOL_USDC-PERPETUAL"),
                             inst("1000PEPE_USDC-PERPETUAL"), inst("BTC-26DEC26", "month"), inst("XRP_USDC-PERPETUAL", active=False)]}, 200)])
    prods = asyncio.run(src.list_products(c))
    assert prods == {"BTC-USD": "BTC-PERPETUAL", "ETH-USD": "ETH-PERPETUAL", "SOL-USD": "SOL_USDC-PERPETUAL", "PEPE-USD": "1000PEPE_USDC-PERPETUAL"}
    assert src._scale["1000PEPE_USDC-PERPETUAL"] == 1000.0 and src._scale["BTC-PERPETUAL"] == 1.0
    assert src._inst("PEPE-USD") == "1000PEPE_USDC-PERPETUAL" and src.knows("PEPE-USD") and not src.knows("XRP-USD")
    assert c.calls[0][1] == {"currency": "any", "kind": "future"}


def test_deribit_messages_inverse_linear_scaled_and_heartbeat():
    src = DeribitSource(cfg(), ["BTC-USD", "SOL-USD", "PEPE-USD"])
    src._insts = {"BTC-PERPETUAL": "BTC-USD", "SOL_USDC-PERPETUAL": "SOL-USD", "1000PEPE_USDC-PERPETUAL": "PEPE-USD"}
    src._scale = {"1000PEPE_USDC-PERPETUAL": 1000.0}
    ticks, emit = collect()
    sub = lambda ch, data: {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": ch, "data": data}}
    src._handle(sub("trades.BTC-PERPETUAL.100ms", [{"timestamp": 1791673862383, "price": 83002.0, "amount": 5000.0, "direction": "sell",
                                                     "instrument_name": "BTC-PERPETUAL", "contracts": 500.0, "trade_id": "1"}]), emit)
    src._handle(sub("trades.SOL_USDC-PERPETUAL.100ms", [{"timestamp": 1791673986064, "price": 110.265, "amount": 0.54, "direction": "buy"}]), emit)
    src._handle(sub("trades.1000PEPE_USDC-PERPETUAL.100ms", [{"timestamp": 1791673986064, "price": 0.0123, "amount": 10.0, "direction": "buy"}]), emit)
    src._handle(sub("ticker.1000PEPE_USDC-PERPETUAL.100ms", {"timestamp": 1791674087164, "best_bid_price": 0.0122, "best_ask_price": 0.0124,
                                                              "last_price": 0.0123, "mark_price": 0.0123, "funding_8h": 0.0001, "open_interest": 1.0}), emit)
    src._handle(sub("ticker.BTC-PERPETUAL.100ms", {"timestamp": 1791674087213, "best_bid_price": 82997.5, "best_ask_price": 82998.0, "last_price": 82998.0}), emit)
    src._handle(sub("trades.ETH-PERPETUAL.100ms", [{"timestamp": 1, "price": 1, "amount": 1, "direction": "buy"}]), emit)      # not ours
    src._handle({"jsonrpc": "2.0", "id": 2, "result": ["trades.BTC-PERPETUAL.100ms"]}, emit)
    src._handle({"jsonrpc": "2.0", "id": 2, "error": {"code": 10001, "message": "bad channel"}}, emit)
    assert len(ticks) == 5
    btc, sol, pepe, pq, bq = ticks
    assert btc.symbol == "BTC-USD" and btc.price == 83002.0 and abs(btc.size - 5000.0 / 83002.0) < 1e-12 and btc.taker_buy is False   # USD notional -> coins
    assert abs(btc.ts - 1791673862.383) < 1e-6 and not btc.quote
    assert sol.price == 110.265 and sol.size == 0.54 and sol.taker_buy is True                                                        # linear: amount in the coin
    assert abs(pepe.price - 0.0000123) < 1e-12 and abs(pepe.size - 10000.0) < 1e-9                                                   # a 1000PEPE contract
    assert pq.quote and abs(pq.bid - 0.0000122) < 1e-12 and abs(pq.ask - 0.0000124) < 1e-12 and abs(pq.price - 0.0000123) < 1e-12
    assert bq.quote and bq.bid == 82997.5 and bq.ask == 82998.0 and bq.price == 82998.0 and abs(bq.ts - 1791674087.213) < 1e-6
    assert "bad channel" in src.note
    answer = json.loads(DeribitSource._reply({"jsonrpc": "2.0", "method": "heartbeat", "params": {"type": "test_request"}}))
    assert answer["method"] == "public/test" and answer["jsonrpc"] == "2.0"
    assert DeribitSource._reply({"method": "heartbeat", "params": {"type": "heartbeat"}}) is None and DeribitSource._reply({"id": 1}) is None


def test_deribit_chart_pages_backward_scaled_and_unknown():
    src = DeribitSource(cfg(), ["PEPE-USD"])
    src.products = {"PEPE-USD": "1000PEPE_USDC-PERPETUAL"}
    src._scale = {"1000PEPE_USDC-PERPETUAL": 1000.0}
    src.PER_REQUEST = 3
    page = lambda ks: {"result": {"status": "ok", "ticks": [(T0 + 300 * k) * 1000 for k in ks], "open": [10.0 + k for k in ks], "high": [11.0 + k for k in ks],
                                  "low": [9.0 + k for k in ks], "close": [10.5 + k for k in ks], "volume": [2.0 + k for k in ks], "cost": [1.0 for _ in ks]}}
    c = Client([(page([1, 2, 3]), 200), (page([0]), 200)])
    out = asyncio.run(src._chart(c, "PEPE-USD", T0, T0 + 300 * 3))
    assert [t.ts for t in out] == [T0, T0 + 300, T0 + 600]                            # [start, end), oldest first, across two pages
    assert abs(out[1].o - 0.011) < 1e-12 and abs(out[1].h - 0.012) < 1e-12 and abs(out[1].l - 0.010) < 1e-12 and abs(out[1].price - 0.0115) < 1e-12
    assert out[1].size == 3000.0                                                       # volume in 1000PEPE -> PEPE
    url, params = c.calls[0]
    assert url.endswith("/public/get_tradingview_chart_data") and params["instrument_name"] == "1000PEPE_USDC-PERPETUAL" and params["resolution"] == "5"
    assert params["start_timestamp"] == T0 * 1000 and params["end_timestamp"] == (T0 + 900) * 1000
    assert c.calls[1][1]["end_timestamp"] == (T0 + 300) * 1000 - 1                      # just before the oldest tick of the full page
    bad = Client([({"error": {"message": "Invalid params", "data": {"reason": "instrument not found"}}}, 400)])
    assert asyncio.run(src._chart(bad, "NOPE-USD", T0, T0 + 600)) == [] and not src.supports("NOPE-USD")


# Aster ------------------------------------------------------------------------------

def test_aster_symbols_and_exchange_info():
    assert AsterSource.pair("BTC-USD") == "BTCUSDT" and AsterSource.pair("doge-usd") == "DOGEUSDT"
    src = AsterSource(cfg(), ["BTC-USD", "PEPE-USD", "SOL-USD"])
    s = lambda name, status="TRADING", quote="USDT", ct="PERPETUAL": {"symbol": name, "status": status, "quoteAsset": quote, "contractType": ct, "baseAsset": name[:-len(quote)]}
    c = Client([({"symbols": [s("BTCUSDT"), s("1000PEPEUSDT"), s("BTCUSD1", quote="USD1"), s("BTCU", quote="U"), s("SOLUSDT", status="SETTLING"),
                              s("ETHUSDT_260327", ct="CURRENT_QUARTER")]}, 200)])
    assert asyncio.run(src.list_products(c)) == {"BTC-USD": "BTCUSDT", "PEPE-USD": "1000PEPEUSDT"}
    assert src._scale == {"BTCUSDT": 1.0, "1000PEPEUSDT": 1000.0}
    assert src._pair("PEPE-USD") == "1000PEPEUSDT" and src._pair("BTC-USD") == "BTCUSDT"
    assert src.knows("BTC-USD") and not src.knows("SOL-USD")


def test_aster_messages_including_a_scaled_contract():
    src = AsterSource(cfg(), ["BTC-USD", "PEPE-USD"])
    src._pairs = {"BTCUSDT": "BTC-USD", "1000PEPEUSDT": "PEPE-USD"}
    src._scale = {"1000PEPEUSDT": 1000.0}
    ticks, emit = collect()
    src._handle({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "E": 1791674086639, "a": 1, "s": "BTCUSDT", "p": "83013.0", "q": "0.050",
                                                        "f": 1, "l": 1, "T": 1791674086450, "m": False}}, emit)
    src._handle({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "83013.0", "q": "0.083", "T": 1791674098150, "m": True}}, emit)
    src._handle({"stream": "btcusdt@bookTicker", "data": {"e": "bookTicker", "u": 1, "s": "BTCUSDT", "b": "83013.0", "B": "0.005", "a": "83013.1", "A": "0.9",
                                                          "T": 1791674086650, "E": 1791674086671}}, emit)
    src._handle({"stream": "1000pepeusdt@aggTrade", "data": {"e": "aggTrade", "s": "1000PEPEUSDT", "p": "0.0123", "q": "10", "T": 1791674086450, "m": False}}, emit)
    src._handle({"stream": "ethusdt@aggTrade", "data": {"e": "aggTrade", "s": "ETHUSDT", "p": "2505", "q": "1", "T": 1, "m": False}}, emit)     # not ours
    assert len(ticks) == 4
    a, b, q, p = ticks
    assert a.symbol == "BTC-USD" and a.price == 83013.0 and a.size == 0.05 and a.taker_buy is True and abs(a.ts - 1791674086.45) < 1e-6
    assert b.taker_buy is False                                                         # m: the buyer was the maker, so the taker sold
    assert q.quote and q.bid == 83013.0 and q.ask == 83013.1 and abs(q.price - 83013.05) < 1e-9 and q.size == 0.0
    assert abs(p.price - 0.0000123) < 1e-12 and abs(p.size - 10000.0) < 1e-9 and p.symbol == "PEPE-USD"


def test_aster_klines_page_forward_scaled_and_unknown():
    src = AsterSource(cfg(), ["PEPE-USD"])
    src.products = {"PEPE-USD": "1000PEPEUSDT"}
    src._scale = {"1000PEPEUSDT": 1000.0}
    src.PER_REQUEST = 2
    row = lambda k: [(T0 + 300 * k) * 1000, f"{10 + k}", f"{11 + k}", f"{9 + k}", f"{10.5 + k}", f"{2 + k}", (T0 + 300 * k + 299) * 1000 + 999, "1", 5, "1", "1", "0"]
    c = Client([([row(0), row(1)], 200), ([row(2), row(3)], 200), ([row(4)], 200)])
    out = asyncio.run(src._klines(c, "PEPE-USD", T0 + 300, T0 + 300 * 4))
    assert [t.ts for t in out] == [T0 + 300, T0 + 600, T0 + 900]                          # [start, end), oldest first, Binance row order
    assert abs(out[0].o - 0.011) < 1e-12 and abs(out[0].h - 0.012) < 1e-12 and abs(out[0].l - 0.010) < 1e-12 and abs(out[0].price - 0.0115) < 1e-12
    assert out[0].size == 3000.0
    url, params = c.calls[0]
    assert url.endswith("/fapi/v1/klines") and params["symbol"] == "1000PEPEUSDT" and params["interval"] == "5m" and params["limit"] == 2
    assert params["startTime"] == (T0 + 300) * 1000 and params["endTime"] == (T0 + 1200) * 1000 - 1
    assert c.calls[1][1]["startTime"] == (T0 + 300) * 1000 + 1                           # one ms past the last open
    bad = Client([({"code": -1121, "msg": "Invalid symbol."}, 400)])
    assert asyncio.run(src._klines(bad, "NOPE-USD", T0, T0 + 600)) == [] and not src.supports("NOPE-USD")
