"""`Account.fees()`: each reader's fee-tier read, parsed from the venue's documented response
shape into bps per leg, through the gate (every call is to a declared endpoint). No network:
`_json` is replaced per test. An unconfigured reader answers None without a request, and so does
a reader that has no fee endpoint."""
import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from steel.accounts import ACCOUNTS, Account
from steel.accounts.aster import AsterAccount
from steel.accounts.base import fee_bps, fee_rates
from steel.accounts.binance import BinanceAccount
from steel.accounts.bitfinex import BitfinexAccount
from steel.accounts.bitstamp import BitstampAccount
from steel.accounts.bybit import BybitAccount
from steel.accounts.coinbase import CoinbaseAccount
from steel.accounts.cryptocom import CryptoComAccount
from steel.accounts.gateio import GateIOAccount
from steel.accounts.gemini import GeminiAccount
from steel.accounts.htx import HTXAccount
from steel.accounts.hyperliquid import HyperliquidAccount
from steel.accounts.kraken import KrakenAccount
from steel.accounts.kucoin import KuCoinAccount
from steel.accounts.mexc import MEXCAccount
from steel.accounts.okx import OKXAccount

KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
WITH_FEES = {CoinbaseAccount, KrakenAccount, BinanceAccount, BybitAccount, OKXAccount, KuCoinAccount, GateIOAccount, CryptoComAccount,
             MEXCAccount, HTXAccount, GeminiAccount, BitstampAccount, BitfinexAccount, HyperliquidAccount, AsterAccount}


def canned(acct, monkeypatch, routes):
    """Replace the gate with a dispatcher on the URL path (a key may be a path or a prefix;
    a value may be an exception to raise, or a callable of the request kwargs). Every call must
    be a GET or POST to a declared endpoint; the calls are returned."""
    calls = []

    async def fake(method, url, **kw):
        assert method in ("GET", "POST") and acct.allows(url), (method, url)
        calls.append((method, url, kw))
        path = urlsplit(url).path
        hit = next((v for k, v in routes.items() if path == k or path.startswith(k)), None)
        assert hit is not None, path
        if isinstance(hit, Exception):
            raise hit
        return hit(kw) if callable(hit) else hit
    monkeypatch.setattr(acct, "_json", fake)
    return calls


def run(coro):
    return asyncio.run(coro)


def test_fee_bps_and_fee_rates():
    assert fee_bps("0.001") == 10.0 and fee_bps(0.0004) == 4.0 and fee_bps("0.2600", 100.0) == 26.0 and fee_bps(35, 1.0) == 35.0
    assert fee_bps("-0.0008") == -8.0                                     # the sign is kept: the caller knows the venue's convention
    assert fee_bps(None) is None and fee_bps("") is None and fee_bps("n/a") is None and fee_bps(float("nan")) is None
    assert fee_bps(0) == 0.0                                              # a real zero (MEXC's maker) is a rate, not "unknown"
    assert fee_rates(spot_taker=10.0, perp_maker=None) == {"spot_taker_bps": 10.0}
    assert fee_rates() == {}


def test_unconfigured_readers_answer_none_without_http(monkeypatch):
    monkeypatch.delenv("STEEL_BINANCE_HOST", raising=False)

    async def boom(*a, **k):
        raise AssertionError("no HTTP for an unconfigured reader")
    for cls in ACCOUNTS:
        a = cls()
        assert not a.configured
        monkeypatch.setattr(a, "_request", boom)
        monkeypatch.setattr(a, "_json", boom)
        assert run(a.fees()) is None, cls.id


def test_readers_without_a_fee_endpoint_answer_none(monkeypatch):
    """The base `fees()` is None even for a configured reader; only the venues with a verified
    fee read override it."""
    async def boom(*a, **k):
        raise AssertionError("no HTTP")
    for cls in ACCOUNTS:
        if cls in WITH_FEES:
            assert cls.fees is not Account.fees, cls.id
            continue
        assert cls.fees is Account.fees, cls.id
        a = cls(**{f["id"]: "x" for f in cls.fields})
        monkeypatch.setattr(a, "_json", boom)
        assert run(a.fees()) is None


def test_every_fee_endpoint_is_a_declared_read():
    for cls in WITH_FEES:
        assert len(cls.endpoints) >= 2 or cls is HyperliquidAccount
        for e in cls.endpoints:
            assert not re.search(r"order|withdraw|transfer|cancel|place|amend|borrow|repay|loan|stake|swap", e, re.I), (cls.id, e)


def test_coinbase_fee_tier(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    a = CoinbaseAccount("organizations/abc/apiKeys/def", pem)
    calls = canned(a, monkeypatch, {"/api/v3/brokerage/transaction_summary": {
        "total_volume": 1000, "total_fees": 25, "fee_tier": {"pricing_tier": "<$10k", "taker_fee_rate": "0.0040", "maker_fee_rate": "0.0025"}}})
    assert run(a.fees()) == {"spot_taker_bps": 40.0, "spot_maker_bps": 25.0}          # decimals; no perps
    (method, url, kw), = calls
    assert method == "GET" and kw["params"] == {"product_type": "SPOT"} and kw["headers"]["Authorization"].startswith("Bearer ")


def test_kraken_trade_volume(monkeypatch):
    a = KrakenAccount("key", KRAKEN_SECRET)
    calls = canned(a, monkeypatch, {"/0/private/TradeVolume": {"error": [], "result": {
        "currency": "ZUSD", "volume": "1000.0000",
        "fees": {"XXBTZUSD": {"fee": "0.2600", "minfee": "0.1000", "maxfee": "0.2600", "nextfee": "0.2400", "tiervolume": "0.0000", "nextvolume": "50000.0000"}},
        "fees_maker": {"XXBTZUSD": {"fee": "0.1600", "minfee": "0.0000", "maxfee": "0.1600", "nextfee": "0.1400", "tiervolume": "0.0000", "nextvolume": "50000.0000"}}}}})
    assert run(a.fees()) == {"spot_taker_bps": 26.0, "spot_maker_bps": 16.0}          # percents
    (method, url, kw), = calls
    assert method == "POST" and kw["data"]["pair"] == "XBTUSD" and kw["data"]["fee-info"] == "true" and "nonce" in kw["data"]
    assert kw["headers"]["API-Key"] == "key" and kw["headers"]["API-Sign"] == a.sign("/0/private/TradeVolume", kw["data"])
    bad = KrakenAccount("key", KRAKEN_SECRET)
    canned(bad, monkeypatch, {"/0/private/TradeVolume": {"error": ["EAPI:Invalid key"]}})
    with pytest.raises(RuntimeError, match="Invalid key"):
        run(bad.fees())


def test_binance_spot_and_futures(monkeypatch):
    monkeypatch.delenv("STEEL_BINANCE_HOST", raising=False)
    a = BinanceAccount(key="k", secret="s")
    calls = canned(a, monkeypatch, {"/sapi/v1/asset/tradeFee": [{"symbol": "BTCUSDT", "makerCommission": "0.001", "takerCommission": "0.001"}],
                                    "/fapi/v1/commissionRate": {"symbol": "BTCUSDT", "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0004"}})
    assert run(a.fees()) == {"spot_taker_bps": 10.0, "spot_maker_bps": 10.0, "perp_taker_bps": 4.0, "perp_maker_bps": 2.0}
    assert [urlsplit(u).netloc for _, u, _ in calls] == ["api.binance.com", "fapi.binance.com"]
    for _, u, kw in calls:
        q = parse_qs(urlsplit(u).query)
        assert q["symbol"] == ["BTCUSDT"] and "signature" in q and "timestamp" in q and kw["headers"]["X-MBX-APIKEY"] == "k"
    # no futures permission: the spot rates stand
    b = BinanceAccount(key="k", secret="s")
    canned(b, monkeypatch, {"/sapi/v1/asset/tradeFee": [{"symbol": "BTCUSDT", "makerCommission": "0.00075", "takerCommission": "0.00075"}],
                            "/fapi/v1/commissionRate": RuntimeError('HTTP 401: {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')})
    assert run(b.fees()) == {"spot_taker_bps": 7.5, "spot_maker_bps": 7.5}
    # Binance.US: the /sapi path is not served there, so a failed spot read leaves nothing, and futures is never asked
    monkeypatch.setenv("STEEL_BINANCE_HOST", "api.binance.us")
    us = BinanceAccount(key="k", secret="s")
    calls = canned(us, monkeypatch, {"/sapi/v1/asset/tradeFee": RuntimeError("HTTP 404: not found")})
    assert run(us.fees()) == {} and len(calls) == 1 and urlsplit(calls[0][1]).netloc == "api.binance.us"


def test_bybit_spot_and_linear(monkeypatch):
    a = BybitAccount(key="k", secret="s")

    def answer(kw, url_holder=[]):
        return None
    rows = {"spot": {"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": [{"symbol": "BTCUSDT", "takerFeeRate": "0.001", "makerFeeRate": "0.001"}]}},
            "linear": {"retCode": 0, "retMsg": "OK", "result": {"list": [{"symbol": "BTCUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.0002"}]}}}
    calls = []

    async def fake(method, url, **kw):
        assert method == "GET" and a.allows(url)
        calls.append(url)
        return rows[parse_qs(urlsplit(url).query)["category"][0]]
    monkeypatch.setattr(a, "_json", fake)
    assert run(a.fees()) == {"spot_taker_bps": 10.0, "spot_maker_bps": 10.0, "perp_taker_bps": 5.5, "perp_maker_bps": 2.0}
    assert [parse_qs(urlsplit(u).query)["category"] for u in calls] == [["spot"], ["linear"]]
    assert all(parse_qs(urlsplit(u).query)["symbol"] == ["BTCUSDT"] for u in calls)


def test_okx_negative_rates_are_fees(monkeypatch):
    a = OKXAccount(key="k", secret="s", passphrase="p")
    rows = {"SPOT": {"code": "0", "msg": "", "data": [{"level": "Lv1", "instType": "SPOT", "taker": "-0.001", "maker": "-0.0008", "takerU": "", "makerU": "",
                                                       "feeGroup": [{"groupId": "1", "taker": "-0.001", "maker": "-0.0008"}]}]},
            "SWAP": {"code": "0", "msg": "", "data": [{"level": "Lv1", "instType": "SWAP", "taker": "-0.0005", "maker": "-0.0002", "takerU": "-0.0005", "makerU": "-0.0002",
                                                       "feeGroup": [{"groupId": "1", "taker": "-0.0005", "maker": "-0.0002"}]}]}}
    calls = []

    async def fake(method, url, **kw):
        assert method == "GET" and a.allows(url)
        calls.append((url, kw["headers"]))
        return rows[parse_qs(urlsplit(url).query)["instType"][0]]
    monkeypatch.setattr(a, "_json", fake)
    assert run(a.fees()) == {"spot_taker_bps": 10.0, "spot_maker_bps": 8.0, "perp_taker_bps": 5.0, "perp_maker_bps": 2.0}
    assert [parse_qs(urlsplit(u).query)["instType"] for u, _ in calls] == [["SPOT"], ["SWAP"]]
    assert all(h["OK-ACCESS-KEY"] == "k" and h["OK-ACCESS-PASSPHRASE"] == "p" for _, h in calls)
    # a rebate (positive at OKX) comes back negative; the feeGroup stands in for an empty takerU
    rows["SWAP"]["data"][0].update({"takerU": "", "makerU": "", "feeGroup": [{"groupId": "1", "taker": "-0.0003", "maker": "0.0001"}]})
    f = run(a.fees())
    assert f["perp_taker_bps"] == 3.0 and f["perp_maker_bps"] == -1.0


def test_kucoin_base_fee_and_futures(monkeypatch):
    a = KuCoinAccount(key="k", secret="s", passphrase="p")
    calls = canned(a, monkeypatch, {"/api/v1/base-fee": {"code": "200000", "data": {"takerFeeRate": "0.001", "makerFeeRate": "0.001"}},
                                    "/api/v1/trade-fees": {"code": "200000", "data": {"symbol": "XBTUSDTM", "takerFeeRate": "0.0006", "makerFeeRate": "0.0002"}}})
    assert run(a.fees()) == {"spot_taker_bps": 10.0, "spot_maker_bps": 10.0, "perp_taker_bps": 6.0, "perp_maker_bps": 2.0}
    assert [urlsplit(u).netloc for _, u, _ in calls] == ["api.kucoin.com", "api-futures.kucoin.com"]
    assert parse_qs(urlsplit(calls[1][1]).query)["symbol"] == ["XBTUSDTM"] and calls[1][2]["headers"]["KC-API-KEY"] == "k"
    b = KuCoinAccount(key="k", secret="s", passphrase="p")
    canned(b, monkeypatch, {"/api/v1/base-fee": {"code": "200000", "data": {"takerFeeRate": "0.0008", "makerFeeRate": "0.0005"}},
                            "/api/v1/trade-fees": {"code": "400003", "msg": "KC-API-KEY not exists"}})
    assert run(b.fees()) == {"spot_taker_bps": 8.0, "spot_maker_bps": 5.0}         # no futures permission: spot stands


def test_gateio_wallet_fee(monkeypatch):
    a = GateIOAccount(key="k", secret="s")
    calls = canned(a, monkeypatch, {"/api/v4/wallet/fee": {"user_id": 10001, "taker_fee": "0.002", "maker_fee": "0.002", "gt_discount": True,
                                                            "gt_taker_fee": "0.0015", "gt_maker_fee": "0.0015", "loan_fee": "0.18", "point_type": "1",
                                                            "futures_taker_fee": "0.0005", "futures_maker_fee": "0.00015",
                                                            "delivery_taker_fee": "0.00050", "delivery_maker_fee": "-0.00020"}})
    assert run(a.fees()) == {"spot_taker_bps": 20.0, "spot_maker_bps": 20.0, "perp_taker_bps": 5.0, "perp_maker_bps": 1.5}
    (method, url, kw), = calls
    assert method == "GET" and parse_qs(urlsplit(url).query) == {"currency_pair": ["BTC_USDT"], "settle": ["usdt"]} and kw["headers"]["KEY"] == "k"


def test_cryptocom_bps_stay_bps(monkeypatch):
    a = CryptoComAccount("k", "s")
    calls = canned(a, monkeypatch, {"/exchange/v1/private/get-fee-rate": {"id": 1, "method": "/private/get-fee-rate", "code": 0, "result": {
        "spot_tier": "3", "deriv_tier": "3", "effective_spot_maker_rate_bps": "6.5", "effective_spot_taker_rate_bps": "6.9",
        "effective_deriv_maker_rate_bps": "1.1", "effective_deriv_taker_rate_bps": "3"}}})
    assert run(a.fees()) == {"spot_taker_bps": 6.9, "spot_maker_bps": 6.5, "perp_taker_bps": 3.0, "perp_maker_bps": 1.1}
    (method, url, kw), = calls
    body = kw["json"]
    assert method == "POST" and body["method"] == "private/get-fee-rate" and body["params"] == {} and body["api_key"] == "k"
    assert body["sig"] == a.sign("private/get-fee-rate", body["id"], {}, body["nonce"])
    bad = CryptoComAccount("k", "s")
    canned(bad, monkeypatch, {"/exchange/v1/private/get-fee-rate": {"code": 10002, "message": "UNAUTHORIZED"}})
    with pytest.raises(RuntimeError, match="UNAUTHORIZED"):
        run(bad.fees())


def test_mexc_trade_fee_and_signed_query(monkeypatch):
    a = MEXCAccount(key="k", secret="s")
    calls = canned(a, monkeypatch, {"/api/v3/tradeFee": {"data": {"makerCommission": 0.0, "takerCommission": 0.0005}, "code": 0, "msg": "success", "timestamp": 1669109672717}})
    assert run(a.fees()) == {"spot_taker_bps": 5.0, "spot_maker_bps": 0.0}         # MEXC's zero maker is a real zero
    (method, url, kw), = calls
    q = urlsplit(url).query
    assert q.startswith("symbol=BTCUSDT&timestamp=") and "&recvWindow=5000&signature=" in q and kw["headers"]["X-MEXC-APIKEY"] == "k"
    unsigned = q.split("&signature=")[0]
    assert q.endswith(a.sign(unsigned))                                   # the symbol is inside the signed string
    assert a.query(ts=1, params={"symbol": "BTCUSDT"}).startswith("symbol=BTCUSDT&timestamp=1&recvWindow=5000&signature=")
    assert a.query(ts=1) == f"timestamp=1&recvWindow=5000&signature={a.sign('timestamp=1&recvWindow=5000')}"   # unchanged for the balance read


def test_htx_actual_rate_and_signed_symbols(monkeypatch):
    a = HTXAccount("k", "s")
    calls = canned(a, monkeypatch, {"/v2/reference/transact-fee-rate": {"code": 200, "success": True, "data": [
        {"symbol": "btcusdt", "actualMakerRate": "0.0016", "actualTakerRate": "0.0018", "takerFeeRate": "0.002", "makerFeeRate": "0.002"}]}})
    assert run(a.fees()) == {"spot_taker_bps": 18.0, "spot_maker_bps": 16.0}      # the deducted rate wins over the basic one
    (method, url, kw), = calls
    p = kw["params"]
    assert method == "GET" and p["symbols"] == "btcusdt" and p["AccessKeyId"] == "k"
    unsigned = {k: v for k, v in p.items() if k != "Signature"}
    assert p["Signature"] == a.sign("GET", "/v2/reference/transact-fee-rate", unsigned)   # the symbols parameter is signed
    b = HTXAccount("k", "s")
    canned(b, monkeypatch, {"/v2/reference/transact-fee-rate": {"code": 200, "success": True, "data": [
        {"symbol": "btcusdt", "takerFeeRate": "0.002", "makerFeeRate": "-0.0001"}]}})
    assert run(b.fees()) == {"spot_taker_bps": 20.0, "spot_maker_bps": -1.0}      # no deduction: the basic rate; a maker rebate stays negative


def test_gemini_bps_stay_bps(monkeypatch):
    a = GeminiAccount("k", "s")
    calls = canned(a, monkeypatch, {"/v1/notionalvolume": {"web_maker_fee_bps": 25, "web_taker_fee_bps": 35, "web_auction_fee_bps": 25,
                                                           "api_maker_fee_bps": 10, "api_taker_fee_bps": 35, "api_auction_fee_bps": 20,
                                                           "fix_maker_fee_bps": 10, "fix_taker_fee_bps": 35, "fix_auction_fee_bps": 20,
                                                           "notional_30d_volume": 150, "last_updated_ms": 1551371446000, "date": "2019-02-28", "notional_1d_volume": []}})
    assert run(a.fees()) == {"spot_taker_bps": 35.0, "spot_maker_bps": 10.0}
    (method, url, kw), = calls
    assert method == "POST" and kw["headers"]["X-GEMINI-APIKEY"] == "k" and kw["headers"]["X-GEMINI-PAYLOAD"]
    bad = GeminiAccount("k", "s")
    canned(bad, monkeypatch, {"/v1/notionalvolume": {"result": "error", "reason": "InvalidSignature", "message": "InvalidSignature"}})
    with pytest.raises(RuntimeError, match="InvalidSignature"):
        run(bad.fees())


def test_bitstamp_percent_per_market(monkeypatch):
    a = BitstampAccount("k", "s")
    calls = canned(a, monkeypatch, {"/api/v2/fees/trading/": [{"currency_pair": "ethusd", "market": "ethusd", "fees": {"maker": "0.30000", "taker": "0.40000"}},
                                                               {"currency_pair": "btcusd", "market": "btcusd", "fees": {"maker": "0.15000", "taker": "0.16000"}}]})
    assert run(a.fees()) == {"spot_taker_bps": 16.0, "spot_maker_bps": 15.0}      # btcusd's row, percents
    (method, url, kw), = calls
    assert method == "POST" and kw["headers"]["X-Auth"] == "BITSTAMP k" and kw["headers"]["X-Auth-Version"] == "v2"
    bad = BitstampAccount("k", "s")
    canned(bad, monkeypatch, {"/api/v2/fees/trading/": {"status": "error", "reason": "Missing key, signature and nonce parameters."}})
    with pytest.raises(RuntimeError, match="Missing key"):
        run(bad.fees())


def test_bitfinex_summary_fee_info(monkeypatch):
    a = BitfinexAccount("k", "s")
    summary = [None, None, None, None,
               [[0.001, 0.001, 0.001, None, None, 0.0002], [0.002, 0.002, 0.0025, None, None, 0.0065]],   # FEE_INFO: maker rates, taker rates
               [[None, None, None, None, None, None], {}], None, None, None, {"leo_lev": 0, "leo_amount_avg": 0}]
    calls = canned(a, monkeypatch, {"/v2/auth/r/summary": summary})
    assert run(a.fees()) == {"spot_taker_bps": 25.0, "spot_maker_bps": 10.0, "perp_taker_bps": 65.0, "perp_maker_bps": -2.0}   # fiat taker; the deriv rebate is a negative fee
    (method, url, kw), = calls
    assert method == "POST" and kw["content"] == "{}" and kw["headers"]["bfx-apikey"] == "k" and kw["headers"]["bfx-signature"]
    bad = BitfinexAccount("k", "s")
    canned(bad, monkeypatch, {"/v2/auth/r/summary": ["error", 10100, "apikey: invalid"]})
    with pytest.raises(RuntimeError, match="apikey: invalid"):
        run(bad.fees())


def test_hyperliquid_user_fees(monkeypatch):
    a = HyperliquidAccount(address=ADDRESS)
    calls = canned(a, monkeypatch, {"/info": {"userCrossRate": "0.000315", "userAddRate": "0.000105", "userSpotCrossRate": "0.00049", "userSpotAddRate": "0.00028",
                                              "activeReferralDiscount": "0.0", "dailyUserVlm": [], "feeSchedule": {"cross": "0.00045", "add": "0.00015"}}})
    assert run(a.fees()) == {"spot_taker_bps": 4.9, "spot_maker_bps": 2.8, "perp_taker_bps": 3.15, "perp_maker_bps": 1.05}
    (method, url, kw), = calls
    assert method == "POST" and kw["json"] == {"type": "userFees", "user": ADDRESS}


def test_aster_commission_rate(monkeypatch):
    a = AsterAccount(key="k", secret="s")
    calls = canned(a, monkeypatch, {"/fapi/v1/commissionRate": {"symbol": "BTCUSDT", "makerCommissionRate": "0.0001", "takerCommissionRate": "0.0004"}})
    assert run(a.fees()) == {"perp_taker_bps": 4.0, "perp_maker_bps": 1.0}     # a perp venue: no spot keys
    (method, url, kw), = calls
    q = parse_qs(urlsplit(url).query)
    assert method == "GET" and q["symbol"] == ["BTCUSDT"] and "signature" in q and kw["headers"]["X-MBX-APIKEY"] == "k"
