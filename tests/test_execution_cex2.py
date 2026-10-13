"""Coinbase, Kraken, KuCoin and Gate.io spot execution clients: the symbol mapping, the signed
request of a market buy and a limit sell in dry-run (exact path, body, header names, secrets
scrubbed), the documented signature vectors (Kraken's AddOrder, Gate.io's GET and POST), the
answer mapping from captured successes and rejections, and the endpoint gate. No network:
answers are canned and the dry-run `_send` never connects."""
import asyncio
import base64
import hashlib
import hmac
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from steel.config import Config
from steel.execution import ExecutionGate, Order
from steel.execution.coinbase import CANCEL as CB_CANCEL, FILLS as CB_FILLS, OPEN as CB_OPEN, ORDERS as CB_ORDERS, CoinbaseExecutor
from steel.execution.gateio import OPEN as GT_OPEN, ORDERS as GT_ORDERS, TRADES as GT_TRADES, GateIOExecutor
from steel.execution.kraken import ADD, CANCEL as KR_CANCEL, OPEN as KR_OPEN, TRADES as KR_TRADES, KrakenExecutor
from steel.execution.kucoin import ACTIVE, ACTIVE_SYMBOLS, FILLS as KC_FILLS, ORDERS as KC_ORDERS, KuCoinExecutor
from steel.sources.base import _iso_ts


def gate(tmp_path, venue):
    c = Config()
    c.execution, c.execution_venues = True, venue
    c.exec_max_order_usd, c.exec_max_name_usd, c.exec_max_venue_usd, c.exec_max_day_usd = 1_000.0, 2_500.0, 3_000.0, 5_000.0
    c.exec_max_exposure_frac, c.exec_max_leverage, c.exec_reject_cooldown_s = 0.5, 2.0, 300.0
    return ExecutionGate(c, tmp_path)


def order(venue, side="buy", qty=0.001, price=None, client_id="t1", sym="BTC-USD"):
    return Order(venue, sym, side, qty, price=price, kind="spot", notional_usd=100.0, client_id=client_id)


def canned(ex, answers: dict):
    """A `_send` that still refuses undeclared paths, records every request and answers by the
    first key found in the URL (a literal from the venue)."""
    calls = []
    ex.dry_run = False

    async def fake(method, url, *, params=None, data=None, json_body=None, headers=None, content=None, timeout=15.0):
        assert ex.allows(url), url
        req = {"method": method, "url": url, "params": params, "data": data, "json": json_body, "content": content, "headers": ex._scrub(headers)}
        calls.append(req)
        for k, v in answers.items():
            if k in url:
                return req, dict(v)
        raise AssertionError(url)
    ex._send = fake
    return calls


def test_describe_and_the_gate_refuses_an_unarmed_venue(tmp_path):
    for cls, n in ((CoinbaseExecutor, 2), (KrakenExecutor, 2), (KuCoinExecutor, 3), (GateIOExecutor, 2)):
        d = cls.describe()
        assert d["id"] == cls.id and d["file"] == f"{cls.id}-trade.json" and d["venue"] == "spot" and len(d["fields"]) == n
        bare = cls(gate(tmp_path, cls.id))
        assert not bare.configured and all(bare.allows(f"{cls.HOST}{e[:-1] + 'x' if e.endswith('*') else e}?y=1") for e in cls.endpoints)
    assert CoinbaseExecutor.describe()["testnet"] and KuCoinExecutor.describe()["testnet"]
    assert not KrakenExecutor.describe()["testnet"] and not GateIOExecutor.describe()["testnet"]
    ex = KrakenExecutor(gate(tmp_path, "coinbase"), key="k", secret=base64.b64encode(b"s").decode())
    res = asyncio.run(ex.place(order("kraken")))
    assert res.status == "refused" and "not armed" in res.error


# Coinbase -----------------------------------------------------------------------------

def _pem():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    return key.public_key(), pem


def test_coinbase_dry_run_requests_and_jwt(tmp_path):
    pub, pem = _pem()
    ex = CoinbaseExecutor(gate(tmp_path, "coinbase"), name="organizations/abc/apiKeys/def", privateKey=pem.replace("\n", "\\n"))
    assert ex.configured and ex.privateKey.startswith("-----BEGIN EC PRIVATE KEY-----\n")
    assert CoinbaseExecutor.product("btc") == "BTC-USD" and CoinbaseExecutor.product("ETH-USD") == "ETH-USD"
    res = asyncio.run(ex.place(order("coinbase")))
    req = res.request
    assert res.ok and res.status == "dry-run" and req["method"] == "POST" and req["url"] == f"https://api.coinbase.com{CB_ORDERS}"
    assert req["json"] == {"client_order_id": "t1", "product_id": "BTC-USD", "side": "BUY", "order_configuration": {"market_market_ioc": {"base_size": "0.001"}}}
    assert req["headers"] == {"Authorization": "***", "Content-Type": "application/json"} and "Bearer" not in json.dumps(req)
    res = asyncio.run(ex.place(order("coinbase", "sell", 0.5, 70000.5, client_id="")))
    body = res.request["json"]
    assert body["side"] == "SELL" and body["order_configuration"] == {"limit_limit_gtc": {"base_size": "0.5", "limit_price": "70000.5", "post_only": False}}
    assert len(body["client_order_id"]) == 36                                              # the venue requires one: minted
    tok = ex.headers("POST", CB_ORDERS, now=time.time())["Authorization"].split(" ", 1)[1]
    claims = pyjwt.decode(tok, pub, algorithms=["ES256"])
    assert claims["uri"] == "POST api.coinbase.com/api/v3/brokerage/orders" and claims["sub"] == ex.name and claims["exp"] - claims["nbf"] == 120
    assert pyjwt.get_unverified_header(tok)["kid"] == ex.name
    sandbox = CoinbaseExecutor(gate(tmp_path, "coinbase"), testnet=True, name="n", privateKey=pem)
    assert sandbox.host == "https://api-sandbox.coinbase.com"
    assert asyncio.run(sandbox.place(order("coinbase"))).request["url"].startswith("https://api-sandbox.coinbase.com/")
    with pytest.raises(PermissionError):
        asyncio.run(ex._send("POST", f"{ex.HOST}/api/v3/brokerage/orders/preview", json_body={}))
    with pytest.raises(PermissionError):
        asyncio.run(ex._send("GET", f"{ex.HOST}/api/v3/brokerage/accounts"))


def test_coinbase_answers(tmp_path):
    _, pem = _pem()
    g = gate(tmp_path, "coinbase")
    ex = CoinbaseExecutor(g, name="n", privateKey=pem)
    calls = canned(ex, {CB_CANCEL: {"results": [{"success": True, "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON", "order_id": "11111-00000-000000"}], "_status": 200},
                        CB_ORDERS: {"success": True, "success_response": {"order_id": "11111-00000-000000", "product_id": "BTC-USD", "side": "BUY", "client_order_id": "t1"},
                                    "order_configuration": {"market_market_ioc": {"base_size": "0.001"}}, "_status": 200}})
    res = asyncio.run(ex.place(order("coinbase")))
    assert res.ok and res.status == "accepted" and res.order_id == "11111-00000-000000" and res.raw["success"]
    res = asyncio.run(ex.cancel("11111-00000-000000", "BTC-USD"))
    assert res.ok and res.status == "accepted" and calls[-1]["json"] == {"order_ids": ["11111-00000-000000"]} and calls[-1]["url"].endswith(CB_CANCEL)
    canned(ex, {CB_CANCEL: {"results": [{"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER", "order_id": "nope"}], "_status": 200},
                CB_ORDERS: {"success": False, "error_response": {"error": "INSUFFICIENT_FUND", "message": "Insufficient balance in source account", "error_details": "",
                                                                 "preview_failure_reason": "PREVIEW_INSUFFICIENT_FUND", "new_order_failure_reason": "INSUFFICIENT_FUND"}, "_status": 200}})
    res = asyncio.run(ex.place(order("coinbase")))
    assert not res.ok and res.status == "rejected" and "Insufficient balance" in res.error and "coinbase" in g.rejects
    res = asyncio.run(ex.cancel("nope", "BTC-USD"))
    assert not res.ok and res.status == "rejected" and res.error == "UNKNOWN_CANCEL_ORDER"
    canned(ex, {CB_ORDERS: {"error": "unauthorized", "message": "Unauthorized", "_status": 401}})
    g.rejects.clear()
    assert asyncio.run(ex.place(order("coinbase"))).error == "Unauthorized"
    calls = canned(ex, {CB_OPEN: {"orders": [{"order_id": "ff6e8dad-22c4-422d-bbf2-3c55b70e42d2", "product_id": "BTC-USD",
                                              "order_configuration": {"limit_limit_gtc": {"base_size": "1", "limit_price": "70000.44", "post_only": False}},
                                              "side": "SELL", "client_order_id": "spot_order_2", "status": "OPEN", "created_time": "2024-06-28T15:16:47.696748Z",
                                              "filled_size": "0", "average_filled_price": "0"}], "has_next": False, "cursor": ""},
                        CB_FILLS: {"fills": [{"entry_id": "f8b3aa2b", "trade_id": "d006d1e0-cf64-4cdf-8fe1-8441221cd96f", "order_id": "37f17b89-cd9e-41eb-9187-2df6aa72f895",
                                              "trade_time": "2024-06-27T20:32:06.568973Z", "trade_type": "FILL", "price": "70000.44", "size": "0.00137199",
                                              "commission": "0.3841596147024", "product_id": "BTC-USD", "side": "SELL", "liquidity_indicator": "MAKER"}], "cursor": ""}})
    rows = asyncio.run(ex.open_orders("BTC-USD"))
    assert rows == [{"order_id": "ff6e8dad-22c4-422d-bbf2-3c55b70e42d2", "symbol": "BTC-USD", "side": "sell", "qty": 1.0, "price": 70000.44, "filled": 0.0, "status": "open"}]
    assert calls[-1]["method"] == "GET" and calls[-1]["params"] == {"order_status": "OPEN", "limit": 250, "product_ids": "BTC-USD"}
    fills = asyncio.run(ex.fills(_iso_ts("2026-10-01T00:00:00Z")))
    assert fills == [{"order_id": "37f17b89-cd9e-41eb-9187-2df6aa72f895", "symbol": "BTC-USD", "side": "sell", "qty": 0.00137199, "price": 70000.44,
                      "fee": 0.3841596147024, "t": _iso_ts("2024-06-27T20:32:06.568973Z")}]
    assert calls[-1]["params"] == {"start_sequence_timestamp": "2026-10-01T00:00:00Z", "limit": 250}


# Kraken -------------------------------------------------------------------------------

KR_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="


def test_kraken_mapping_vector_and_dry_run(tmp_path):
    p, s = KrakenExecutor.pair, KrakenExecutor.symbol_of
    assert p("BTC-USD") == "XBTUSD" and p("doge") == "XDGUSD" and p("ETH-USD") == "ETHUSD" and p("SOL-USD") == "SOLUSD"
    assert s("XBTUSD") == "BTC-USD" and s("XXBTZUSD") == "BTC-USD" and s("XXDGZUSD") == "DOGE-USD" and s("XETHZUSD") == "ETH-USD" and s("SOLUSD") == "SOL-USD"
    assert s("XRPUSD") == "XRP-USD" and s("XBT/USD") == "BTC-USD"
    ex = KrakenExecutor(gate(tmp_path, "kraken"), key="key", secret=KR_SECRET)
    data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD", "price": 37500, "type": "buy", "volume": 1.25}   # the docs' AddOrder example
    assert ex.sign(ADD, data) == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    assert ex.headers(ADD, data) == {"API-Key": "key", "API-Sign": ex.sign(ADD, data)}
    ex.nonce = lambda: "1616492376594"
    res = asyncio.run(ex.place(order("kraken")))
    req = res.request
    assert res.status == "dry-run" and req["method"] == "POST" and req["url"] == f"https://api.kraken.com{ADD}" and req["json"] is None
    assert req["data"] == {"nonce": "1616492376594", "ordertype": "market", "type": "buy", "volume": "0.001", "pair": "XBTUSD", "cl_ord_id": "t1"}
    assert req["headers"] == {"API-Key": "***", "API-Sign": "***"}
    res = asyncio.run(ex.place(order("kraken", "sell", 1.25, 37500, client_id="a-ticket-id-that-is-too-long")))
    assert res.request["data"] == {"nonce": "1616492376594", "ordertype": "limit", "type": "sell", "volume": "1.25", "pair": "XBTUSD", "price": "37500"}
    uuid = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
    assert asyncio.run(ex.place(order("kraken", client_id=uuid))).request["data"]["cl_ord_id"] == uuid
    with pytest.raises(PermissionError):
        asyncio.run(ex._send("POST", f"{ex.HOST}/0/private/Balance", data={}))
    with pytest.raises(PermissionError):
        asyncio.run(ex._send("POST", f"{ex.HOST}/0/private/EditOrder", data={}))


def test_kraken_answers(tmp_path):
    g = gate(tmp_path, "kraken")
    ex = KrakenExecutor(g, key="key", secret=KR_SECRET)
    ex.nonce = lambda: "1616492376594"
    calls = canned(ex, {ADD: {"error": [], "result": {"descr": {"order": "buy 0.00100000 XBTUSD @ market"}, "txid": ["OUF4EM-FRGI2-MQMWZD"]}, "_status": 200},
                        KR_CANCEL: {"error": [], "result": {"count": 1, "pending": False}, "_status": 200}})
    res = asyncio.run(ex.place(order("kraken")))
    assert res.ok and res.status == "accepted" and res.order_id == "OUF4EM-FRGI2-MQMWZD"
    res = asyncio.run(ex.cancel("OUF4EM-FRGI2-MQMWZD", "BTC-USD"))
    assert res.ok and calls[-1]["data"] == {"nonce": "1616492376594", "txid": "OUF4EM-FRGI2-MQMWZD"} and calls[-1]["url"].endswith(KR_CANCEL)
    canned(ex, {ADD: {"error": ["EOrder:Insufficient funds"], "_status": 200}, KR_CANCEL: {"error": ["EOrder:Unknown order"], "_status": 200}})
    res = asyncio.run(ex.place(order("kraken")))
    assert not res.ok and res.status == "rejected" and res.error == "EOrder:Insufficient funds" and "kraken" in g.rejects
    assert asyncio.run(ex.cancel("x", "BTC-USD")).error == "EOrder:Unknown order"
    calls = canned(ex, {KR_OPEN: {"error": [], "result": {"open": {"OQCLML-BW3P3-BUCMWZ": {
        "refid": None, "userref": 0, "status": "open", "opentm": 1688666559.8974, "starttm": 0, "expiretm": 0,
        "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit", "price": "27500.0", "price2": "0", "leverage": "none", "order": "buy 1.25000000 XBTUSD @ limit 27500.0", "close": ""},
        "vol": "1.25000000", "vol_exec": "0.37500000", "cost": "10292.3", "fee": "0", "price": "27446.1", "stopprice": "0", "limitprice": "0", "misc": "", "oflags": "fciq"}}}},
                        KR_TRADES: {"error": [], "result": {"trades": {"THVRQM-33VKH-UCI7BS": {
                            "ordertxid": "OQCLML-BW3P3-BUCMWZ", "postxid": "TKH2SE-M7IF5-CFI7LT", "pair": "XXBTZUSD", "time": 1688667796.8802, "type": "buy", "ordertype": "limit",
                            "price": "30010.00000", "cost": "600.20000", "fee": "0.00000", "vol": "0.02000000", "margin": "0.00000", "misc": "", "trade_id": 39482674, "maker": True}}, "count": 1}}})
    rows = asyncio.run(ex.open_orders())
    assert rows == [{"order_id": "OQCLML-BW3P3-BUCMWZ", "symbol": "BTC-USD", "side": "buy", "qty": 1.25, "price": 27500.0, "filled": 0.375, "status": "open"}]
    assert asyncio.run(ex.open_orders("ETH-USD")) == [] and calls[-1]["data"] == {"nonce": "1616492376594"}
    fills = asyncio.run(ex.fills(1688660000.5))
    assert fills == [{"order_id": "OQCLML-BW3P3-BUCMWZ", "symbol": "BTC-USD", "side": "buy", "qty": 0.02, "price": 30010.0, "fee": 0.0, "t": 1688667796.8802}]
    assert calls[-1]["data"] == {"nonce": "1616492376594", "start": 1688660000}


# KuCoin -------------------------------------------------------------------------------

def test_kucoin_signing_and_dry_run(tmp_path):
    assert KuCoinExecutor.pair("BTC-USD") == "BTC-USDT" and KuCoinExecutor.pair("pepe") == "PEPE-USDT"
    ex = KuCoinExecutor(gate(tmp_path, "kucoin"), key="k", secret="s3cret", passphrase="pass")
    ts, content = 1791676800000, '{"clientOid":"t1","side":"buy","symbol":"BTC-USDT","type":"market","size":"0.001"}'
    want = base64.b64encode(hmac.new(b"s3cret", f"{ts}POST{KC_ORDERS}{content}".encode(), hashlib.sha256).digest()).decode()
    assert ex.sign(ts, "post", KC_ORDERS, content) == want
    h = ex.headers("POST", KC_ORDERS, content, ts=ts)
    assert h["KC-API-SIGN"] == want and h["KC-API-KEY"] == "k" and h["KC-API-TIMESTAMP"] == "1791676800000" and h["KC-API-KEY-VERSION"] == "2"
    assert h["KC-API-PASSPHRASE"] == base64.b64encode(hmac.new(b"s3cret", b"pass", hashlib.sha256).digest()).decode()
    signed = []
    orig = ex.headers
    ex.headers = lambda m, e, body="", ts=None: signed.append((m, e, body)) or orig(m, e, body, ts)
    res = asyncio.run(ex.place(order("kucoin")))
    req = res.request
    assert res.status == "dry-run" and req["method"] == "POST" and req["url"] == f"https://api.kucoin.com{KC_ORDERS}" and req["content"] == content
    assert req["headers"] == {"KC-API-KEY": "***", "KC-API-SIGN": "***", "KC-API-TIMESTAMP": h["KC-API-TIMESTAMP"][:0] + req["headers"]["KC-API-TIMESTAMP"],
                              "KC-API-PASSPHRASE": "***", "KC-API-KEY-VERSION": "2", "Content-Type": "application/json"}
    assert signed[-1] == ("POST", KC_ORDERS, content) and ex.traded == {"BTC-USD"}
    res = asyncio.run(ex.place(order("kucoin", "sell", 0.5, 70000.5, client_id="")))
    body = json.loads(res.request["content"])
    assert body["type"] == "limit" and body["price"] == "70000.5" and body["size"] == "0.5" and body["timeInForce"] == "GTC" and len(body["clientOid"]) == 32
    res = asyncio.run(ex.cancel("671124f9365ccb00073debd4", "BTC-USD"))
    assert res.status == "dry-run" and res.request["method"] == "DELETE" and res.request["content"] is None
    assert res.request["url"] == "https://api.kucoin.com/api/v1/hf/orders/671124f9365ccb00073debd4?symbol=BTC-USDT"
    assert signed[-1] == ("DELETE", "/api/v1/hf/orders/671124f9365ccb00073debd4?symbol=BTC-USDT", "")      # the query is in the signed string
    assert KuCoinExecutor(gate(tmp_path, "kucoin"), testnet=True).host == "https://openapi-sandbox.kucoin.com"
    for path in ("/api/v1/orders", "/api/v1/accounts", "/api/v1/hf/margin/orders"):
        with pytest.raises(PermissionError):
            asyncio.run(ex._send("POST", f"{ex.HOST}{path}", content="{}"))


def test_kucoin_answers(tmp_path):
    g = gate(tmp_path, "kucoin")
    ex = KuCoinExecutor(g, key="k", secret="s", passphrase="p")
    calls = canned(ex, {"/hf/orders/671124f9365ccb00073debd4": {"code": "200000", "data": {"orderId": "671124f9365ccb00073debd4"}, "_status": 200},
                        KC_ORDERS: {"code": "200000", "data": {"orderId": "5bd6e9286d99522a52e458de", "clientOid": "t1"}, "_status": 200}})
    res = asyncio.run(ex.place(order("kucoin")))
    assert res.ok and res.status == "accepted" and res.order_id == "5bd6e9286d99522a52e458de"
    res = asyncio.run(ex.cancel("671124f9365ccb00073debd4", "BTC-USD"))
    assert res.ok and res.order_id == "671124f9365ccb00073debd4" and calls[-1]["method"] == "DELETE"
    canned(ex, {"/hf/orders/x": {"code": "400100", "msg": "order_not_exist_or_not_allow_to_cancel", "_status": 200},
                KC_ORDERS: {"code": "200004", "msg": "Balance insufficient!", "_status": 200}})
    res = asyncio.run(ex.place(order("kucoin")))
    assert not res.ok and res.status == "rejected" and res.error == "200004: Balance insufficient!" and "kucoin" in g.rejects
    assert asyncio.run(ex.cancel("x", "BTC-USD")).error == "400100: order_not_exist_or_not_allow_to_cancel"
    row = {"id": "5c9e2f1a", "symbol": "BTC-USDT", "opType": "DEAL", "type": "limit", "side": "buy", "price": "60000", "size": "0.001", "funds": "0", "dealSize": "0",
           "dealFunds": "0", "fee": "0", "feeCurrency": "USDT", "timeInForce": "GTC", "postOnly": False, "cancelExist": False, "clientOid": "t1",
           "createdAt": 1729577515473, "tradeType": "TRADE", "inOrderBook": True, "remainSize": "0.001", "active": True}
    fill = {"id": 19814995255305, "orderId": "5bd6e9286d99522a52e458de", "counterOrderId": "x", "tradeId": 11029373945659392, "symbol": "BTC-USDT", "side": "buy",
            "liquidity": "taker", "type": "limit", "forceTaker": False, "price": "67717.6", "size": "0.00001", "funds": "0.677176", "fee": "0.000677176",
            "feeRate": "0.001", "feeCurrency": "USDT", "stop": "", "tradeType": "TRADE", "createdAt": 1729577515473}
    calls = canned(ex, {ACTIVE_SYMBOLS: {"code": "200000", "data": {"symbols": ["BTC-USDT"]}}, ACTIVE: {"code": "200000", "data": [row]},
                        KC_FILLS: {"code": "200000", "data": {"items": [fill], "lastId": 19814995255305}}})
    rows = asyncio.run(ex.open_orders())
    assert rows == [{"order_id": "5c9e2f1a", "symbol": "BTC-USD", "side": "buy", "qty": 0.001, "price": 60000.0, "filled": 0.0, "status": "open"}]
    assert [c["url"] for c in calls] == [f"{ex.HOST}{ACTIVE_SYMBOLS}", f"{ex.HOST}{ACTIVE}?symbol=BTC-USDT"]
    assert asyncio.run(ex.open_orders("BTC-USD")) == rows and calls[-1]["url"].endswith("?symbol=BTC-USDT")
    fills = asyncio.run(ex.fills(1729577000.0))
    assert fills == [{"order_id": "5bd6e9286d99522a52e458de", "symbol": "BTC-USD", "side": "buy", "qty": 0.00001, "price": 67717.6, "fee": 0.000677176, "t": 1729577515.473}]
    assert calls[-1]["url"] == f"{ex.HOST}{KC_FILLS}?symbol=BTC-USDT&startAt=1729577000000&limit=100"


# Gate.io ------------------------------------------------------------------------------

def test_gateio_vectors_and_dry_run(tmp_path):
    assert GateIOExecutor.pair("BTC-USD") == "BTC_USDT" and GateIOExecutor.text("abc") == "t-abc"
    assert GateIOExecutor.text("ticket 1/2:" + "x" * 40) == "t-ticket12" + "x" * 20 and GateIOExecutor.text("").startswith("t-") and len(GateIOExecutor.text("")) == 26
    ex = GateIOExecutor(gate(tmp_path, "gateio"), key="key", secret="secret")
    ts = 1541993715                                                                           # the docs' worked examples
    assert ex.sign("GET", "/api/v4/futures/orders", "contract=BTC_USD&status=finished&limit=50", "", ts) == \
        "55f84ea195d6fe57ce62464daaa7c3c02fa9d1dde954e4c898289c9a2407a3d6fb3faf24deff16790d726b66ac9f74526668b13bd01029199cc4fcc522418b8a"
    body = '{"contract":"BTC_USD","type":"limit","size":100,"price":6800,"time_in_force":"gtc"}'
    assert ex.sign("POST", "/api/v4/futures/orders", "", body, ts) == \
        "eae42da914a590ddf727473aff25fc87d50b64783941061f47a3fdb92742541fc4c2c14017581b4199a1418d54471c269c03a38d788d802e2c306c37636389f0"
    assert ex.headers("POST", GT_ORDERS, "", body, ts) == {"KEY": "key", "Timestamp": "1541993715", "SIGN": ex.sign("POST", GT_ORDERS, "", body, ts), "Content-Type": "application/json"}
    signed = []
    orig = ex.headers
    ex.headers = lambda m, p, q="", body="", ts=None: signed.append((m, p, q, body)) or orig(m, p, q, body, ts)
    res = asyncio.run(ex.place(order("gateio")))                                                 # a market buy spends the ticket's notional of USDT
    req = res.request
    assert res.status == "dry-run" and req["method"] == "POST" and req["url"] == f"https://api.gateio.ws{GT_ORDERS}"
    assert req["content"] == '{"text":"t-t1","currency_pair":"BTC_USDT","account":"spot","side":"buy","type":"market","time_in_force":"ioc","amount":"100"}'
    assert req["headers"] == {"KEY": "***", "Timestamp": req["headers"]["Timestamp"], "SIGN": "***", "Content-Type": "application/json"} and req["headers"]["Timestamp"].isdigit()
    assert signed[-1] == ("POST", GT_ORDERS, "", req["content"])
    assert json.loads(asyncio.run(ex.place(order("gateio", "sell"))).request["content"])["amount"] == "0.001"   # a market sell is in base
    body = json.loads(asyncio.run(ex.place(order("gateio", "sell", 0.5, 70000.5))).request["content"])
    assert body == {"text": "t-t1", "currency_pair": "BTC_USDT", "account": "spot", "side": "sell", "type": "limit", "time_in_force": "gtc", "amount": "0.5", "price": "70000.5"}
    res = asyncio.run(ex.cancel("1852454420", "BTC-USD"))
    assert res.status == "dry-run" and res.request["method"] == "DELETE" and res.request["url"] == "https://api.gateio.ws/api/v4/spot/orders/1852454420?currency_pair=BTC_USDT"
    assert signed[-1] == ("DELETE", "/api/v4/spot/orders/1852454420", "currency_pair=BTC_USDT", "")
    assert GateIOExecutor(gate(tmp_path, "gateio"), testnet=True).host == ex.HOST                  # no spot testnet
    for path in ("/api/v4/spot/accounts", "/api/v4/futures/usdt/orders", "/api/v4/spot/batch_orders"):
        with pytest.raises(PermissionError):
            asyncio.run(ex._send("POST", f"{ex.HOST}{path}", content="{}"))


def test_gateio_answers(tmp_path):
    g = gate(tmp_path, "gateio")
    ex = GateIOExecutor(g, key="k", secret="s")
    done = {"id": "1852454420", "text": "t-t1", "create_time": "1710488334", "update_time": "1710488334", "create_time_ms": 1710488334073, "status": "closed",
            "currency_pair": "BTC_USDT", "type": "limit", "account": "spot", "side": "buy", "amount": "0.001", "price": "65000", "time_in_force": "gtc", "iceberg": "0",
            "left": "0", "filled_total": "64.5", "avg_deal_price": "64500", "fee": "0.000001", "fee_currency": "BTC", "point_fee": "0", "gt_fee": "0", "gt_discount": False,
            "rebated_fee": "0", "rebated_fee_currency": "USDT", "finish_as": "filled", "_status": 201}
    canned(ex, {GT_ORDERS: done})
    res = asyncio.run(ex.place(order("gateio", price=65000)))
    assert res.ok and res.status == "filled" and res.order_id == "1852454420" and res.filled_qty == 0.001 and res.avg_price == 64500.0
    canned(ex, {GT_ORDERS: done | {"type": "market", "time_in_force": "ioc", "amount": "100", "left": "0", "filled_total": "100"}})
    res = asyncio.run(ex.place(order("gateio")))
    assert res.status == "filled" and res.filled_qty == pytest.approx(100 / 64500)                 # a market buy's amount is quote: base = total / average
    canned(ex, {GT_ORDERS: done | {"status": "open", "left": "0.001", "filled_total": "0", "avg_deal_price": "0", "finish_as": "open"}})
    res = asyncio.run(ex.place(order("gateio", price=60000)))
    assert res.status == "accepted" and res.filled_qty == 0.0 and res.avg_price is None
    canned(ex, {GT_ORDERS: {"label": "BALANCE_NOT_ENOUGH", "message": "Not enough balance", "_status": 400}})
    res = asyncio.run(ex.place(order("gateio")))
    assert not res.ok and res.status == "rejected" and res.error == "BALANCE_NOT_ENOUGH: Not enough balance" and "gateio" in g.rejects
    calls = canned(ex, {"/spot/orders/1852454420": done | {"status": "cancelled", "finish_as": "cancelled", "left": "0.001", "_status": 200}})
    res = asyncio.run(ex.cancel("1852454420", "BTC-USD"))
    assert res.ok and res.status == "accepted" and calls[-1]["url"].endswith("/spot/orders/1852454420?currency_pair=BTC_USTD".replace("USTD", "USDT"))
    canned(ex, {"/spot/orders/x": {"label": "ORDER_NOT_FOUND", "message": "order not found", "_status": 404}})
    assert asyncio.run(ex.cancel("x", "BTC-USD")).error == "ORDER_NOT_FOUND: order not found"
    o = {"id": "12332324", "text": "t-123456", "create_time": "1548000000", "status": "open", "currency_pair": "BTC_USDT", "type": "limit", "account": "spot",
         "side": "buy", "amount": "1", "price": "5.00032", "time_in_force": "gtc", "left": "0.5", "filled_total": "2.50016", "fee": "0.005", "fee_currency": "ETH"}
    calls = canned(ex, {GT_OPEN: {"data": [{"currency_pair": "BTC_USDT", "total": 1, "orders": [o]},
                                           {"currency_pair": "SOL_USDT", "total": 1, "orders": [o | {"id": "999", "currency_pair": "SOL_USDT", "side": "sell", "amount": "2", "left": "2", "price": "150"}]}], "_status": 200},
                        GT_TRADES: {"data": [{"id": "1232893232", "create_time": "1548000000", "create_time_ms": "1548000000123.456", "currency_pair": "BTC_USDT", "side": "buy",
                                              "role": "maker", "amount": "0.001", "price": "65000", "order_id": "232893232", "fee": "0.0000005", "fee_currency": "BTC",
                                              "point_fee": "0", "gt_fee": "0", "text": "t-t1"}], "_status": 200}})
    rows = asyncio.run(ex.open_orders())
    assert rows == [{"order_id": "12332324", "symbol": "BTC-USD", "side": "buy", "qty": 1.0, "price": 5.00032, "filled": 0.5, "status": "open"},
                    {"order_id": "999", "symbol": "SOL-USD", "side": "sell", "qty": 2.0, "price": 150.0, "filled": 0.0, "status": "open"}]
    assert [r["symbol"] for r in asyncio.run(ex.open_orders("SOL-USD"))] == ["SOL-USD"] and calls[-1]["url"] == f"{ex.HOST}{GT_OPEN}?account=spot"
    fills = asyncio.run(ex.fills(1548000000.9))
    assert fills == [{"order_id": "232893232", "symbol": "BTC-USD", "side": "buy", "qty": 0.001, "price": 65000.0, "fee": 5e-7, "t": 1548000000.123456}]
    assert calls[-1]["url"] == f"{ex.HOST}{GT_TRADES}?account=spot&from=1548000000&limit=1000"
