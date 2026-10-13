"""The Binance futures, Bybit, OKX and Bitget execution clients: symbol mapping, the signed
requests of a market buy and a limit sell in dry-run, the documented signature vectors, the
answer mapping from captured successes and rejections, and the refusal of an undeclared path.
No network: `_send` is replaced with canned answers wherever a client would send."""
import asyncio
import base64
import hashlib
import hmac

import pytest

from steel.config import Config
from steel.execution import ExecutionGate, Order
from steel.execution.binance import OPEN_ORDERS as BN_OPEN, ORDER as BN_ORDER, TRADES as BN_TRADES, BinanceExecutor
from steel.execution.bitget import CANCEL as BG_CANCEL, PENDING as BG_PENDING, PLACE as BG_PLACE, BitgetExecutor
from steel.execution.bybit import CANCEL as BY_CANCEL, CREATE as BY_CREATE, EXECUTIONS as BY_EXEC, OPEN_ORDERS as BY_OPEN, BybitExecutor
from steel.execution.okx import CANCEL as OK_CANCEL, FILLS as OK_FILLS, INSTRUMENTS as OK_INST, PLACE as OK_PLACE, OKXExecutor

NOW = 1_791_676_800.0          # 2026-10-11 00:00:00 UTC


def gate(tmp_path, venue):
    c = Config()
    c.execution, c.execution_venues = True, venue
    c.exec_max_order_usd, c.exec_max_name_usd, c.exec_max_venue_usd, c.exec_max_day_usd = 1_000.0, 2_500.0, 3_000.0, 5_000.0
    c.exec_max_exposure_frac, c.exec_max_leverage, c.exec_reject_cooldown_s = 0.5, 2.0, 300.0
    return ExecutionGate(c, tmp_path)


def market_buy(venue, sym="BTC-USD", qty=0.01):
    return Order(venue, sym, "buy", qty, notional_usd=600.0, client_id="steel-1")


def limit_sell(venue, sym="BTC-USD", qty=0.01, price=65_000.0):
    return Order(venue, sym, "sell", qty, price=price, reduce_only=True, notional_usd=650.0, client_id="steel:2")


def canned(ex, answers):
    """Replace the client's `_send` with one that records the request and answers from `answers` (by path)."""
    calls = []

    async def fake(method, url, *, params=None, data=None, json_body=None, headers=None, content=None, timeout=15.0):
        if not ex.allows(url):
            raise PermissionError("undeclared")
        path = url.split(".com", 1)[1].split("?")[0]
        calls.append((method, url, headers, content))
        return {"method": method, "url": url, "content": content, "headers": ex._scrub(headers)}, dict(answers[path])
    ex._send = fake
    return calls


# Binance -------------------------------------------------------------------------------------

def test_binance_symbols_and_documented_signature(tmp_path):
    assert BinanceExecutor.contract("BTC-USD") == ("BTCUSDT", 1.0) and BinanceExecutor.contract("pepe-usd") == ("1000PEPEUSDT", 1000.0)
    assert BinanceExecutor.contract("BABYDOGE-USD") == ("1MBABYDOGEUSDT", 1e6) and BinanceExecutor.canonical("1000SHIBUSDT") == ("SHIB-USD", 1000.0)
    ex = BinanceExecutor(gate(tmp_path, "binance"), key="vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A",
                         secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"   # Binance's docs vector
    assert ex.sign(q) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert ex.query({"symbol": "BTCUSDT"}, ts=1499827319559).startswith("symbol=BTCUSDT&timestamp=1499827319559&recvWindow=5000&signature=")
    assert ex.host == "https://fapi.binance.com" and BinanceExecutor(gate(tmp_path, "binance"), testnet=True).host == "https://testnet.binancefuture.com"
    assert BinanceExecutor.describe()["file"] == "binance-trade.json" and BinanceExecutor.describe()["testnet"]


def test_binance_dry_run_market_buy_and_limit_sell(tmp_path):
    ex = BinanceExecutor(gate(tmp_path, "binance"), key="k", secret="s", dry_run=True)
    ex.now = NOW
    res = asyncio.run(ex.place(market_buy("binance", "PEPE-USD", 50_000.0)))
    assert res.ok and res.status == "dry-run"
    assert res.request["method"] == "POST" and res.request["url"].startswith("https://fapi.binance.com/fapi/v1/order?")
    assert res.request["url"].split("?")[1] == ("symbol=1000PEPEUSDT&side=BUY&type=MARKET&quantity=50&reduceOnly=false&newClientOrderId=steel-1"
                                                "&timestamp=1791676800000&recvWindow=5000&signature=***")        # the signature never reaches the log
    assert res.request["headers"] == {"X-MBX-APIKEY": "***"}
    res = asyncio.run(ex.place(limit_sell("binance")))
    assert res.status == "dry-run" and res.request["url"].split("?")[1].startswith(
        "symbol=BTCUSDT&side=SELL&type=LIMIT&quantity=0.01&reduceOnly=true&timeInForce=GTC&price=65000&newClientOrderId=steel%3A2&timestamp=")
    assert ex.gate.spent_day[ex.gate.day()] == 1_250.0


def test_binance_answer_mapping_and_undeclared_path(tmp_path):
    ex = BinanceExecutor(gate(tmp_path, "binance"), key="k", secret="s", dry_run=False)
    ok = {"clientOrderId": "testOrder", "cumQty": "0", "cumQuote": "0", "executedQty": "0", "orderId": 22542179, "avgPrice": "0.00000", "origQty": "10",
          "price": "0", "reduceOnly": False, "side": "BUY", "positionSide": "BOTH", "status": "NEW", "symbol": "BTCUSDT", "timeInForce": "GTC",
          "type": "LIMIT", "updateTime": 1566818724722, "_status": 200}
    calls = canned(ex, {BN_ORDER: ok})
    res = asyncio.run(ex.place(limit_sell("binance")))
    assert res.ok and res.status == "accepted" and res.order_id == "22542179" and res.filled_qty == 0.0 and res.avg_price is None
    filled = ok | {"status": "FILLED", "executedQty": "2000", "avgPrice": "0.0000125", "symbol": "1000PEPEUSDT", "orderId": 7, "type": "MARKET"}
    canned(ex, {BN_ORDER: filled})
    res = asyncio.run(ex.place(market_buy("binance", "PEPE-USD", 2_000_000.0)))
    assert res.status == "filled" and res.order_id == "7" and res.filled_qty == 2_000_000.0 and res.avg_price == pytest.approx(1.25e-8)
    canned(ex, {BN_ORDER: {"code": -2022, "msg": "ReduceOnly Order is rejected.", "_status": 400}})
    res = asyncio.run(ex.place(limit_sell("binance")))
    assert not res.ok and res.status == "rejected" and res.error == "ReduceOnly Order is rejected. (-2022)" and ex.gate.rejects.pop("binance") > 0
    canned(ex, {BN_ORDER: ok | {"status": "CANCELED", "orderId": 283194212}})
    res = asyncio.run(ex.cancel("283194212", "BTC-USD"))
    assert res.order_id == "283194212" and res.status == "rejected" and res.request["method"] == "DELETE" and "symbol=BTCUSDT&orderId=283194212" in res.request["url"]
    calls = canned(ex, {BN_OPEN: {"data": [{"orderId": 1, "symbol": "1000PEPEUSDT", "side": "SELL", "origQty": "50", "price": "0.0000130", "executedQty": "10",
                                           "status": "PARTIALLY_FILLED"}]},
                        BN_TRADES: {"data": [{"id": 9, "orderId": 1, "symbol": "1000PEPEUSDT", "side": "SELL", "qty": "10", "price": "0.0000130", "commission": "0.0026",
                                             "commissionAsset": "USDT", "time": 1791676800123}]}})
    ex.recent = {"1000PEPEUSDT"}
    assert asyncio.run(ex.open_orders()) == [{"order_id": "1", "symbol": "PEPE-USD", "side": "sell", "qty": 50_000.0, "price": pytest.approx(1.3e-8),
                                              "filled": 10_000.0, "status": "partially_filled"}]
    fills = asyncio.run(ex.fills(NOW - 60))
    assert fills == [{"order_id": "1", "symbol": "PEPE-USD", "side": "sell", "qty": 10_000.0, "price": pytest.approx(1.3e-8), "fee": 0.0026, "t": 1791676800.123}]
    assert "startTime=1791676740000" in calls[-1][1]
    with pytest.raises(PermissionError):
        asyncio.run(BinanceExecutor(gate(tmp_path, "binance"), key="k", secret="s")._send("GET", "https://fapi.binance.com/fapi/v2/balance"))


# Bybit ---------------------------------------------------------------------------------------

def test_bybit_symbols_and_documented_pre_sign_string(tmp_path):
    assert BybitExecutor.contract("BTC-USD") == ("BTCUSDT", 1.0) and BybitExecutor.contract("PEPE-USD") == ("1000PEPEUSDT", 1000.0)
    assert BybitExecutor.contract("MOG-USD") == ("1000000MOGUSDT", 1e6) and BybitExecutor.contract("SATS-USD") == ("SATSUSDT", 1.0)   # 10000SATS is unmapped
    ex = BybitExecutor(gate(tmp_path, "bybit"), key="XXXXXXXXXX", secret="secret")
    q = "category=option&symbol=BTC-29JUL22-25000-C"                             # the query from Bybit's docs example
    want = hmac.new(b"secret", b"1658384314791XXXXXXXXXX5000category=option&symbol=BTC-29JUL22-25000-C", hashlib.sha256).hexdigest()
    assert ex.sign(1658384314791, q) == want
    assert ex.headers(q, ts=1658384314791) == {"X-BAPI-API-KEY": "XXXXXXXXXX", "X-BAPI-TIMESTAMP": "1658384314791", "X-BAPI-RECV-WINDOW": "5000",
                                               "X-BAPI-SIGN-TYPE": "2", "X-BAPI-SIGN": want, "Content-Type": "application/json"}
    assert BybitExecutor(gate(tmp_path, "bybit"), testnet=True).host == "https://api-testnet.bybit.com"


def test_bybit_dry_run_market_buy_and_limit_sell(tmp_path):
    ex = BybitExecutor(gate(tmp_path, "bybit"), key="k", secret="s", dry_run=True)
    ex.now = NOW
    res = asyncio.run(ex.place(market_buy("bybit")))
    assert res.status == "dry-run" and res.request["url"] == "https://api.bybit.com/v5/order/create" and res.request["method"] == "POST"
    assert res.request["content"] == '{"category":"linear","symbol":"BTCUSDT","side":"Buy","orderType":"Market","qty":"0.01","timeInForce":"IOC","reduceOnly":false,"positionIdx":0,"orderLinkId":"steel-1"}'
    assert res.request["headers"] == {"X-BAPI-API-KEY": "***", "X-BAPI-TIMESTAMP": "1791676800000", "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN-TYPE": "2",
                                      "X-BAPI-SIGN": "***", "Content-Type": "application/json"}
    res = asyncio.run(ex.place(limit_sell("bybit", "PEPE-USD", 100_000.0, 0.00001)))
    assert res.status == "dry-run" and res.request["content"] == ('{"category":"linear","symbol":"1000PEPEUSDT","side":"Sell","orderType":"Limit","qty":"100",'
                                                                  '"timeInForce":"GTC","reduceOnly":true,"positionIdx":0,"price":"0.01","orderLinkId":"steel2"}')


def test_bybit_answer_mapping_and_undeclared_path(tmp_path):
    ex = BybitExecutor(gate(tmp_path, "bybit"), key="k", secret="s", dry_run=False)
    ok = {"retCode": 0, "retMsg": "OK", "result": {"orderId": "1321003749386327552", "orderLinkId": "steel-1"}, "retExtInfo": {}, "time": 1672211918471, "_status": 200}
    canned(ex, {BY_CREATE: ok})
    res = asyncio.run(ex.place(market_buy("bybit")))
    assert res.ok and res.status == "accepted" and res.order_id == "1321003749386327552"
    canned(ex, {BY_CREATE: {"retCode": 110007, "retMsg": "ab not enough for new order", "result": {}, "retExtInfo": {}, "time": 1672211918471, "_status": 200}})
    res = asyncio.run(ex.place(market_buy("bybit")))
    assert not res.ok and res.status == "rejected" and res.error == "ab not enough for new order (110007)" and ex.gate.rejects.pop("bybit") > 0
    canned(ex, {BY_CREATE: {"text": "", "_status": 401}})
    assert asyncio.run(ex.place(market_buy("bybit"))).error == "HTTP 401: "
    calls = canned(ex, {BY_CANCEL: {"retCode": 0, "retMsg": "OK", "result": {"orderId": "c6f055d9-7f21-4079-913d-e6523a9cfffa", "orderLinkId": "linear-004"}, "_status": 200}})
    res = asyncio.run(ex.cancel("c6f055d9-7f21-4079-913d-e6523a9cfffa", "ETH-USD"))
    assert res.ok and res.status == "accepted" and calls[0][3] == '{"category":"linear","symbol":"ETHUSDT","orderId":"c6f055d9-7f21-4079-913d-e6523a9cfffa"}'
    calls = canned(ex, {BY_OPEN: {"retCode": 0, "result": {"list": [{"orderId": "o1", "symbol": "ETHUSDT", "side": "Buy", "price": "1600.00", "qty": "0.10", "cumExecQty": "0.00",
                                                                     "orderStatus": "New"}]}, "_status": 200},
                        BY_EXEC: {"retCode": 0, "result": {"list": [{"symbol": "1000PEPEUSDT", "side": "Buy", "orderId": "8c06", "execId": "e0cb", "execQty": "100",
                                                                     "execPrice": "0.0125", "execFee": "0.000687", "execTime": "1672282722429", "execType": "Trade"}]}, "_status": 200}})
    assert asyncio.run(ex.open_orders()) == [{"order_id": "o1", "symbol": "ETH-USD", "side": "buy", "qty": 0.1, "price": 1600.0, "filled": 0.0, "status": "new"}]
    assert "category=linear&limit=50&settleCoin=USDT" in calls[0][1]
    ex.now = NOW
    assert asyncio.run(ex.fills(NOW - 30 * 86_400)) == [{"order_id": "8c06", "symbol": "PEPE-USD", "side": "buy", "qty": 100_000.0, "price": 1.25e-5,
                                                         "fee": 0.000687, "t": 1672282722.429}]
    assert f"startTime={int((NOW - 7 * 86_400) * 1000)}" in calls[-1][1]                          # clamped to Bybit's 7-day window
    with pytest.raises(PermissionError):
        asyncio.run(BybitExecutor(gate(tmp_path, "bybit"), key="k", secret="s")._send("GET", "https://api.bybit.com/v5/account/wallet-balance"))


# OKX -----------------------------------------------------------------------------------------

def test_okx_instruments_stamp_and_signature(tmp_path):
    assert OKXExecutor.inst("btc-usd") == "BTC-USDT-SWAP" and OKXExecutor.canonical("PEPE-USDT-SWAP") == "PEPE-USD"
    ex = OKXExecutor(gate(tmp_path, "okx"), key="k", secret="22582BD0CFF14C41EDBF1AB98506286D", passphrase="pp")    # the sample secret format from OKX's docs
    assert OKXExecutor.stamp(1607418537.715) == "2020-12-08T09:08:57.715Z"
    ts, path = "2020-12-08T09:08:57.715Z", "/api/v5/account/balance?ccy=BTC"
    want = base64.b64encode(hmac.new(b"22582BD0CFF14C41EDBF1AB98506286D", (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    assert ex.sign(ts, "GET", path) == want and ex.sign(ts, "GET", path, "{}") != want
    assert ex.headers("GET", path, ts=ts) == {"OK-ACCESS-KEY": "k", "OK-ACCESS-SIGN": want, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": "pp",
                                              "Content-Type": "application/json"}
    demo = OKXExecutor(gate(tmp_path, "okx"), key="k", secret="s", passphrase="p", testnet=True)
    assert demo.host == "https://www.okx.com" and demo.headers("GET", path, ts=ts)["x-simulated-trading"] == "1" and OKXExecutor.describe()["testnet"]
    assert ex.contracts("BTC-USDT-SWAP", 0.01) == "1" and ex.contracts("BTC-USDT-SWAP", 0.0157) == "1.57" and ex.contracts("DOGE-USDT-SWAP", 2500) == "2.5"
    assert ex.contracts("BTC-USDT-SWAP", 0.000149) == "0.01"                     # floored to the lot, never up
    with pytest.raises(ValueError, match="under one lot"):
        ex.contracts("BTC-USDT-SWAP", 0.00001)
    with pytest.raises(ValueError, match="unknown"):
        ex.contracts("NOPE-USDT-SWAP", 1.0)


def test_okx_dry_run_market_buy_and_limit_sell(tmp_path):
    ex = OKXExecutor(gate(tmp_path, "okx"), key="k", secret="s", passphrase="p", dry_run=True)
    ex.now = NOW
    res = asyncio.run(ex.place(market_buy("okx")))
    assert res.status == "dry-run" and res.request["url"] == "https://www.okx.com/api/v5/trade/order" and res.request["method"] == "POST"
    assert res.request["content"] == '{"instId":"BTC-USDT-SWAP","tdMode":"isolated","side":"buy","ordType":"market","sz":"1","reduceOnly":false,"clOrdId":"steel1"}'
    assert res.request["headers"] == {"OK-ACCESS-KEY": "***", "OK-ACCESS-SIGN": "***", "OK-ACCESS-TIMESTAMP": "2026-10-11T00:00:00.000Z", "OK-ACCESS-PASSPHRASE": "***",
                                      "Content-Type": "application/json"}
    res = asyncio.run(ex.place(limit_sell("okx", "ETH-USD", 0.5, 3_200.5)))
    assert res.status == "dry-run" and res.request["content"] == '{"instId":"ETH-USDT-SWAP","tdMode":"isolated","side":"sell","ordType":"limit","sz":"5","reduceOnly":true,"px":"3200.5","clOrdId":"steel2"}'
    res = asyncio.run(ex.place(market_buy("okx", "NOPE-USD", 1.0)))
    assert res.status == "failed" and "contract value of NOPE-USDT-SWAP unknown" in res.error   # never sized blind


def test_okx_answer_mapping_instrument_table_and_undeclared_path(tmp_path):
    ex = OKXExecutor(gate(tmp_path, "okx"), key="k", secret="s", passphrase="p", dry_run=False)
    ex.now = NOW
    inst = {"code": "0", "msg": "", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "state": "live"},
                                              {"instId": "XRP-USDT-SWAP", "ctVal": "100", "lotSz": "0.01", "minSz": "0.01", "state": "live"},
                                              {"instId": "BTC-USD-SWAP", "ctVal": "100", "lotSz": "1", "state": "live"}], "_status": 200}
    ok = {"code": "0", "msg": "", "data": [{"clOrdId": "oktswap6", "ordId": "312269865356374016", "tag": "", "ts": "1695190491421", "sCode": "0", "sMsg": ""}],
          "inTime": "1695190491421339", "outTime": "1695190491423240", "_status": 200}
    calls = canned(ex, {OK_INST: inst, OK_PLACE: ok})
    res = asyncio.run(ex.place(market_buy("okx", "XRP-USD", 250.0)))
    assert res.ok and res.status == "accepted" and res.order_id == "312269865356374016"
    assert calls[0][0] == "GET" and calls[0][1].endswith("/api/v5/public/instruments?instType=SWAP") and '"sz":"2.5"' in calls[1][3]
    assert "XRP-USDT-SWAP" in ex.table and "BTC-USD-SWAP" not in ex.table and ex.table_t == NOW
    failed = {"code": "1", "msg": "", "data": [{"clOrdId": "", "ordId": "", "tag": "", "ts": "1695190491421", "sCode": "51008",
                                                "sMsg": "Order failed. Insufficient USDT balance in account"}], "_status": 200}
    canned(ex, {OK_PLACE: failed})
    res = asyncio.run(ex.place(market_buy("okx")))
    assert not res.ok and res.status == "rejected" and res.error == "Order failed. Insufficient USDT balance in account (51008)" and len(calls) == 2
    assert ex.gate.rejects.pop("okx") > 0                                           # a rejection starts the gate's cooldown
    canned(ex, {OK_PLACE: {"code": "50113", "msg": "Invalid Sign", "data": [], "_status": 401}})
    assert asyncio.run(ex.place(market_buy("okx"))).error == "Invalid Sign (50113)"
    calls = canned(ex, {OK_CANCEL: {"code": "0", "msg": "", "data": [{"clOrdId": "", "ordId": "12345689", "ts": "1695190491421", "sCode": "0", "sMsg": ""}], "_status": 200}})
    res = asyncio.run(ex.cancel("12345689", "BTC-USD"))
    assert res.ok and res.order_id == "12345689" and calls[0][3] == '{"instId":"BTC-USDT-SWAP","ordId":"12345689"}'
    calls = canned(ex, {"/api/v5/trade/orders-pending": {"code": "0", "data": [{"ordId": "o1", "instId": "BTC-USDT-SWAP", "side": "sell", "sz": "2", "px": "70000",
                                                                                   "accFillSz": "0.5", "state": "partially_filled"}], "_status": 200},
                        OK_FILLS: {"code": "0", "data": [{"side": "buy", "fillSz": "2.5", "fillPx": "0.52", "fee": "-0.0065", "ordId": "o2", "instId": "XRP-USDT-SWAP",
                                                          "tradeId": "744876980", "ts": "1708587373362"}], "_status": 200}})
    assert asyncio.run(ex.open_orders("BTC-USD")) == [{"order_id": "o1", "symbol": "BTC-USD", "side": "sell", "qty": 0.02, "price": 70_000.0, "filled": 0.005,
                                                       "status": "partially_filled"}]
    assert calls[0][1].endswith("/api/v5/trade/orders-pending?instType=SWAP&instId=BTC-USDT-SWAP")
    assert asyncio.run(ex.fills(NOW - 60)) == [{"order_id": "o2", "symbol": "XRP-USD", "side": "buy", "qty": 250.0, "price": 0.52, "fee": 0.0065, "t": 1708587373.362}]
    with pytest.raises(PermissionError):
        asyncio.run(OKXExecutor(gate(tmp_path, "okx"), key="k", secret="s", passphrase="p")._send("GET", "https://www.okx.com/api/v5/account/balance"))


# Bitget --------------------------------------------------------------------------------------

def test_bitget_symbols_signature_and_headers(tmp_path):
    assert BitgetExecutor.contract("BTC-USD") == ("BTCUSDT", 1.0) and BitgetExecutor.contract("BONK-USD") == ("1000BONKUSDT", 1000.0)
    assert BitgetExecutor.contract("PEPE-USD") == ("PEPEUSDT", 1.0) and BitgetExecutor.canonical("1MBABYDOGEUSDT") == ("BABYDOGE-USD", 1e6)
    ex = BitgetExecutor(gate(tmp_path, "bitget"), key="k", secret="s3cret", passphrase="pass")
    ts, q = 1791676800000, "productType=USDT-FUTURES"
    want = base64.b64encode(hmac.new(b"s3cret", f"{ts}GET{BG_PENDING}?{q}".encode(), hashlib.sha256).digest()).decode()
    assert ex.sign(ts, "get", BG_PENDING, q) == want
    body = '{"symbol":"BTCUSDT"}'
    assert ex.sign(ts, "POST", BG_PLACE, "", body) == base64.b64encode(hmac.new(b"s3cret", f"{ts}POST{BG_PLACE}{body}".encode(), hashlib.sha256).digest()).decode()
    assert ex.headers("GET", BG_PENDING, q, ts=ts) == {"ACCESS-KEY": "k", "ACCESS-SIGN": want, "ACCESS-TIMESTAMP": "1791676800000", "ACCESS-PASSPHRASE": "pass",
                                                       "locale": "en-US", "Content-Type": "application/json"}
    demo = BitgetExecutor(gate(tmp_path, "bitget"), key="k", secret="s", passphrase="p", testnet=True)
    assert demo.host == "https://api.bitget.com" and demo.headers("GET", BG_PENDING, q)["paptrading"] == "1" and BitgetExecutor.describe()["testnet"]


def test_bitget_dry_run_market_buy_and_limit_sell(tmp_path):
    ex = BitgetExecutor(gate(tmp_path, "bitget"), key="k", secret="s", passphrase="p", dry_run=True)
    ex.now = NOW
    res = asyncio.run(ex.place(market_buy("bitget")))
    assert res.status == "dry-run" and res.request["url"] == "https://api.bitget.com/api/v2/mix/order/place-order" and res.request["method"] == "POST"
    assert res.request["content"] == ('{"symbol":"BTCUSDT","productType":"USDT-FUTURES","marginMode":"isolated","marginCoin":"USDT","size":"0.01","side":"buy",'
                                      '"orderType":"market","reduceOnly":"NO","clientOid":"steel-1"}')
    assert res.request["headers"] == {"ACCESS-KEY": "***", "ACCESS-SIGN": "***", "ACCESS-TIMESTAMP": "1791676800000", "ACCESS-PASSPHRASE": "***", "locale": "en-US",
                                      "Content-Type": "application/json"}
    res = asyncio.run(ex.place(limit_sell("bitget", "BONK-USD", 200_000.0, 0.00002)))
    assert res.status == "dry-run" and res.request["content"] == ('{"symbol":"1000BONKUSDT","productType":"USDT-FUTURES","marginMode":"isolated","marginCoin":"USDT",'
                                                                  '"size":"200","side":"sell","orderType":"limit","reduceOnly":"YES","price":"0.02","force":"gtc","clientOid":"steel2"}')


def test_bitget_answer_mapping_and_undeclared_path(tmp_path):
    ex = BitgetExecutor(gate(tmp_path, "bitget"), key="k", secret="s", passphrase="p", dry_run=False)
    canned(ex, {BG_PLACE: {"code": "00000", "msg": "success", "requestTime": 1695806875837, "data": {"clientOid": "121211212122", "orderId": "121211212122"}, "_status": 200}})
    res = asyncio.run(ex.place(market_buy("bitget")))
    assert res.ok and res.status == "accepted" and res.order_id == "121211212122"
    canned(ex, {BG_PLACE: {"code": "00000", "msg": "success", "requestTime": 1, "data": {"clientOid": "steel-1", "orderId": ""}, "_status": 200}})
    assert asyncio.run(ex.place(market_buy("bitget"))).order_id == "steel-1"                 # a trimmed reduce-only answer carries no orderId
    canned(ex, {BG_PLACE: {"code": "40762", "msg": "The order size is greater than the max open size", "requestTime": 1, "data": None, "_status": 400}})
    res = asyncio.run(ex.place(market_buy("bitget")))
    assert not res.ok and res.status == "rejected" and res.error == "The order size is greater than the max open size (40762)" and ex.gate.rejects["bitget"] > 0
    calls = canned(ex, {BG_CANCEL: {"code": "00000", "data": {"orderId": "123", "clientOid": ""}, "msg": "success", "requestTime": 1627293504612, "_status": 200}})
    res = asyncio.run(ex.cancel("123", "ETH-USD"))
    assert res.ok and res.order_id == "123" and calls[0][3] == '{"symbol":"ETHUSDT","productType":"USDT-FUTURES","marginCoin":"USDT","orderId":"123"}'
    calls = canned(ex, {BG_PENDING: {"code": "00000", "data": {"entrustedList": [{"symbol": "ethusdt", "size": "100", "orderId": "123", "clientOid": "12321", "baseVolume": "12.1",
                                                                                "price": "1900", "priceAvg": "1903", "status": "partially_filled", "side": "buy",
                                                                                "orderType": "limit", "cTime": "1627293504612"}], "endId": "123"}, "_status": 200},
                        "/api/v2/mix/order/fills": {"code": "00000", "data": {"fillList": [{"tradeId": "123", "symbol": "1000BONKUSDT", "orderId": "121212", "price": "0.02",
                                                                                            "baseVolume": "200", "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.0024"}],
                                                                                            "side": "buy", "cTime": "1627293509612"}], "endId": "123"}, "_status": 200}})
    assert asyncio.run(ex.open_orders()) == [{"order_id": "123", "symbol": "ETH-USD", "side": "buy", "qty": 100.0, "price": 1900.0, "filled": 12.1, "status": "partially_filled"}]
    assert calls[0][1].endswith("/api/v2/mix/order/orders-pending?productType=USDT-FUTURES&limit=100")
    assert asyncio.run(ex.fills(NOW)) == [{"order_id": "121212", "symbol": "BONK-USD", "side": "buy", "qty": 200_000.0, "price": pytest.approx(2e-5), "fee": 0.0024,
                                           "t": 1627293509.612}]
    assert calls[-1][1].endswith(f"/api/v2/mix/order/fills?productType=USDT-FUTURES&startTime={int(NOW * 1000)}&limit=100")
    with pytest.raises(PermissionError):
        asyncio.run(BitgetExecutor(gate(tmp_path, "bitget"), key="k", secret="s", passphrase="p")._send("GET", "https://api.bitget.com/api/v2/mix/account/accounts"))
