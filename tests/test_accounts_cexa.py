"""Bitget, KuCoin, Gate.io and MEXC read-only account readers: the signatures (the pre-sign
string at a fixed timestamp, checked against an independent HMAC; MEXC's documented vector),
the header sets, balance parsing from captured response shapes, asset codes, `configured`,
`describe()` and the endpoint gate. No network: `_json` is stubbed per test."""
import asyncio
import base64
import hashlib
import hmac
import re

import pytest

from steel.accounts.bitget import ASSETS, MIX_ACCOUNTS, POSITIONS, BitgetAccount
from steel.accounts.gateio import FUT_ACCOUNT, FUT_POSITIONS, SPOT, GateIOAccount
from steel.accounts.kucoin import ACCOUNTS, OVERVIEW, KuCoinAccount
from steel.accounts.mexc import ACCOUNT, MEXCAccount

READ = re.compile(r"account|balance|asset|position|fee", re.I)        # a fee-tier read counts: it is what the account is charged


def stub(acct, routes):
    """Replace the gate with a dispatcher on URL substrings; records every call's URL and headers."""
    calls = []

    async def fake(method, url, **kw):
        calls.append((method, url, kw.get("headers") or {}))
        for key, body in routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected {url}")
    acct._json = fake
    return calls


def test_describe_configured_and_the_gate():
    for cls, n in ((BitgetAccount, 3), (KuCoinAccount, 3), (GateIOAccount, 2), (MEXCAccount, 2)):
        d = cls.describe()
        assert d["id"] == cls.id and d["name"] == cls.label and d["file"] == f"{cls.id}.json" and d["kind"] == "json_multi" and d["url"].startswith("https://")
        assert "read-only" in d["note"] and cls.label in d["note"] and len(d["fields"]) == n and [f["id"] for f in d["fields"]][:2] == ["key", "secret"]
        assert cls.venue in ("spot", "mixed")
        empty = cls()
        assert not empty.configured and set(empty.values()) == {f["id"] for f in cls.fields}
        full = cls(**{f["id"]: f"x-{f['id']}" for f in cls.fields})
        assert full.configured
        for e in cls.endpoints:
            assert READ.search(e) and full.allows(f"{cls.HOST}{e}?x=1")
        assert not full.allows(f"{cls.HOST}/api/v3/nope") and not full.allows(f"{cls.HOST}/api/v2/spot/nope")
        with pytest.raises(PermissionError):
            asyncio.run(full._request("GET", f"{cls.HOST}/api/v2/spot/nope"))
    k = KuCoinAccount()
    k.configure({"key": " k ", "secret": "s", "passphrase": ""})
    assert k.key == "k" and not k.configured
    k.configure({"passphrase": "p"})
    assert k.configured and k.values() == {"key": "k", "secret": "s", "passphrase": "p"}


# Bitget -------------------------------------------------------------------------------

def test_bitget_signature_and_headers():
    a = BitgetAccount(key="k", secret="s3cret", passphrase="pass")
    ts = 1791676800000
    want = base64.b64encode(hmac.new(b"s3cret", f"{ts}GET{MIX_ACCOUNTS}?productType=USDT-FUTURES".encode(), hashlib.sha256).digest()).decode()
    assert a.sign(ts, "get", MIX_ACCOUNTS, "productType=USDT-FUTURES") == want
    assert a.sign(ts, "GET", ASSETS) == base64.b64encode(hmac.new(b"s3cret", f"{ts}GET{ASSETS}".encode(), hashlib.sha256).digest()).decode()   # no "?" without a query
    h = a.headers("GET", MIX_ACCOUNTS, "productType=USDT-FUTURES", ts=ts)
    assert h == {"ACCESS-KEY": "k", "ACCESS-SIGN": want, "ACCESS-TIMESTAMP": "1791676800000", "ACCESS-PASSPHRASE": "pass", "locale": "en-US",
                 "Content-Type": "application/json"}
    assert a.headers("GET", ASSETS)["ACCESS-TIMESTAMP"].isdigit() and len(a.headers("GET", ASSETS)["ACCESS-TIMESTAMP"]) == 13


def test_bitget_balances_spot_futures_and_positions():
    a = BitgetAccount(key="k", secret="s", passphrase="p")
    calls = stub(a, {
        ASSETS: {"code": "00000", "msg": "success", "data": [{"coin": "BTC", "available": "0.5", "frozen": "0.1", "locked": "0", "limitAvailable": "0"},
                                                             {"coin": "USDT", "available": "100", "frozen": "0", "locked": "0"},
                                                             {"coin": "DUST", "available": "0", "frozen": "0", "locked": "0"}]},
        MIX_ACCOUNTS: {"code": "00000", "data": [{"marginCoin": "USDT", "available": "900", "locked": "100", "accountEquity": "1050.5", "unrealizedPL": "50.5"}]},
        POSITIONS: {"code": "00000", "data": [{"symbol": "ETHUSDT", "marginCoin": "USDT", "holdSide": "short", "total": "2", "openPriceAvg": "2600", "markPrice": "2500",
                                               "unrealizedPL": "200", "leverage": "3"},
                                              {"symbol": "1000PEPEUSDT", "holdSide": "long", "total": "10", "openPriceAvg": "0.01", "markPrice": "0.012", "unrealizedPL": "0.2", "leverage": "2"},
                                              {"symbol": "SOLUSDT", "holdSide": "long", "total": "0", "openPriceAvg": "0", "markPrice": "110", "leverage": "1"}]},
    })
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "BTC", "qty": 0.6, "hold": 0.1, "id": "bitget"} and rows[1]["asset"] == "USDT" and rows[1]["qty"] == 100.0
    assert rows[2] == {"asset": "USDT", "qty": 1050.5, "hold": 100.0, "id": "bitget-futures"}
    eth = rows[3]
    assert eth["asset"] == "ETH" and eth["kind"] == "perp" and eth["qty"] == -2.0 and eth["price"] == 2500.0 and eth["entry"] == 2600.0 and eth["leverage"] == 3.0 and eth["upnl"] == 200.0
    pepe = rows[4]
    assert pepe["asset"] == "PEPE" and pepe["qty"] == 10_000.0 and pepe["price"] == pytest.approx(0.000012) and pepe["entry"] == pytest.approx(0.00001)
    assert len(rows) == 5                                                             # the dust and the empty position are skipped
    assert [u.split("?")[0].split(".com")[1] for _, u, _ in calls] == [ASSETS, MIX_ACCOUNTS, POSITIONS] and calls[1][1].endswith("?productType=USDT-FUTURES")
    assert all(set(h) >= {"ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE"} and m == "GET" for m, _, h in calls)
    stub(a, {ASSETS: {"code": "00000", "data": [{"coin": "BTC", "available": "1", "frozen": "0", "locked": "0"}]}, "/mix/": RuntimeError("HTTP 400: 40014 no futures permission")})
    assert [r["asset"] for r in asyncio.run(a.balances())] == ["BTC"]               # futures failing still shows spot
    stub(a, {ASSETS: {"code": "40037", "msg": "Apikey does not exist", "data": None}})
    with pytest.raises(RuntimeError, match="40037"):
        asyncio.run(a.balances())


# KuCoin -------------------------------------------------------------------------------

def test_kucoin_signature_passphrase_and_headers():
    a = KuCoinAccount(key="k", secret="s3cret", passphrase="pass")
    ts = 1791676800000
    endpoint = f"{OVERVIEW}?currency=USDT"
    want = base64.b64encode(hmac.new(b"s3cret", f"{ts}GET{endpoint}".encode(), hashlib.sha256).digest()).decode()
    assert a.sign(ts, "get", endpoint) == want
    h = a.headers("GET", endpoint, ts=ts)
    assert h["KC-API-KEY"] == "k" and h["KC-API-SIGN"] == want and h["KC-API-TIMESTAMP"] == "1791676800000" and h["KC-API-KEY-VERSION"] == "2"
    assert h["KC-API-PASSPHRASE"] == base64.b64encode(hmac.new(b"s3cret", b"pass", hashlib.sha256).digest()).decode()     # v2: the passphrase is signed too
    assert KuCoinAccount.asset("XBT") == "BTC" and KuCoinAccount.asset("eth") == "ETH" and KuCoinAccount.asset("") == ""


def test_kucoin_balances_sum_account_types_and_add_futures_equity():
    a = KuCoinAccount(key="k", secret="s", passphrase="p")
    calls = stub(a, {
        ACCOUNTS: {"code": "200000", "data": [{"id": "5bd6e9286d99522a52e458de", "currency": "BTC", "type": "main", "balance": "0.4", "available": "0.3", "holds": "0.1"},
                                              {"id": "5bd6e9216d99522a52e458d6", "currency": "BTC", "type": "trade", "balance": "0.2", "available": "0.2", "holds": "0"},
                                              {"id": "x", "currency": "USDT", "type": "trade", "balance": "0", "available": "0", "holds": "0"},
                                              {"id": "y", "currency": "USDT", "type": "main", "balance": "25.5", "available": "25.5", "holds": "0"}]},
        OVERVIEW: {"code": "200000", "data": {"accountEquity": 1000.5, "unrealisedPNL": 0.5, "marginBalance": 1000, "positionMargin": 100, "orderMargin": 0,
                                              "frozenFunds": 0, "availableBalance": 900, "currency": "USDT"}},
    })
    rows = asyncio.run(a.balances())
    assert rows == [{"asset": "BTC", "qty": pytest.approx(0.6), "hold": 0.1, "id": "****58de"}, {"asset": "USDT", "qty": 25.5, "hold": 0.0, "id": "****"},
                    {"asset": "USDT", "qty": 1000.5, "hold": 100.5, "id": "kucoin-futures"}]
    assert calls[0][1] == f"https://api.kucoin.com{ACCOUNTS}" and calls[1][1] == f"https://api-futures.kucoin.com{OVERVIEW}?currency=USDT"
    stub(a, {ACCOUNTS: {"code": "200000", "data": [{"id": "z", "currency": "ETH", "type": "main", "balance": "1", "holds": "0"}]}, OVERVIEW: RuntimeError("HTTP 401")})
    assert [r["asset"] for r in asyncio.run(a.balances())] == ["ETH"]
    stub(a, {ACCOUNTS: {"code": "400003", "msg": "The API key does not exist or site mismatch."}})
    with pytest.raises(RuntimeError, match="400003"):
        asyncio.run(a.balances())


# Gate.io ------------------------------------------------------------------------------

def test_gateio_signature_string_and_headers():
    a = GateIOAccount(key="key", secret="secret")
    ts = 1541993715
    msg = "\n".join(["GET", SPOT, "", hashlib.sha512(b"").hexdigest(), str(ts)])                      # METHOD, path, query, sha512(body), timestamp
    assert a.sign("get", SPOT, ts=ts) == hmac.new(b"secret", msg.encode(), hashlib.sha512).hexdigest()
    with_query = "\n".join(["GET", FUT_POSITIONS, "holding=true", hashlib.sha512(b"").hexdigest(), str(ts)])
    assert a.sign("GET", FUT_POSITIONS, "holding=true", ts=ts) == hmac.new(b"secret", with_query.encode(), hashlib.sha512).hexdigest()
    h = a.headers("GET", SPOT, ts=ts)
    assert h == {"KEY": "key", "Timestamp": "1541993715", "SIGN": a.sign("GET", SPOT, ts=ts), "Content-Type": "application/json"}
    assert len(a.headers("GET", SPOT)["Timestamp"]) == 10                                               # seconds, not milliseconds


def test_gateio_balances_and_positions_from_value_over_mark():
    a = GateIOAccount(key="k", secret="s")
    calls = stub(a, {
        SPOT: [{"currency": "BTC", "available": "0.5", "locked": "0.25"}, {"currency": "USDT", "available": "0", "locked": "0"}],
        FUT_ACCOUNT: {"total": "1000", "unrealised_pnl": "-20", "position_margin": "150", "order_margin": "0", "available": "800", "point": "0", "currency": "USDT"},
        FUT_POSITIONS: [{"user": 1, "contract": "BTC_USDT", "size": -5, "leverage": "10", "value": "41.52", "entry_price": "80000", "mark_price": "83040",
                         "unrealised_pnl": "-1.5", "mode": "single"},
                        {"user": 1, "contract": "ETH_USDT", "size": 0, "leverage": "0", "value": "0", "entry_price": "0", "mark_price": "2500", "unrealised_pnl": "0"},
                        {"user": 1, "contract": "1000SHIB_USDT", "size": 3, "leverage": "0", "value": "30", "entry_price": "0.01", "mark_price": "0.012", "unrealised_pnl": "5"}],
    })
    rows = asyncio.run(a.balances())
    assert rows[0] == {"asset": "BTC", "qty": 0.75, "hold": 0.25, "id": "gateio"}
    assert rows[1] == {"asset": "USDT", "qty": 980.0, "hold": 200.0, "id": "gateio-futures"}
    btc = rows[2]
    assert btc["asset"] == "BTC" and btc["kind"] == "perp" and btc["qty"] == pytest.approx(-0.0005) and btc["price"] == 83040.0 and btc["entry"] == 80000.0
    assert btc["leverage"] == 10.0 and btc["upnl"] == -1.5
    shib = rows[3]
    assert shib["asset"] == "SHIB" and shib["qty"] == pytest.approx(2_500_000.0) and shib["price"] == pytest.approx(0.000012) and shib["leverage"] == 0.0   # 0 is cross
    assert len(rows) == 4 and [u.replace("https://api.gateio.ws", "") for _, u, _ in calls] == [SPOT, FUT_ACCOUNT, FUT_POSITIONS]
    assert all(set(h) >= {"KEY", "Timestamp", "SIGN"} for _, _, h in calls)
    stub(a, {SPOT: [{"currency": "ETH", "available": "1", "locked": "0"}], "/futures/": RuntimeError("HTTP 401: {\"label\":\"INVALID_KEY\"}")})
    assert [r["asset"] for r in asyncio.run(a.balances())] == ["ETH"]


# MEXC ---------------------------------------------------------------------------------

def test_mexc_signature_and_query():
    a = MEXCAccount(key="mx0aBYs33eIilxBWC5", secret="45d0b3c26f2644f19bfb98b07741b2f5")
    q = "timestamp=1644489390087&recvWindow=5000"
    assert a.sign(q) == hmac.new(b"45d0b3c26f2644f19bfb98b07741b2f5", q.encode(), hashlib.sha256).hexdigest()
    assert a.query(ts=1644489390087) == f"{q}&signature={a.sign(q)}" and re.fullmatch(r"[0-9a-f]{64}", a.sign(q))
    assert a.query().startswith("timestamp=") and "&recvWindow=5000&signature=" in a.query()


def test_mexc_balances():
    a = MEXCAccount(key="k", secret="s")
    calls = stub(a, {ACCOUNT: {"canTrade": True, "accountType": "SPOT", "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}, {"asset": "ZERO", "free": "0", "locked": "0"},
                                                                                     {"asset": "usdt", "free": "12.5", "locked": "0"}], "permissions": ["SPOT"]}})
    rows = asyncio.run(a.balances())
    assert rows == [{"asset": "BTC", "qty": 0.6, "hold": 0.1, "id": "mexc"}, {"asset": "USDT", "qty": 12.5, "hold": 0.0, "id": "mexc"}]
    m, url, h = calls[0]
    assert m == "GET" and url.startswith(f"https://api.mexc.com{ACCOUNT}?timestamp=") and "&signature=" in url and h == {"X-MEXC-APIKEY": "k"}
    stub(a, {ACCOUNT: {"code": 10072, "msg": "Api key info invalid"}})
    with pytest.raises(RuntimeError, match="10072"):
        asyncio.run(a.balances())
