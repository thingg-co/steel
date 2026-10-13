"""The perp execution clients (Kraken Futures, BloFin, Deribit, Gate.io USDT perps): the symbol
mapping, the signed dry-run request for a market buy and a limit sell, the signing vectors the
readers pin (and Gate.io's published ones), the answer mapping from captured success and
rejection answers, the contract sizing, and the endpoint fence. No network."""
import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from steel.config import Config
from steel.execution import ExecutionGate, Order
from steel.execution.blofin import BloFinExecutor
from steel.execution.deribit import DeribitExecutor
from steel.execution.gateio_perp import GateIOPerpExecutor
from steel.execution.krakenfutures import KrakenFuturesExecutor

ALL = [KrakenFuturesExecutor, BloFinExecutor, DeribitExecutor, GateIOPerpExecutor]


def gate(tmp_path, venue):
    c = Config()
    c.execution, c.execution_venues = True, venue
    c.exec_max_order_usd, c.exec_max_name_usd, c.exec_max_venue_usd, c.exec_max_day_usd = 5_000.0, 10_000.0, 20_000.0, 50_000.0
    c.exec_max_exposure_frac, c.exec_max_leverage, c.exec_reject_cooldown_s = 0.5, 2.0, 300.0
    return ExecutionGate(c, tmp_path)


def run(coro):
    return asyncio.run(coro)


def canned(ex, answers):
    """Replace the gate's HTTP with canned answers; the endpoint fence still applies."""
    ex.dry_run = False
    sent = []

    async def send(method, url, *, params=None, data=None, json_body=None, headers=None, content=None, timeout=15.0):
        assert ex.allows(url), url
        sent.append({"method": method, "url": url, "content": content, "headers": ex._scrub(headers)})
        return sent[-1], answers.pop(0)
    ex._send = send
    return sent


def market_buy(venue, sym="BTC-USD", qty=0.02, px=80_000.0, client_id="steel-42", **kw):
    return Order(venue, sym, "buy", qty, notional_usd=qty * px, client_id=client_id, **kw)


def limit_sell(venue, sym="BTC-USD", qty=0.02, px=81_000.0, client_id="steel-43", **kw):
    return Order(venue, sym, "sell", qty, price=px, notional_usd=qty * px, reduce_only=True, client_id=client_id, **kw)


def test_descriptions_and_fence():
    for cls in ALL:
        d = cls.describe()
        assert d["venue"] == "perp" and d["file"] == f"{cls.id}-trade.json" and d["testnet"] and cls.HOST.startswith("https://")
        assert [f["id"] for f in cls.fields][:2] == ["key", "secret"]
        ex = cls(gate("/tmp/steel-no-state", cls.id), key="k", secret="c2VjcmV0" * 11, passphrase="p")
        assert ex.host == cls.HOST and cls(ex.gate, testnet=True, key="k", secret="s").host == cls.TESTNET
        for e in cls.endpoints:
            assert ex.allows(cls.HOST + e.rstrip("*") + ("x" if e.endswith("*") else ""))
        with pytest.raises(PermissionError):
            run(ex._send("GET", cls.HOST + "/api/v3/accounts"))                   # a reader's path is not an order endpoint
    assert {c.id for c in ALL} == {"krakenfutures", "blofin", "deribit", "gateio_perp"}


# Kraken Futures --------------------------------------------------------------------

def test_krakenfutures_mapping_signing_and_dry_run(tmp_path):
    secret = base64.b64encode(bytes(range(64))).decode()
    ex = KrakenFuturesExecutor(gate(tmp_path, "krakenfutures"), key="k", secret=secret)
    assert ex.pair("BTC-USD") == "PF_XBTUSD" and ex.pair("pepe") == "PF_PEPEUSD" and ex.unpair("PF_XBTUSD") == "BTC-USD" and ex.unpair("PI_XBTUSD") is None
    nonce, post = "1415957147987", "orderType=mkt&symbol=PF_XBTUSD&side=buy&size=0.02"
    digest = hashlib.sha256((post + nonce + "/api/v3/sendorder").encode()).digest()
    want = base64.b64encode(hmac.new(base64.b64decode(secret), digest, hashlib.sha512).digest()).decode()
    assert ex.sign("/derivatives/api/v3/sendorder", post, nonce) == want and ex.sign("/api/v3/sendorder", post, nonce) == want
    assert ex.headers("/derivatives/api/v3/sendorder", post, nonce) == {"APIKey": "k", "Authent": want, "Nonce": nonce}
    res = run(ex.place(market_buy("krakenfutures")))
    assert res.ok and res.status == "dry-run" and res.request["method"] == "POST" and res.request["content"] is None
    u = urlsplit(res.request["url"])
    assert u.path == "/derivatives/api/v3/sendorder" and u.netloc == "futures.kraken.com"
    assert parse_qs(u.query) == {"orderType": ["mkt"], "symbol": ["PF_XBTUSD"], "side": ["buy"], "size": ["0.02"], "cliOrdId": ["steel-42"]}
    assert res.request["headers"] == {"APIKey": "***", "Authent": "***", "Nonce": res.request["headers"]["Nonce"]}
    res = run(ex.place(limit_sell("krakenfutures", qty=0.0123456789)))
    q = parse_qs(urlsplit(res.request["url"]).query)
    assert q["orderType"] == ["lmt"] and q["side"] == ["sell"] and q["limitPrice"] == ["81000"] and q["reduceOnly"] == ["true"]
    assert q["size"] == ["0.01234567"]                                               # cut, never rounded up
    assert ex.gate.spent_day[ex.gate.day()] == pytest.approx(0.02 * 80_000 + 0.0123456789 * 81_000)


def test_krakenfutures_answers(tmp_path):
    ex = KrakenFuturesExecutor(gate(tmp_path, "krakenfutures"), key="k", secret=base64.b64encode(b"s" * 32).decode())
    placed = {"result": "success", "sendStatus": {"order_id": "179f9af8-e45e-469d-b3e9-2fd4675cb7d0", "status": "placed", "receivedTime": "2019-09-05T16:33:50.734Z",
              "orderEvents": [{"type": "PLACE", "order": {"orderId": "179f9af8-e45e-469d-b3e9-2fd4675cb7d0", "type": "lmt", "symbol": "PF_XBTUSD", "side": "sell",
                                                            "quantity": 0.02, "filled": 0, "limitPrice": 81000, "reduceOnly": True}}]}, "serverTime": "2019-09-05T16:33:50.734Z"}
    executed = {"result": "success", "sendStatus": {"order_id": "61ca5732-3478-42fe-8362-abbfd9006294", "status": "placed", "receivedTime": "2019-12-11T17:17:33.888Z",
                "orderEvents": [{"type": "EXECUTION", "executionId": "e1ec9f63-2338-4c44-b40a-43486c6732d7", "price": 80010.0, "amount": 0.02, "orderPriorEdit": None,
                                 "orderPriorExecution": {"orderId": "61ca5732-3478-42fe-8362-abbfd9006294", "type": "lmt", "symbol": "PF_XBTUSD", "side": "buy", "quantity": 0.02}}]},
                "serverTime": "2019-12-11T17:17:33.888Z"}
    rejected = {"result": "success", "sendStatus": {"order_id": "614a5298-0071-450f-83c6-0617ce8c6bc4", "status": "insufficientAvailableFunds", "receivedTime": "2019-09-05T16:38:43.343Z",
                "orderEvents": [{"type": "REJECT", "reason": "INSUFFICIENT_MARGIN", "uid": "614a5298-0071-450f-83c6-0617ce8c6bc4"}]}, "serverTime": "2019-09-05T16:38:43.343Z"}
    err = {"result": "error", "error": "apiLimitExceeded", "serverTime": "2019-09-05T16:38:43.343Z", "_status": 200}
    sent = canned(ex, [placed, executed, rejected, err])
    r = run(ex.place(limit_sell("krakenfutures")))
    assert r.ok and r.status == "accepted" and r.order_id == "179f9af8-e45e-469d-b3e9-2fd4675cb7d0" and r.filled_qty == 0 and r.raw is placed
    r = run(ex.place(market_buy("krakenfutures")))
    assert r.ok and r.status == "filled" and r.filled_qty == 0.02 and r.avg_price == 80010.0
    r = run(ex.place(market_buy("krakenfutures")))
    assert not r.ok and r.status == "rejected" and r.error == "insufficientAvailableFunds" and ex.gate.rejects["krakenfutures"]
    ex.gate.rejects.clear()
    r = run(ex.place(market_buy("krakenfutures")))
    assert not r.ok and r.status == "rejected" and r.error == "apiLimitExceeded"
    assert all(s["headers"]["Authent"] == "***" and s["headers"]["APIKey"] == "***" for s in sent)
    canned(ex, [{"result": "success", "cancelStatus": {"status": "cancelled", "order_id": "cb4e34f6-4eb3-4d4b-9724-4c3035b99d47", "receivedTime": "2020-07-22T13:26:20.806Z"}},
                {"result": "success", "cancelStatus": {"status": "notFound", "order_id": "x", "receivedTime": "2020-07-22T13:26:20.806Z"}},
                {"result": "success", "openOrders": [{"order_id": "59302619-41d2-4f0b-941f-7e7914760ad3", "symbol": "PF_XBTUSD", "side": "sell", "orderType": "lmt", "limitPrice": 81000,
                                                      "unfilledSize": 0.015, "filledSize": 0.005, "status": "partiallyFilled", "receivedTime": "2019-09-05T17:01:17.410Z", "reduceOnly": True},
                                                     {"order_id": "0227", "symbol": "PI_XBTUSD", "side": "buy", "orderType": "lmt", "limitPrice": 7200, "unfilledSize": 1501, "filledSize": 0, "status": "untouched"}]},
                {"result": "success", "fills": [{"fill_id": "3d57ed09", "symbol": "PF_XBTUSD", "side": "buy", "order_id": "693af756", "size": 0.02, "price": 80010.0, "fillTime": "2026-10-11T12:00:00.000Z", "fillType": "taker"},
                                                {"fill_id": "56b86ada", "symbol": "PF_ETHUSD", "side": "buy", "order_id": "3f513c4c", "size": 1.0, "price": 2500.0, "fillTime": "2026-10-10T12:00:00.000Z", "fillType": "maker"}]}])
    assert run(ex.cancel("cb4e34f6-4eb3-4d4b-9724-4c3035b99d47", "BTC-USD")).status == "cancelled"
    bad = run(ex.cancel("x", "BTC-USD"))
    assert not bad.ok and bad.error == "notFound"
    oo = run(ex.open_orders())
    assert oo == [{"order_id": "59302619-41d2-4f0b-941f-7e7914760ad3", "symbol": "BTC-USD", "side": "sell", "qty": 0.02, "price": 81000, "filled": 0.005, "status": "partiallyFilled", "client_id": ""}]
    f = run(ex.fills(1_791_720_000.0))                                                # 2026-10-11T12:00Z: the older fill is dropped
    assert f == [{"order_id": "693af756", "symbol": "BTC-USD", "side": "buy", "qty": 0.02, "price": 80010.0, "fee": None, "t": 1_791_720_000.0}]


# BloFin -----------------------------------------------------------------------------

BTC = {"instId": "BTC-USDT", "factor": 1.0, "contract": 0.001, "lot": 0.1, "min": 0.1}
PEPE = {"instId": "1000PEPE-USDT", "factor": 1000.0, "contract": 1.0, "lot": 1.0, "min": 1.0}


def test_blofin_mapping_signing_and_dry_run(tmp_path):
    ex = BloFinExecutor(gate(tmp_path, "blofin"), key="k1", secret="s3cret", passphrase="pp")
    assert ex.inst("BTC-USD") == "BTC-USDT" and ex.symbol("1000PEPE-USDT") == "PEPE-USD" and ex.symbol("BTC-USDT") == "BTC-USD"
    body = '{"a":1}'
    assert ex.sign("POST", "/api/v1/trade/order", "1", "n", body) == base64.b64encode(
        hmac.new(b"s3cret", b'/api/v1/trade/orderPOST1n{"a":1}', hashlib.sha256).hexdigest().encode()).decode()
    h = ex.headers("GET", "/api/v1/trade/orders-pending?limit=100", ts=1597026383085, nonce="7c3a1f1e-0d9b-4c7a-9a2b-1f2e3d4c5b6a")
    assert h["ACCESS-KEY"] == "k1" and h["ACCESS-PASSPHRASE"] == "pp" and h["ACCESS-TIMESTAMP"] == "1597026383085" and h["Content-Type"] == "application/json"
    assert h["ACCESS-SIGN"] == ex.sign("GET", "/api/v1/trade/orders-pending?limit=100", "1597026383085", "7c3a1f1e-0d9b-4c7a-9a2b-1f2e3d4c5b6a")
    r = run(ex.place(market_buy("blofin")))                                            # dry-run cannot fetch the instrument list
    assert not r.ok and r.status == "refused" and "contract value unknown" in r.error and "blofin" not in ex.gate.rejects
    ex.insts.update({"BTC-USD": BTC, "PEPE-USD": PEPE})
    assert ex.contracts("BTC-USD", 0.02) == (BTC, "20") and ex.contracts("BTC-USD", 0.02345) == (BTC, "23.4")
    assert ex.contracts("PEPE-USD", 2_500_000.0) == (PEPE, "2500") and ex.contracts("PEPE-USD", 1999.0)[1] == "1"
    with pytest.raises(ValueError):
        ex.contracts("BTC-USD", 0.00005)
    r = run(ex.place(market_buy("blofin")))
    assert r.ok and r.status == "dry-run" and urlsplit(r.request["url"]).path == "/api/v1/trade/order" and r.request["method"] == "POST"
    assert json.loads(r.request["content"]) == {"instId": "BTC-USDT", "marginMode": "isolated", "positionSide": "net", "side": "buy", "orderType": "market",
                                                "size": "20", "reduceOnly": "false", "clientOrderId": "steel42"}
    assert set(r.request["headers"]) == {"ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-NONCE", "ACCESS-PASSPHRASE", "Content-Type"}
    assert r.request["headers"]["ACCESS-KEY"] == "***" and r.request["headers"]["ACCESS-SIGN"] == "***" and r.request["headers"]["ACCESS-PASSPHRASE"] == "***"
    r = run(ex.place(limit_sell("blofin", sym="PEPE-USD", qty=2_500_000.0, px=0.000004)))
    assert json.loads(r.request["content"]) == {"instId": "1000PEPE-USDT", "marginMode": "isolated", "positionSide": "net", "side": "sell", "orderType": "limit",
                                                "price": "0.004", "size": "2500", "reduceOnly": "true", "clientOrderId": "steel43"}


def test_blofin_answers(tmp_path):
    ex = BloFinExecutor(gate(tmp_path, "blofin"), key="k", secret="s", passphrase="p")
    ex.insts.update({"BTC-USD": BTC, "PEPE-USD": PEPE})
    ok = {"code": "0", "msg": "success", "data": [{"orderId": "28150801", "clientOrderId": "steel42", "msg": "", "code": "0"}], "_status": 200}
    row_err = {"code": "0", "msg": "", "data": [{"orderId": "", "clientOrderId": "steel42", "msg": "Insufficient balance", "code": "152401"}], "_status": 200}
    top_err = {"code": "152002", "msg": "Parameter instId error.", "data": [], "_status": 200}
    canned(ex, [ok, row_err, top_err])
    r = run(ex.place(market_buy("blofin")))
    assert r.ok and r.status == "accepted" and r.order_id == "28150801" and r.filled_qty == 0.0
    r = run(ex.place(market_buy("blofin")))
    assert not r.ok and r.status == "rejected" and r.error == "152401: Insufficient balance"
    ex.gate.rejects.clear()
    r = run(ex.place(market_buy("blofin")))
    assert not r.ok and r.status == "rejected" and r.error == "152002: Parameter instId error."
    sent = canned(ex, [{"code": "0", "msg": "", "data": [{"orderId": "28150801", "clientOrderId": "", "msg": "", "code": "0"}]},
                       {"code": "0", "msg": "", "data": [{"orderId": "28150801", "clientOrderId": "", "msg": "Order does not exist", "code": "1000"}]},
                       {"code": "0", "msg": "", "data": [{"orderId": "29531103", "clientOrderId": "steel43", "instId": "1000PEPE-USDT", "marginMode": "isolated", "positionSide": "net",
                                                          "side": "sell", "orderType": "limit", "price": "0.004", "size": "2500", "filledSize": "500", "averagePrice": "0.004",
                                                          "state": "partially_filled", "reduceOnly": "true", "leverage": "3"}]},
                       {"code": "0", "msg": "", "data": [{"tradeId": "1", "orderId": "28150801", "instId": "BTC-USDT", "side": "buy", "fillPrice": "80010.0", "fillSize": "20",
                                                          "fee": "-0.96", "ts": "1791720000000"}]}])
    assert run(ex.cancel("28150801", "BTC-USD")).status == "cancelled" and json.loads(sent[0]["content"]) == {"instId": "BTC-USDT", "orderId": "28150801"}
    assert run(ex.cancel("28150801", "BTC-USD")).error == "1000: Order does not exist"
    assert run(ex.open_orders("PEPE-USD")) == [{"order_id": "29531103", "symbol": "PEPE-USD", "side": "sell", "qty": 2_500_000.0, "price": 4e-06, "filled": 500_000.0,
                                               "status": "partially_filled", "client_id": "steel43"}]
    assert "instId=1000PEPE-USDT" in sent[2]["url"]
    assert run(ex.fills(1_791_700_000.0)) == [{"order_id": "28150801", "symbol": "BTC-USD", "side": "buy", "qty": 0.02, "price": 80010.0, "fee": -0.96, "t": 1_791_720_000.0}]
    assert "begin=1791700000000" in sent[3]["url"]


# Deribit ----------------------------------------------------------------------------

def test_deribit_mapping_signing_and_dry_run(tmp_path):
    ex = DeribitExecutor(gate(tmp_path, "deribit"), key="AMANDA", secret="AMANDASECRECT")
    assert ex.inst("BTC-USD") == "BTC-PERPETUAL" and ex.inst("SOL-USD") == "SOL_USDC-PERPETUAL" and ex.symbol("1000PEPE_USDC-PERPETUAL") == ("PEPE-USD", 1000.0)
    uri = "/api/v2/private/buy?instrument_name=BTC-PERPETUAL&amount=1600&type=market"
    want = hmac.new(b"AMANDASECRECT", f"1554373534000\nabcd\nGET\n{uri}\n\n".encode(), hashlib.sha256).hexdigest()
    assert ex.sign("GET", uri, "", ts=1554373534000, nonce="abcd") == ("1554373534000", "abcd", want)
    assert ex.headers("GET", uri, ts=1554373534000, nonce="abcd") == {"Authorization": f"deri-hmac-sha256 id=AMANDA,ts=1554373534000,nonce=abcd,sig={want}"}
    r = run(ex.place(market_buy("deribit")))                                           # 0.02 BTC at 80,000 = 1,600 USD = 160 contracts of 10 USD
    assert r.ok and r.status == "dry-run" and r.request["method"] == "GET" and r.request["headers"] == {"Authorization": "***"}
    u = urlsplit(r.request["url"])
    assert u.netloc == "www.deribit.com" and u.path == "/api/v2/private/buy"
    assert parse_qs(u.query) == {"instrument_name": ["BTC-PERPETUAL"], "amount": ["1600"], "type": ["market"], "label": ["steel-42"]}
    r = run(ex.place(limit_sell("deribit", qty=0.0211, px=81_000.0)))                   # 1,709.1 USD cut to 1,700
    u = urlsplit(r.request["url"])
    assert u.path == "/api/v2/private/sell"
    assert parse_qs(u.query) == {"instrument_name": ["BTC-PERPETUAL"], "amount": ["1700"], "type": ["limit"], "price": ["81000"], "post_only": ["false"],
                                 "reduce_only": ["true"], "label": ["steel-43"]}
    r = run(ex.place(market_buy("deribit", sym="ETH-USD", qty=0.0043, px=2500.0)))      # ETH contracts are 1 USD: 10.75 -> 10
    assert parse_qs(urlsplit(r.request["url"]).query)["amount"] == ["10"]
    r = run(ex.place(market_buy("deribit", qty=0.0001, px=80_000.0)))                   # 8 USD: under one contract
    assert not r.ok and r.status == "refused" and "under one 10 USD contract" in r.error and "deribit" not in ex.gate.rejects
    ex.products.update({"SOL-USD": ("SOL_USDC-PERPETUAL", 1.0, 0.001), "PEPE-USD": ("1000PEPE_USDC-PERPETUAL", 1000.0, 1.0)})
    r = run(ex.place(limit_sell("deribit", sym="SOL-USD", qty=12.34567, px=150.0)))
    assert parse_qs(urlsplit(r.request["url"]).query)["amount"] == ["12.345"]
    r = run(ex.place(limit_sell("deribit", sym="PEPE-USD", qty=2_500_999.0, px=0.000004)))
    q = parse_qs(urlsplit(r.request["url"]).query)
    assert q["instrument_name"] == ["1000PEPE_USDC-PERPETUAL"] and q["amount"] == ["2500"] and q["price"] == ["0.004"]
    r = run(ex.place(market_buy("deribit", sym="XRP-USD", qty=3.5, px=2.0)))            # unlisted linear name: the static spelling, the float as is
    assert parse_qs(urlsplit(r.request["url"]).query)["instrument_name"] == ["XRP_USDC-PERPETUAL"] and parse_qs(urlsplit(r.request["url"]).query)["amount"] == ["3.5"]


def test_deribit_answers(tmp_path):
    ex = DeribitExecutor(gate(tmp_path, "deribit"), key="id", secret="sec")
    ex.products["BTC-USD"] = ("BTC-PERPETUAL", 1.0, 10.0)                             # listed: no instrument fetch
    filled = {"jsonrpc": "2.0", "id": 5275, "result": {"trades": [{"trade_seq": 1966056, "trade_id": "ETH-2696083", "timestamp": 1590483938456, "tick_direction": 0, "state": "filled",
                                                                 "reduce_only": False, "price": 80010.0, "post_only": False, "order_type": "market", "order_id": "ETH-584849853",
                                                                 "matching_id": None, "liquidity": "T", "label": "steel-42", "instrument_name": "BTC-PERPETUAL", "fee_currency": "BTC",
                                                                 "fee": 0.0000005, "direction": "buy", "amount": 1600}],
                                                      "order": {"web": False, "time_in_force": "good_til_cancelled", "replaced": False, "reduce_only": False, "profit_loss": 0,
                                                                "price": 80500.0, "post_only": False, "order_type": "market", "order_state": "filled", "order_id": "ETH-584849853",
                                                                "max_show": 1600, "last_update_timestamp": 1590483938456, "label": "steel-42", "is_liquidation": False,
                                                                "instrument_name": "BTC-PERPETUAL", "filled_amount": 1600, "direction": "buy", "creation_timestamp": 1590483938456,
                                                                "commission": 0.0000005, "average_price": 80010.0, "api": True, "amount": 1600}}, "_status": 200}
    opened = {"jsonrpc": "2.0", "id": 1, "result": {"trades": [], "order": {"order_state": "open", "order_id": "ETH-584849900", "instrument_name": "BTC-PERPETUAL", "direction": "sell",
                                                                           "amount": 1600, "filled_amount": 0, "average_price": 0, "price": 81000.0, "label": "steel-43", "reduce_only": True}}, "_status": 200}
    error = {"jsonrpc": "2.0", "id": 2, "error": {"code": 10009, "message": "not_enough_funds", "data": {"reason": "insufficient funds"}}, "testnet": False, "_status": 400}
    canned(ex, [filled, opened, error])
    r = run(ex.place(market_buy("deribit")))
    assert r.ok and r.status == "filled" and r.order_id == "ETH-584849853" and r.filled_qty == pytest.approx(1600 / 80010.0) and r.avg_price == 80010.0
    r = run(ex.place(limit_sell("deribit")))
    assert r.ok and r.status == "accepted" and r.order_id == "ETH-584849900" and r.filled_qty == 0.0 and r.avg_price is None
    r = run(ex.place(market_buy("deribit")))
    assert not r.ok and r.status == "rejected" and r.error == "not_enough_funds (10009, insufficient funds)" and ex.gate.rejects["deribit"]
    sent = canned(ex, [{"jsonrpc": "2.0", "id": 4, "result": {"order_id": "ETH-584849900", "order_state": "cancelled", "cancel_reason": "user_request"}},
                       {"jsonrpc": "2.0", "id": 5, "error": {"code": 11044, "message": "not_open_order"}},
                       {"jsonrpc": "2.0", "id": 6, "result": [{"order_id": "ETH-584849900", "instrument_name": "BTC-PERPETUAL", "direction": "sell", "amount": 1600, "filled_amount": 800,
                                                               "price": 81000.0, "average_price": 81000.0, "order_state": "open", "order_type": "limit", "label": "steel-43", "reduce_only": True},
                                                              {"order_id": "SOL-1", "instrument_name": "1000PEPE_USDC-PERPETUAL", "direction": "buy", "amount": 2500, "filled_amount": 0,
                                                               "price": 0.004, "average_price": 0, "order_state": "open", "order_type": "limit", "label": ""}]},
                       {"jsonrpc": "2.0", "id": 7, "result": {"trades": [{"trade_id": "ETH-2696083", "order_id": "ETH-584849853", "instrument_name": "BTC-PERPETUAL", "direction": "buy",
                                                                           "amount": 1600, "price": 80010.0, "fee": 0.0000005, "fee_currency": "BTC", "timestamp": 1791720000000, "liquidity": "T"}],
                                                              "has_more": False}}])
    assert run(ex.cancel("ETH-584849900", "BTC-USD")).status == "cancelled" and "order_id=ETH-584849900" in sent[0]["url"]
    assert run(ex.cancel("ETH-584849900", "BTC-USD")).error == "not_open_order (11044)"
    oo = run(ex.open_orders())
    assert oo[0] == {"order_id": "ETH-584849900", "symbol": "BTC-USD", "side": "sell", "qty": pytest.approx(1600 / 81000), "price": 81000.0, "filled": pytest.approx(800 / 81000),
                     "status": "open", "client_id": "steel-43"}
    assert oo[1]["symbol"] == "PEPE-USD" and oo[1]["qty"] == 2_500_000.0 and oo[1]["price"] == pytest.approx(4e-06)
    assert parse_qs(urlsplit(sent[2]["url"]).query) == {"currency": ["any"], "kind": ["future"]}
    f = run(ex.fills(1_791_700_000.0))
    assert f == [{"order_id": "ETH-584849853", "symbol": "BTC-USD", "side": "buy", "qty": pytest.approx(1600 / 80010.0), "price": 80010.0, "fee": 0.0000005, "t": 1_791_720_000.0}]
    assert parse_qs(urlsplit(sent[3]["url"]).query) == {"currency": ["any"], "kind": ["future"], "start_timestamp": ["1791700000000"], "count": ["1000"]}


# Gate.io USDT perps ----------------------------------------------------------------

def test_gateio_perp_mapping_published_vectors_and_dry_run(tmp_path, monkeypatch):
    ex = GateIOPerpExecutor(gate(tmp_path, "gateio_perp"), key="key", secret="secret")
    assert ex.contract("BTC-USD") == "BTC_USDT" and ex.symbol("PEPE_USDT") == ("PEPE-USD", 1.0) and ex.symbol("1000LUNC_USDT") == ("LUNC-USD", 1000.0)
    # the two worked examples in the API v4 docs (key "key", secret "secret", timestamp 1541993715)
    assert ex.sign("GET", "/api/v4/futures/orders", "contract=BTC_USD&status=finished&limit=50", ts=1541993715) == \
        "55f84ea195d6fe57ce62464daaa7c3c02fa9d1dde954e4c898289c9a2407a3d6fb3faf24deff16790d726b66ac9f74526668b13bd01029199cc4fcc522418b8a"
    assert ex.sign("POST", "/api/v4/futures/orders", "", '{"contract":"BTC_USD","type":"limit","size":100,"price":6800,"time_in_force":"gtc"}', ts=1541993715) == \
        "eae42da914a590ddf727473aff25fc87d50b64783941061f47a3fdb92742541fc4c2c14017581b4199a1418d54471c269c03a38d788d802e2c306c37636389f0"
    h = ex.headers("POST", "/api/v4/futures/usdt/orders", "", "{}", ts=1541993715)
    assert h == {"KEY": "key", "Timestamp": "1541993715", "SIGN": ex.sign("POST", "/api/v4/futures/usdt/orders", "", "{}", ts=1541993715), "Content-Type": "application/json"}
    r = run(ex.place(market_buy("gateio_perp")))                                       # dry-run cannot fetch the contract
    assert not r.ok and r.status == "refused" and "contract size unknown" in r.error
    ex.contracts.update({"BTC_USDT": {"mult": 0.0001, "min": 1, "max": 12_000_000}, "PEPE_USDT": {"mult": 10_000_000.0, "min": 1, "max": 0}})
    assert ex.size("BTC-USD", 0.02, "buy") == 200 and ex.size("BTC-USD", 0.02345, "sell") == -234 and ex.size("PEPE-USD", 25_000_000.0, "buy") == 2
    with pytest.raises(ValueError):
        ex.size("BTC-USD", 0.00005, "buy")
    with pytest.raises(ValueError):
        ex.size("BTC-USD", 5_000.0, "buy")                                               # over order_size_max
    r = run(ex.place(market_buy("gateio_perp")))
    assert r.ok and r.status == "dry-run" and r.request["method"] == "POST" and r.request["url"] == "https://api.gateio.ws/api/v4/futures/usdt/orders"
    assert json.loads(r.request["content"]) == {"contract": "BTC_USDT", "size": 200, "price": "0", "tif": "ioc", "reduce_only": False, "text": "t-steel-42"}
    assert r.request["headers"] == {"KEY": "***", "Timestamp": r.request["headers"]["Timestamp"], "SIGN": "***", "Content-Type": "application/json"}
    r = run(ex.place(limit_sell("gateio_perp", client_id="")))
    assert json.loads(r.request["content"]) == {"contract": "BTC_USDT", "size": -200, "price": "81000", "tif": "gtc", "reduce_only": True}
    r = run(ex.place(limit_sell("gateio_perp", client_id="a b/c" + "x" * 40)))
    assert json.loads(r.request["content"])["text"] == "t-" + "abc" + "x" * 25
    # the trade key: its own file, else the venue's
    import steel.execution.gateio_perp as mod
    files = {"gateio_perp-trade": {"key": "", "secret": ""}, "gateio-trade": {"key": "shared", "secret": "s"}}
    monkeypatch.setattr(mod, "read_creds", lambda service, fields: dict(files[service]))
    assert GateIOPerpExecutor.load(ex.gate).key == "shared"
    files["gateio_perp-trade"] = {"key": "own", "secret": "o"}
    assert GateIOPerpExecutor.load(ex.gate).key == "own"


def test_gateio_perp_answers(tmp_path):
    ex = GateIOPerpExecutor(gate(tmp_path, "gateio_perp"), key="k", secret="s")
    ex.contracts["BTC_USDT"] = {"mult": 0.0001, "min": 1, "max": 0}
    done = {"id": 15675394, "user": 100000, "contract": "BTC_USDT", "create_time": 1546569968, "size": 200, "iceberg": 0, "left": 0, "price": "0", "fill_price": "80010.5",
            "mkfr": "-0.00025", "tkfr": "0.00075", "tif": "ioc", "refu": 0, "is_reduce_only": False, "is_close": False, "is_liq": False, "text": "t-steel-42", "status": "finished",
            "finish_time": 1546569968, "finish_as": "filled", "stp_id": 0, "stp_act": "-", "amend_text": "-", "_status": 201}
    opened = {"id": 15675395, "contract": "BTC_USDT", "create_time": 1546569969, "size": -200, "left": -200, "price": "81000", "fill_price": "0", "tif": "gtc",
              "is_reduce_only": True, "text": "t-steel-43", "status": "open", "stp_act": "-", "_status": 201}
    nothing = {"id": 15675396, "contract": "BTC_USDT", "size": 200, "left": 200, "price": "0", "fill_price": "0", "tif": "ioc", "status": "finished", "finish_as": "ioc", "_status": 201}
    error = {"label": "INSUFFICIENT_AVAILABLE", "message": "Insufficient available balance", "_status": 400}
    canned(ex, [done, opened, nothing, error])
    r = run(ex.place(market_buy("gateio_perp")))
    assert r.ok and r.status == "filled" and r.order_id == "15675394" and r.filled_qty == pytest.approx(0.02) and r.avg_price == 80010.5
    r = run(ex.place(limit_sell("gateio_perp")))
    assert r.ok and r.status == "accepted" and r.order_id == "15675395" and r.filled_qty == 0.0 and r.avg_price is None
    r = run(ex.place(market_buy("gateio_perp")))
    assert not r.ok and r.status == "rejected" and r.error == "finished: ioc"
    ex.gate.rejects.clear()
    r = run(ex.place(market_buy("gateio_perp")))
    assert not r.ok and r.status == "rejected" and r.error == "INSUFFICIENT_AVAILABLE: Insufficient available balance" and ex.gate.rejects["gateio_perp"]
    sent = canned(ex, [{"id": 15675395, "contract": "BTC_USDT", "size": -200, "left": -200, "status": "finished", "finish_as": "cancelled", "_status": 200},
                       {"label": "ORDER_NOT_FOUND", "message": "order not found", "_status": 404},
                       {"data": [{"id": 15675395, "contract": "BTC_USDT", "size": -200, "left": -150, "price": "81000", "status": "open", "text": "t-steel-43"}], "_status": 200},
                       {"data": [{"id": 121234231, "create_time": 1791720000, "contract": "BTC_USDT", "order_id": "15675394", "size": 200, "price": "80010.5", "role": "taker",
                                  "text": "t-steel-42", "fee": "0.12", "point_fee": "0"}], "_status": 200}])
    assert run(ex.cancel("15675395", "BTC-USD")).status == "cancelled" and sent[0]["method"] == "DELETE" and sent[0]["url"].endswith("/api/v4/futures/usdt/orders/15675395")
    assert run(ex.cancel("15675395", "BTC-USD")).error == "ORDER_NOT_FOUND: order not found"
    assert run(ex.open_orders("BTC-USD")) == [{"order_id": "15675395", "symbol": "BTC-USD", "side": "sell", "qty": pytest.approx(0.02), "price": 81000.0, "filled": pytest.approx(0.005),
                                              "status": "open", "client_id": "t-steel-43"}]
    assert sent[2]["url"].endswith("/api/v4/futures/usdt/orders?status=open&contract=BTC_USDT")
    assert run(ex.fills(1_791_700_000.0)) == [{"order_id": "15675394", "symbol": "BTC-USD", "side": "buy", "qty": pytest.approx(0.02), "price": 80010.5, "fee": 0.12, "t": 1_791_720_000.0}]
    assert sent[3]["url"].endswith("/api/v4/futures/usdt/my_trades_timerange?from=1791700000&limit=100")
