"""Read-only account readers for the perp venues (Kraken Futures, dYdX, Deribit, Aster) and the
CEX trio (Binance, Bybit, OKX): signatures against documented vectors or fixed pre-sign strings,
balance parsing from captured response shapes, asset mappings, `configured` and `describe()`.
No network: the gate's `_json` is replaced by a dispatcher over canned responses."""
import asyncio
import base64
import hashlib
import hmac
from urllib.parse import urlsplit

import pytest

from steel.accounts.aster import AsterAccount
from steel.accounts.binance import BinanceAccount
from steel.accounts.bybit import BybitAccount
from steel.accounts.deribit import DeribitAccount
from steel.accounts.dydx import DydxAccount
from steel.accounts.krakenfutures import KrakenFuturesAccount
from steel.accounts.okx import OKXAccount

ALL = [KrakenFuturesAccount, DydxAccount, DeribitAccount, AsterAccount, BinanceAccount, BybitAccount, OKXAccount]


def canned(acct, monkeypatch, routes):
    """Replace the gate: `routes` maps a path (or prefix) to its JSON, or to an exception to raise.
    Every call must be a GET to a declared endpoint; the calls are returned for inspection."""
    calls = []

    async def fake(method, url, **kw):
        assert method == "GET" and acct.allows(url), url
        calls.append((url, kw))
        path = urlsplit(url).path
        hit = next((v for k, v in routes.items() if path == k or path.startswith(k)), None)
        if isinstance(hit, Exception):
            raise hit
        assert hit is not None, path
        return hit
    monkeypatch.setattr(acct, "_json", fake)
    return calls


def test_describe_fields_endpoints_and_notes():
    for cls in ALL:
        d = cls.describe()
        assert d["id"] == cls.id and d["name"] == cls.label and d["file"] == f"{cls.id}.json" and d["kind"] == "json_multi"
        assert d["url"].startswith("https://") and "read-only" in d["note"] and d["venue"] in ("spot", "perp", "mixed")
        assert [f["id"] for f in d["fields"]] == [f["id"] for f in cls.fields] and cls.endpoints
        a = cls()
        assert not a.configured and a.values() == {f["id"]: "" for f in cls.fields}
        for e in cls.endpoints:
            assert a.allows(f"https://x.test{e.rstrip('*')}") and a.allows(f"https://x.test{e.rstrip('*')}?x=1")
    assert [c.venue for c in ALL] == ["perp", "perp", "perp", "perp", "mixed", "mixed", "mixed"]
    assert [f["id"] for f in DydxAccount.fields] == ["address"] and [f["id"] for f in OKXAccount.fields] == ["key", "secret", "passphrase"]
    assert {c.id for c in ALL} == {"krakenfutures", "dydx", "deribit", "aster", "binance", "bybit", "okx"}


# Kraken Futures ---------------------------------------------------------------------

def test_krakenfutures_signature_and_headers():
    secret = base64.b64encode(bytes(range(64))).decode()
    a = KrakenFuturesAccount(key="k", secret=secret)
    assert a.configured and not KrakenFuturesAccount(key="k").configured
    nonce = "1415957147987"
    digest = hashlib.sha256(("" + nonce + "/api/v3/accounts").encode()).digest()          # postData + nonce + endpointPath, no /derivatives
    want = base64.b64encode(hmac.new(base64.b64decode(secret), digest, hashlib.sha512).digest()).decode()
    assert a.sign("/derivatives/api/v3/accounts", "", nonce) == want and a.sign("/api/v3/accounts", "", nonce) == want
    assert a.sign("/derivatives/api/v3/openpositions", "", nonce) != want
    digest = hashlib.sha256(("symbol=pf_xbtusd" + nonce + "/api/v3/accounts").encode()).digest()
    assert a.sign("/api/v3/accounts", "symbol=pf_xbtusd", nonce) == base64.b64encode(hmac.new(base64.b64decode(secret), digest, hashlib.sha512).digest()).decode()
    assert a.headers("/derivatives/api/v3/accounts", nonce=nonce) == {"APIKey": "k", "Authent": want, "Nonce": nonce}
    assert a.headers("/derivatives/api/v3/accounts")["Nonce"].isdigit()
    assert KrakenFuturesAccount.asset("XBT") == "BTC" and KrakenFuturesAccount.asset("xdg") == "DOGE" and KrakenFuturesAccount.asset("usdt") == "USDT"


def test_krakenfutures_balances(monkeypatch):
    a = KrakenFuturesAccount(key="k", secret=base64.b64encode(b"s" * 32).decode())
    accounts = {"result": "success", "serverTime": "2026-10-11T00:00:00.000Z", "accounts": {
        "cash": {"type": "cashAccount", "balances": {"xbt": 0.01, "eth": 0.0}},
        "fi_xbtusd": {"type": "marginAccount", "currency": "xbt", "balances": {"xbt": 0.0}},
        "flex": {"type": "multiCollateralMarginAccount",
                 "currencies": {"XBT": {"quantity": 0.1185308247, "value": 4998.72, "collateral": 4886.5, "available": 0.1185308247},
                                "USD": {"quantity": 0.0, "value": 0.0, "collateral": 0.0, "available": 0.0},
                                "USDT": {"quantity": 1500.5, "value": 1500.5, "collateral": 1485.0, "available": 1200.0}},
                 "initialMargin": 100.0, "balanceValue": 6499.2, "portfolioValue": 6450.0, "collateralValue": 6371.5, "pnl": -49.2,
                 "unrealizedFunding": 0.3, "totalUnrealized": -48.9, "marginEquity": 6300.0}}}
    positions = {"result": "success", "serverTime": "2026-10-11T00:00:00.000Z", "openPositions": [
        {"side": "short", "symbol": "PF_ETHUSD", "price": 2500.5, "size": 2.0, "unrealizedPnl": -12.5, "unrealizedFunding": 0.1, "maxFixedLeverage": 5},
        {"side": "long", "symbol": "PF_SOLUSD", "price": 110.0, "size": 10.0, "unrealizedPnl": 3.0, "unrealizedFunding": None},
        {"side": "short", "symbol": "PI_XBTUSD", "price": 9392.7, "size": 10000, "unrealizedPnl": -1.0, "unrealizedFunding": None}]}
    calls = canned(a, monkeypatch, {"/derivatives/api/v3/accounts": accounts, "/derivatives/api/v3/openpositions": positions})
    rows = asyncio.run(a.balances())
    assert {(r["asset"], r.get("kind", "spot"), r["qty"]) for r in rows} == {("BTC", "spot", 0.1185308247), ("USDT", "spot", 1500.5), ("BTC", "spot", 0.01),
                                                                           ("ETH", "perp", -2.0), ("SOL", "perp", 10.0)}       # the inverse PI_ one is skipped
    eth = next(r for r in rows if r.get("kind") == "perp" and r["asset"] == "ETH")
    assert eth["entry"] == 2500.5 and eth["upnl"] == -12.5 and eth["leverage"] == 5 and "price" not in eth
    assert [urlsplit(u).path for u, _ in calls] == ["/derivatives/api/v3/accounts", "/derivatives/api/v3/openpositions"]
    assert all(kw["headers"]["APIKey"] == "k" and kw["headers"]["Authent"] and kw["headers"]["Nonce"] for _, kw in calls)
    bad = KrakenFuturesAccount(key="k", secret=base64.b64encode(b"s" * 32).decode())
    canned(bad, monkeypatch, {"/derivatives/api/v3/accounts": {"result": "error", "error": "authenticationError"}})
    with pytest.raises(RuntimeError, match="authenticationError"):
        asyncio.run(bad.balances())


# dYdX -------------------------------------------------------------------------------

def test_dydx_is_address_only(monkeypatch):
    assert not DydxAccount(address="0xabc").configured and not DydxAccount().configured
    a = DydxAccount(address=" dydx1qwerty1234567890asdfghjklzxcvbnm ")
    assert a.configured and a.path() == "/v4/addresses/dydx1qwerty1234567890asdfghjklzxcvbnm"
    assert a.allows(f"{a.HOST}{a.path()}") and not a.allows(f"{a.HOST}/v4/perpetualMarkets")
    body = {"subaccounts": [
        {"address": a.address, "subaccountNumber": 0, "equity": "1510.2", "freeCollateral": "1200.0", "marginEnabled": True,
         "openPerpetualPositions": {"ETH-USD": {"market": "ETH-USD", "status": "OPEN", "side": "SHORT", "size": "0.5", "entryPrice": "2500", "unrealizedPnl": "12.5"},
                                    "BTC-USD": {"market": "BTC-USD", "status": "OPEN", "side": "LONG", "size": "0.01", "entryPrice": "80000", "unrealizedPnl": "-3"}},
         "assetPositions": {"USDC": {"symbol": "USDC", "side": "LONG", "size": "1500.25", "assetId": "0"}}},
        {"subaccountNumber": 1, "openPerpetualPositions": {}, "assetPositions": {"USDC": {"symbol": "USDC", "side": "LONG", "size": "0"}}}]}
    calls = canned(a, monkeypatch, {"/v4/addresses/": body})
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "USDC", "qty": 1500.25, "hold": 0.0, "id": "subaccount 0"}
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["kind"] == "perp" and eth["qty"] == -0.5 and eth["entry"] == 2500.0 and eth["upnl"] == 12.5 and "price" not in eth
    assert next(r for r in rows if r["asset"] == "BTC")["qty"] == 0.01 and len(rows) == 3
    assert calls[0][0] == f"{a.HOST}{a.path()}" and "headers" not in calls[0][1]           # nothing to sign
    missing = DydxAccount(address="dydx1zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    canned(missing, monkeypatch, {"/v4/addresses/": RuntimeError('HTTP 404: {"errors":[{"msg":"No subaccounts found for address dydx1zzz"}]}')})
    with pytest.raises(RuntimeError, match="no subaccounts"):
        asyncio.run(missing.balances())


# Deribit ----------------------------------------------------------------------------

def test_deribit_signature_and_headers():
    a = DeribitAccount(key="AMANDA", secret="AMANDASECRECT")               # the ids from Deribit's docs example
    uri = "/api/v2/private/get_account_summaries?extended=true"
    want = hmac.new(b"AMANDASECRECT", b"1554373534000\nabcd\nGET\n/api/v2/private/get_account_summaries?extended=true\n\n", hashlib.sha256).hexdigest()
    assert a.sign("GET", uri, "", ts=1554373534000, nonce="abcd") == ("1554373534000", "abcd", want)
    assert a.sign("get", uri, "", ts="1554373534000", nonce="abcd")[2] == want
    assert a.headers("GET", uri, ts=1554373534000, nonce="abcd") == {"Authorization": f"deri-hmac-sha256 id=AMANDA,ts=1554373534000,nonce=abcd,sig={want}"}
    ts, nonce, sig = a.sign("GET", uri)
    assert ts.isdigit() and len(nonce) == 16 and sig != want
    assert a.configured and not DeribitAccount(key="x").configured


def test_deribit_balances(monkeypatch):
    a = DeribitAccount(key="id", secret="sec")
    summaries = {"jsonrpc": "2.0", "result": {"id": 1, "summaries": [
        {"currency": "BTC", "equity": 0.5123, "balance": 0.5, "available_funds": 0.4, "margin_balance": 0.51},
        {"currency": "ETH", "equity": 0.0, "balance": 0.0, "available_funds": 0.0},
        {"currency": "USDC", "equity": 2500.0, "balance": 2500.0, "available_funds": 2500.0}]}}
    positions = {"jsonrpc": "2.0", "result": [
        {"instrument_name": "BTC-PERPETUAL", "kind": "future", "direction": "sell", "size": -5000.0, "size_currency": -0.0602396, "average_price": 83000.0,
         "mark_price": 83002.0, "floating_profit_loss": -0.0001, "leverage": 10, "index_price": 82990.0},
        {"instrument_name": "SOL_USDC-PERPETUAL", "kind": "future", "direction": "buy", "size": 12.5, "size_currency": 12.5, "average_price": 110.0,
         "mark_price": 110.25, "floating_profit_loss": 3.125, "leverage": 5},
        {"instrument_name": "1000PEPE_USDC-PERPETUAL", "kind": "future", "direction": "buy", "size": 3.0, "average_price": 0.012, "mark_price": 0.0123,
         "floating_profit_loss": 0.0009, "leverage": 2},
        {"instrument_name": "ETH-26DEC26", "kind": "future", "direction": "zero", "size": 0.0, "size_currency": 0.0, "average_price": 0.0, "mark_price": 2500.0,
         "floating_profit_loss": 0.0, "leverage": 1}]}
    calls = canned(a, monkeypatch, {"/api/v2/private/get_account_summaries": summaries, "/api/v2/private/get_positions": positions})
    rows = asyncio.run(a.balances())
    spot = [r for r in rows if r.get("kind", "spot") == "spot"]
    assert [(r["asset"], r["qty"]) for r in spot] == [("BTC", 0.5123), ("USDC", 2500.0)] and spot[0]["hold"] == pytest.approx(0.1123)
    btc, sol, pepe = [r for r in rows if r.get("kind") == "perp"]
    assert btc["asset"] == "BTC" and btc["qty"] == -0.0602396 and btc["price"] == 83002.0 and btc["entry"] == 83000.0 and btc["leverage"] == 10
    assert btc["upnl"] == pytest.approx(-0.0001 * 83002.0)                                     # inverse P&L is in coins: to USD at the mark
    assert sol["qty"] == 12.5 and sol["price"] == 110.25 and sol["upnl"] == 3.125 and sol["leverage"] == 5
    assert pepe["asset"] == "PEPE" and pepe["qty"] == 3000.0 and pepe["price"] == pytest.approx(0.0000123) and pepe["entry"] == pytest.approx(0.000012)
    assert calls[0][0].endswith("/api/v2/private/get_account_summaries?extended=true") and calls[1][0].endswith("/api/v2/private/get_positions?currency=any&kind=future")
    assert all(kw["headers"]["Authorization"].startswith("deri-hmac-sha256 id=id,ts=") for _, kw in calls)
    bad = DeribitAccount(key="id", secret="sec")
    canned(bad, monkeypatch, {"/api/v2/private/get_account_summaries": {"jsonrpc": "2.0", "error": {"code": 13004, "message": "invalid_credentials"}}})
    with pytest.raises(RuntimeError, match="invalid_credentials"):
        asyncio.run(bad.balances())


# Aster ------------------------------------------------------------------------------

def test_aster_signature_matches_the_documented_example():
    a = AsterAccount(key="dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83", secret="2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9")
    q = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"   # Aster's docs example (their string)
    assert a.sign(q) == "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"
    assert a.query(ts=1591702613943) == "timestamp=1591702613943&recvWindow=5000&signature=" + a.sign("timestamp=1591702613943&recvWindow=5000")
    assert a.query({"symbol": "BTCUSDT"}, ts=1).startswith("symbol=BTCUSDT&timestamp=1&recvWindow=5000&signature=")
    assert AsterAccount.contract("BTCUSDT") == ("BTC", 1.0) and AsterAccount.contract("1000PEPEUSDT") == ("PEPE", 1000.0) and AsterAccount.contract("ETHUSDC") == ("ETH", 1.0)
    assert a.configured and not AsterAccount(key="k").configured


def test_aster_balances(monkeypatch):
    a = AsterAccount(key="k", secret="s")
    balance = [{"accountAlias": "SgsR", "asset": "USDT", "balance": "1226.35137903", "crossWalletBalance": "1200.0", "crossUnPnl": "0.5", "availableBalance": "1100.0",
                "maxWithdrawAmount": "1100.0", "marginAvailable": True, "updateTime": 1},
               {"asset": "BNB", "balance": "0.00000000", "availableBalance": "0"}]
    positions = [{"symbol": "BTCUSDT", "positionAmt": "0.000", "entryPrice": "0.0", "markPrice": "83000", "unRealizedProfit": "0", "leverage": "20", "positionSide": "BOTH"},
                 {"symbol": "ETHUSDT", "positionAmt": "-0.250", "entryPrice": "2510.5", "markPrice": "2505.25", "unRealizedProfit": "1.3125", "leverage": "10", "positionSide": "BOTH"},
                 {"symbol": "1000PEPEUSDT", "positionAmt": "20", "entryPrice": "0.0120", "markPrice": "0.0123", "unRealizedProfit": "0.006", "leverage": "5"}]
    calls = canned(a, monkeypatch, {"/fapi/v2/balance": balance, "/fapi/v2/positionRisk": positions})
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "USDT", "qty": 1226.35137903, "hold": pytest.approx(126.35137903), "id": "futures"}
    eth, pepe = [r for r in rows if r.get("kind") == "perp"]
    assert eth["asset"] == "ETH" and eth["qty"] == -0.25 and eth["price"] == 2505.25 and eth["entry"] == 2510.5 and eth["leverage"] == 10 and eth["upnl"] == 1.3125
    assert pepe["asset"] == "PEPE" and pepe["qty"] == 20000.0 and pepe["price"] == pytest.approx(0.0000123) and len(rows) == 3
    for url, kw in calls:
        assert kw["headers"] == {"X-MBX-APIKEY": "k"} and "timestamp=" in url and "recvWindow=5000&signature=" in url


# Binance ----------------------------------------------------------------------------

def test_binance_signature_matches_the_documented_example(monkeypatch):
    monkeypatch.delenv("STEEL_BINANCE_HOST", raising=False)
    a = BinanceAccount(key="vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A", secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"   # Binance's docs example (their string)
    assert a.sign(q) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert a.host == "api.binance.com" and a.HOST == "https://api.binance.com" and a.futures and a.configured
    assert a.query({"omitZeroBalances": "true"}, ts=1499827319559).startswith("omitZeroBalances=true&timestamp=1499827319559&recvWindow=5000&signature=")
    monkeypatch.setenv("STEEL_BINANCE_HOST", "api.binance.us")
    us = BinanceAccount(key="k", secret="s")
    assert us.HOST == "https://api.binance.us" and not us.futures
    assert BinanceAccount.contract("1000SHIBUSDT") == ("SHIB", 1000.0) and BinanceAccount.contract("BTCUSDC") == ("BTC", 1.0)


def test_binance_balances_spot_and_futures(monkeypatch):
    monkeypatch.delenv("STEEL_BINANCE_HOST", raising=False)
    a = BinanceAccount(key="k", secret="s")
    spot = {"makerCommission": 10, "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}, {"asset": "USDT", "free": "100", "locked": "0"}, {"asset": "LDO", "free": "0", "locked": "0"}]}
    wallet = [{"accountAlias": "x", "asset": "USDT", "balance": "500.0", "crossWalletBalance": "500", "crossUnPnl": "-2.5", "availableBalance": "450.0"},
              {"asset": "BNB", "balance": "0", "availableBalance": "0"}]
    risk = [{"symbol": "SOLUSDT", "positionAmt": "-10", "entryPrice": "112.0", "markPrice": "110.5", "unRealizedProfit": "15.0", "leverage": "3", "positionSide": "BOTH"}]
    calls = canned(a, monkeypatch, {"/api/v3/account": spot, "/fapi/v2/balance": wallet, "/fapi/v2/positionRisk": risk})
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "BTC", "qty": 0.6, "hold": 0.1, "id": "spot"} and rows[1] == {"asset": "USDT", "qty": 100.0, "hold": 0.0, "id": "spot"}
    assert rows[2] == {"asset": "USDT", "qty": 500.0, "hold": 50.0, "id": "futures"}
    sol = rows[3]
    assert sol["kind"] == "perp" and sol["qty"] == -10.0 and sol["price"] == 110.5 and sol["entry"] == 112.0 and sol["leverage"] == 3 and sol["upnl"] == 15.0 and len(rows) == 4
    assert calls[0][0].startswith("https://api.binance.com/api/v3/account?omitZeroBalances=true&timestamp=")
    assert calls[1][0].startswith("https://fapi.binance.com/fapi/v2/balance?timestamp=") and calls[2][0].startswith("https://fapi.binance.com/fapi/v2/positionRisk?")
    assert all(kw["headers"] == {"X-MBX-APIKEY": "k"} for _, kw in calls)
    b = BinanceAccount(key="k", secret="s")                                     # a key without the futures permission keeps its spot rows
    canned(b, monkeypatch, {"/api/v3/account": spot, "/fapi/v2/balance": RuntimeError('HTTP 401: {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')})
    assert [r["asset"] for r in asyncio.run(b.balances())] == ["BTC", "USDT"] and "2015" in b.futures_note
    monkeypatch.setenv("STEEL_BINANCE_HOST", "api.binance.us")                  # Binance.US: spot only, no omitZeroBalances parameter
    us = BinanceAccount(key="k", secret="s")
    calls = canned(us, monkeypatch, {"/api/v3/account": spot})
    assert len(asyncio.run(us.balances())) == 2 and len(calls) == 1 and calls[0][0].startswith("https://api.binance.us/api/v3/account?timestamp=")


# Bybit ------------------------------------------------------------------------------

def test_bybit_pre_sign_string_and_headers():
    a = BybitAccount(key="XXXXXXXXXX", secret="secret")
    q = "category=option&symbol=BTC-29JUL22-25000-C"                             # the query from Bybit's docs example
    want = hmac.new(b"secret", b"1658384314791XXXXXXXXXX5000category=option&symbol=BTC-29JUL22-25000-C", hashlib.sha256).hexdigest()
    assert a.sign(1658384314791, q) == want and a.sign("1658384314791", q) == want
    assert a.headers(q, ts=1658384314791) == {"X-BAPI-API-KEY": "XXXXXXXXXX", "X-BAPI-TIMESTAMP": "1658384314791", "X-BAPI-RECV-WINDOW": "5000",
                                              "X-BAPI-SIGN-TYPE": "2", "X-BAPI-SIGN": want}
    assert a.headers(q)["X-BAPI-TIMESTAMP"].isdigit()
    assert BybitAccount.contract("BTCUSDT") == ("BTC", 1.0) and BybitAccount.contract("BTCPERP") == ("BTC", 1.0) and BybitAccount.contract("1000PEPEUSDT") == ("PEPE", 1000.0)
    assert a.configured and not BybitAccount(secret="s").configured


def test_bybit_balances_and_position_pages(monkeypatch):
    a = BybitAccount(key="k", secret="s")
    wallet = {"retCode": 0, "retMsg": "OK", "result": {"list": [{"totalEquity": "3.31", "accountType": "UNIFIED", "coin": [
        {"coin": "BTC", "equity": "0.02", "walletBalance": "0.02", "locked": "0.005", "unrealisedPnl": "0"},
        {"coin": "USDT", "equity": "1000", "walletBalance": "1000", "locked": "0", "unrealisedPnl": "0"},
        {"coin": "ETH", "equity": "0", "walletBalance": "0", "locked": "0", "unrealisedPnl": "0"}]}]}, "time": 1}
    pages = iter([
        {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "side": "Sell", "size": "4", "avgPrice": "112", "markPrice": "110.5", "unrealisedPnl": "6", "leverage": "2"}], "nextPageCursor": "abc"}},
        {"retCode": 0, "result": {"list": [{"symbol": "1000PEPEUSDT", "side": "Buy", "size": "50", "avgPrice": "0.012", "markPrice": "0.0123", "unrealisedPnl": "0.015", "leverage": "5"}], "nextPageCursor": ""}},
        {"retCode": 0, "result": {"list": [{"symbol": "BTCPERP", "side": "Buy", "size": "0.01", "avgPrice": "80000", "markPrice": "83000", "unrealisedPnl": "30", "leverage": "10"}], "nextPageCursor": ""}}])
    urls = []

    async def fake(method, url, **kw):
        urls.append(url)
        assert method == "GET" and a.allows(url) and kw["headers"]["X-BAPI-SIGN-TYPE"] == "2" and kw["headers"]["X-BAPI-API-KEY"] == "k"
        return wallet if "/wallet-balance" in url else next(pages)
    monkeypatch.setattr(a, "_json", fake)
    rows = asyncio.run(a.balances())
    assert rows[:2] == [{"asset": "BTC", "qty": 0.02, "hold": 0.005, "id": "unified"}, {"asset": "USDT", "qty": 1000.0, "hold": 0.0, "id": "unified"}]
    sol, pepe, btc = rows[2:]
    assert sol["qty"] == -4.0 and sol["price"] == 110.5 and sol["entry"] == 112.0 and sol["leverage"] == 2 and sol["upnl"] == 6.0 and sol["kind"] == "perp"
    assert pepe["asset"] == "PEPE" and pepe["qty"] == 50000.0 and pepe["price"] == pytest.approx(0.0000123) and btc["asset"] == "BTC" and btc["qty"] == 0.01
    assert "accountType=UNIFIED" in urls[0] and "settleCoin=USDT" in urls[1] and "cursor=abc" in urls[2] and "settleCoin=USDC" in urls[3] and len(urls) == 4
    bad = BybitAccount(key="k", secret="s")
    canned(bad, monkeypatch, {"/v5/account/wallet-balance": {"retCode": 10003, "retMsg": "API key is invalid.", "result": {}}})
    with pytest.raises(RuntimeError, match="API key is invalid"):
        asyncio.run(bad.balances())


# OKX --------------------------------------------------------------------------------

def test_okx_stamp_signature_and_headers():
    a = OKXAccount(key="k", secret="22582BD0CFF14C41EDBF1AB98506286D", passphrase="pp")        # the sample secret format from OKX's docs
    assert OKXAccount.stamp(1607418537.715) == "2020-12-08T09:08:57.715Z" and OKXAccount.stamp().endswith("Z")
    ts, path = "2020-12-08T09:08:57.715Z", "/api/v5/account/balance?ccy=BTC"
    want = base64.b64encode(hmac.new(b"22582BD0CFF14C41EDBF1AB98506286D", (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    assert a.sign(ts, "GET", path) == want and a.sign(ts, "get", path) == want and a.sign(ts, "GET", path, "{}") != want
    assert a.headers("GET", path, ts=ts) == {"OK-ACCESS-KEY": "k", "OK-ACCESS-SIGN": want, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": "pp",
                                             "Content-Type": "application/json"}
    assert a.configured and not OKXAccount(key="k", secret="s").configured      # the passphrase is a required field


def test_okx_balances(monkeypatch):
    a = OKXAccount(key="k", secret="s", passphrase="p")
    balance = {"code": "0", "msg": "", "data": [{"totalEq": "1000", "details": [{"ccy": "BTC", "eq": "0.5", "cashBal": "0.4", "frozenBal": "0.1", "availBal": "0.3"},
                                                                                {"ccy": "USDT", "eq": "0", "cashBal": "0", "frozenBal": "0"}]}]}
    funding = {"code": "0", "data": [{"ccy": "ETH", "bal": "2.5", "frozenBal": "0.5", "availBal": "2"}]}
    positions = {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "pos": "-3", "posSide": "net", "avgPx": "80000", "markPx": "83000", "upl": "-90", "lever": "5", "notionalUsd": "2490"},
        {"instId": "ETH-USD-SWAP", "pos": "10", "posSide": "long", "avgPx": "2400", "markPx": "2500", "upl": "0.4", "lever": "3", "notionalUsd": "1000"},
        {"instId": "SOL-USDT-SWAP", "pos": "0", "posSide": "net", "avgPx": "", "markPx": "110", "upl": "0", "lever": "1", "notionalUsd": "0"}]}
    calls = canned(a, monkeypatch, {"/api/v5/account/balance": balance, "/api/v5/asset/balances": funding, "/api/v5/account/positions": positions})
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "BTC", "qty": 0.4, "hold": 0.1, "id": "trading"} and rows[1] == {"asset": "ETH", "qty": 2.5, "hold": 0.5, "id": "funding"}
    btc, eth = rows[2:]
    assert btc["asset"] == "BTC" and btc["kind"] == "perp" and btc["qty"] == pytest.approx(-0.03) and btc["price"] == 83000.0 and btc["entry"] == 80000.0
    assert btc["leverage"] == 5 and btc["upnl"] == -90.0 and eth["asset"] == "ETH" and eth["qty"] == pytest.approx(0.4) and len(rows) == 4
    for url, kw in calls:
        h = kw["headers"]
        assert h["OK-ACCESS-KEY"] == "k" and h["OK-ACCESS-PASSPHRASE"] == "p" and h["OK-ACCESS-TIMESTAMP"].endswith("Z") and h["OK-ACCESS-SIGN"]
    bad = OKXAccount(key="k", secret="s", passphrase="p")
    canned(bad, monkeypatch, {"/api/v5/account/balance": {"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []}})
    with pytest.raises(RuntimeError, match="Invalid OK-ACCESS-KEY"):
        asyncio.run(bad.balances())
