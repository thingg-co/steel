"""HTX, Crypto.com, Bitfinex, Gemini and Bitstamp readers: signatures against fixed vectors,
balance parsing from captured response shapes, asset codes, `configured`, `describe()` and the
gate. No network."""
import asyncio
import base64
import json
import re

import pytest

from steel.accounts.bitfinex import BitfinexAccount
from steel.accounts.bitstamp import BitstampAccount
from steel.accounts.cryptocom import CryptoComAccount
from steel.accounts.gemini import GeminiAccount
from steel.accounts.htx import HTXAccount

READERS = [HTXAccount, CryptoComAccount, BitfinexAccount, GeminiAccount, BitstampAccount]


def canned(acct, responses):
    """Replace the gate's JSON call with canned answers keyed by a path fragment; records calls."""
    calls = []

    async def fake(method, url, **kw):
        calls.append((method, url, kw))
        for frag, body in responses.items():
            if frag in url:
                return body
        raise AssertionError(url)
    acct._json = fake
    return calls


def test_htx_signature_version_2():
    a = HTXAccount("e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx", "secret")
    p = a.params("2017-05-11T15:19:30")
    assert HTXAccount.canonical("GET", "/v1/account/accounts", p) == \
        "GET\napi.huobi.pro\n/v1/account/accounts\nAccessKeyId=e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx&SignatureMethod=HmacSHA256&SignatureVersion=2&Timestamp=2017-05-11T15%3A19%3A30"
    assert a.sign("GET", "/v1/account/accounts", p) == "fNzfF9N0eQRsGTFTk856iT2mcoK3el67WZrRtYQo/5w="
    q = a.signed("GET", "/v1/account/accounts", "2017-05-11T15:19:30")
    assert q["Signature"] == "fNzfF9N0eQRsGTFTk856iT2mcoK3el67WZrRtYQo/5w=" and q["Timestamp"] == "2017-05-11T15:19:30" and q["SignatureVersion"] == "2"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", a.params()["Timestamp"])


def test_htx_balances_read_the_working_spot_account():
    a = HTXAccount("k", "s")
    calls = canned(a, {"/10000001/balance": {"status": "ok", "data": {"id": 10000001, "type": "spot", "state": "working", "list": [
                           {"currency": "usdt", "type": "trade", "balance": "91.850043797676510303", "seq-num": "477"},
                           {"currency": "usdt", "type": "frozen", "balance": "5.160000000000000015", "seq-num": "477"},
                           {"currency": "btc", "type": "trade", "balance": "0.5", "seq-num": "2"},
                           {"currency": "eth", "type": "trade", "balance": "0", "seq-num": "1"},
                           {"currency": "sol", "type": "loan", "balance": "3", "seq-num": "1"}]}},
                       "/v1/account/accounts": {"status": "ok", "data": [{"id": 10000001, "type": "spot", "subtype": "", "state": "working"},
                                                                        {"id": 10000002, "type": "otc", "subtype": "", "state": "working"},
                                                                        {"id": 10000003, "type": "spot", "subtype": "", "state": "lock"}]}})
    rows = asyncio.run(a.balances())
    assert [u for _, u, _ in calls] == ["https://api.huobi.pro/v1/account/accounts", "https://api.huobi.pro/v1/account/accounts/10000001/balance"]
    assert all(m == "GET" and "Signature" in kw["params"] for m, _, kw in calls)
    assert sorted(rows, key=lambda r: r["asset"]) == [{"asset": "BTC", "qty": 0.5, "hold": 0.0, "id": "****0001"},
                                                      {"asset": "USDT", "qty": pytest.approx(97.01), "hold": pytest.approx(5.16), "id": "****0001"}]
    canned(a, {"/v1/account/accounts": {"status": "error", "err-code": "api-signature-not-valid", "err-msg": "Signature not valid"}})
    with pytest.raises(RuntimeError, match="Signature not valid"):
        asyncio.run(a.balances())


def test_cryptocom_signature_and_params_string():
    a = CryptoComAccount("token", "secretKey")
    assert a.sign("private/user-balance", 11, {}, 1611022832613) == "c1a4f2cc3e9148d10859e5813ac8bd3d010ef67d80fcaf9e4de9a9777e03c6f5"
    assert CryptoComAccount.params_str({"b": "2", "a": ["x", {"d": 1, "c": None}], "e": True}) == "axcnulld1b2etrue"
    body = a.request("private/user-balance", req_id=11, nonce=1611022832613)
    assert body == {"id": 11, "method": "private/user-balance", "api_key": "token", "params": {}, "nonce": 1611022832613,
                    "sig": "c1a4f2cc3e9148d10859e5813ac8bd3d010ef67d80fcaf9e4de9a9777e03c6f5"}
    assert isinstance(a.request("private/user-balance")["nonce"], int)


def test_cryptocom_balances_from_position_balances():
    a = CryptoComAccount("k", "s")
    calls = canned(a, {"/private/user-balance": {"id": "11", "method": "private/user-balance", "code": "0", "result": {"data": [{
        "total_cash_balance": "7890.00320721", "instrument_name": "USD", "position_balances": [
            {"instrument_name": "CRO", "quantity": "24422.72427884", "market_value": "4776.107959969951", "reserved_qty": "0.00000000"},
            {"instrument_name": "USD", "quantity": "3113.50747209", "market_value": "3113.50747209", "reserved_qty": "100"},
            {"instrument_name": "BTC", "quantity": "0", "market_value": "0", "reserved_qty": "0"}]}]}}})
    rows = asyncio.run(a.balances())
    assert calls[0][0] == "POST" and calls[0][1] == "https://api.crypto.com/exchange/v1/private/user-balance" and calls[0][2]["json"]["method"] == "private/user-balance"
    assert rows == [{"asset": "CRO", "qty": 24422.72427884, "hold": 0.0, "id": "cryptocom"}, {"asset": "USD", "qty": 3113.50747209, "hold": 100.0, "id": "cryptocom"}]
    canned(a, {"/private/user-balance": {"id": "1", "method": "private/user-balance", "code": "40101", "message": "UNAUTHORIZED"}})
    with pytest.raises(RuntimeError, match="40101"):
        asyncio.run(a.balances())


def test_bitfinex_signature_headers_and_wallets():
    a = BitfinexAccount("key", "secret")
    assert a.sign("/v2/auth/r/wallets", "1700000000000") == \
        "cfa2c01e3a546a55ff7acb15580ff8d9e89c65daa8096d392c0b3d0f93b6f1cc598ddc9156fd8111810b5cefddfb8acf"
    h = a.headers("/v2/auth/r/wallets", "1700000000000")
    assert h == {"bfx-nonce": "1700000000000", "bfx-apikey": "key", "Content-Type": "application/json",
                 "bfx-signature": "cfa2c01e3a546a55ff7acb15580ff8d9e89c65daa8096d392c0b3d0f93b6f1cc598ddc9156fd8111810b5cefddfb8acf"}
    assert a.headers("/v2/auth/r/wallets")["bfx-nonce"].isdigit()
    asset = BitfinexAccount.asset
    assert asset("UST") == "USDT" and asset("UDC") == "USDC" and asset("BTCF0") == "BTC" and asset("DSH") == "DASH" and asset("TESTBTC") == "" and asset("SOL") == "SOL"
    calls = canned(a, {"/v2/auth/r/wallets": [["exchange", "UST", 2.0, 0, 2.0, "Exchange 2.0 UST for USD @ 11.696", {"reason": "TRADE"}],
                                              ["margin", "BTC", 1.5, 0, 1.0, None, None], ["exchange", "BTC", 0.5, 0, 0.5, None, None],
                                              ["exchange", "TESTBTC", 9.0, 0, 9.0, None, None], ["funding", "ETH", 0.0, 0, 0.0, None, None]]})
    rows = asyncio.run(a.balances())
    assert calls[0][0] == "POST" and calls[0][2]["content"] == "{}" and calls[0][2]["headers"]["bfx-apikey"] == "key"
    assert sorted(rows, key=lambda r: r["asset"]) == [{"asset": "BTC", "qty": 2.0, "hold": 0.5, "id": "bitfinex"}, {"asset": "USDT", "qty": 2.0, "hold": 0.0, "id": "bitfinex"}]
    canned(a, {"/v2/auth/r/wallets": ["error", 10100, "apikey: invalid"]})
    with pytest.raises(RuntimeError, match="apikey: invalid"):
        asyncio.run(a.balances())


def test_gemini_payload_headers_and_balances():
    a = GeminiAccount("key", "secret")
    p = GeminiAccount.payload("/v1/balances", 1776294447)
    assert p == "eyJyZXF1ZXN0IjoiL3YxL2JhbGFuY2VzIiwibm9uY2UiOjE3NzYyOTQ0NDd9"
    assert json.loads(base64.b64decode(p)) == {"request": "/v1/balances", "nonce": 1776294447}
    h = a.headers("/v1/balances", 1776294447)
    assert h == {"Content-Type": "text/plain", "Content-Length": "0", "Cache-Control": "no-cache", "X-GEMINI-APIKEY": "key", "X-GEMINI-PAYLOAD": p,
                 "X-GEMINI-SIGNATURE": "fdec89d64ac13893ed77b3549f57361eb9e502f91b1195c683989f9ce3c8a05d4a6dccdcaa76c0819ff4581e4bacdadd"}
    nonce = json.loads(base64.b64decode(a.headers("/v1/balances")["X-GEMINI-PAYLOAD"]))["nonce"]
    assert isinstance(nonce, int) and 1_700_000_000 < nonce < 2_000_000_000        # epoch seconds, the time-based mode
    calls = canned(a, {"/v1/balances": [{"type": "exchange", "currency": "BTC", "amount": "1154.62034001", "available": "1129.10517279", "availableForWithdrawal": "1129.10517279"},
                                        {"type": "exchange", "currency": "USD", "amount": "18722.79", "available": "14481.62", "availableForWithdrawal": "14481.62"},
                                        {"type": "exchange", "currency": "ETH", "amount": "0", "available": "0", "availableForWithdrawal": "0"}]})
    rows = asyncio.run(a.balances())
    assert calls[0][0] == "POST" and calls[0][1] == "https://api.gemini.com/v1/balances" and "content" not in calls[0][2] and "json" not in calls[0][2]
    assert rows == [{"asset": "BTC", "qty": 1154.62034001, "hold": pytest.approx(25.51516722), "id": "gemini"},
                    {"asset": "USD", "qty": 18722.79, "hold": pytest.approx(4241.17), "id": "gemini"}]
    canned(a, {"/v1/balances": {"result": "error", "reason": "InvalidSignature", "message": "InvalidSignature"}})
    with pytest.raises(RuntimeError, match="InvalidSignature"):
        asyncio.run(a.balances())


def test_bitstamp_v2_signature_and_balances():
    a = BitstampAccount("key", "secret")
    nonce, ts = "f8f5a3d2-1c4e-4b7a-9e0d-123456789abc", "1700000000000"
    assert a.message("POST", "/api/v2/account_balances/", nonce, ts) == "BITSTAMP keyPOSTwww.bitstamp.net/api/v2/account_balances/" + nonce + ts + "v2"
    h = a.headers("POST", "/api/v2/account_balances/", nonce, ts)
    assert h == {"X-Auth": "BITSTAMP key", "X-Auth-Signature": "1c58896240a71f435ae2cc260c7f625de9d4edb8ba0503d1818dfe98584be1d2",
                 "X-Auth-Nonce": nonce, "X-Auth-Timestamp": ts, "X-Auth-Version": "v2"}
    assert "Content-Type" not in h
    live = a.headers("POST", "/api/v2/account_balances/")
    assert re.fullmatch(r"[0-9a-f-]{36}", live["X-Auth-Nonce"]) and live["X-Auth-Timestamp"].isdigit()
    calls = canned(a, {"/api/v2/account_balances/": [{"currency": "btc", "total": "1.0", "available": "0.9", "reserved": "0.1"},
                                                     {"currency": "usd", "total": "100.00", "available": "90.00", "reserved": "10.00"},
                                                     {"currency": "eth", "total": "0", "available": "0", "reserved": "0"}]})
    rows = asyncio.run(a.balances())
    assert calls[0][0] == "POST" and calls[0][1] == "https://www.bitstamp.net/api/v2/account_balances/" and "data" not in calls[0][2]
    assert rows == [{"asset": "BTC", "qty": 1.0, "hold": 0.1, "id": "bitstamp"}, {"asset": "USD", "qty": 100.0, "hold": 10.0, "id": "bitstamp"}]
    canned(a, {"/api/v2/account_balances/": {"status": "error", "reason": "Invalid signature", "code": "API0005"}})
    with pytest.raises(RuntimeError, match="Invalid signature"):
        asyncio.run(a.balances())


@pytest.mark.parametrize("cls", READERS)
def test_readers_describe_configure_and_only_declare_reads(cls):
    a = cls()
    assert not a.configured and a.values() == {"key": "", "secret": ""}
    a.configure({"key": " k ", "secret": ""})
    assert a.key == "k" and not a.configured
    a.configure({"secret": "s"})
    assert a.configured and cls(key="k", secret="s").configured and cls("k", "s").configured
    d = cls.describe()
    assert d["id"] == cls.id and d["name"] == cls.label and d["file"] == f"{cls.id}.json" and d["kind"] == "json_multi"
    assert d["url"].startswith("https://") and "read-only" in d["note"] and d["venue"] == "spot"
    assert [f["id"] for f in d["fields"]] == ["key", "secret"]
    assert cls.endpoints and cls.HOST.startswith("https://")
    for e in cls.endpoints:                                 # balance reads and the fee-tier read (summary, notionalvolume, fees/, fee-rate)
        assert re.search(r"account|balance|wallet|fee|summary|notionalvolume", e, re.I)
        assert not re.search(r"order|withdraw|transfer|cancel|place|amend|convert|borrow|repay|loan|stake|swap", e, re.I)
        assert "fee" in e.lower() or not re.search(r"trade", e, re.I)
    assert not a.allows(f"{cls.HOST}/v1/nope")
    with pytest.raises(PermissionError):
        asyncio.run(a._request("GET", f"{cls.HOST}/v1/nope"))
    with pytest.raises(PermissionError):
        asyncio.run(a._request("DELETE", f"{cls.HOST}{cls.endpoints[0].rstrip('*')}"))
