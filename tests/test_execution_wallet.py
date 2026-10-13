"""The wallet-signed execution clients (Hyperliquid, Aster): the symbol mapping both ways, the
size and price rounding, the signed dry-run request for a market buy and a limit sell with the
signature deterministic for a fixed key and nonce and every secret scrubbed, the Hyperliquid
SDK's published signing vectors (the action hash and four EIP-712 signatures), Aster's exact
pre-sign payload, the answer mapping from captured literals, and the endpoint fence. No network."""
import asyncio
from urllib.parse import urlsplit

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from steel.config import Config
from steel.execution import ExecutionGate, Order
from steel.execution.aster import AsterExecutor
from steel.execution.hyperliquid import HyperliquidExecutor, action_hash, wire

KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"      # the SDK's test key
SIGNER = Account.from_key(KEY).address                                           # 0x14791697260E4c9A71f18484C9f997B308e59325
USER = "0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e"                              # the Aster docs' demo account
VAULT = "0x1719884eb866cb12b2287399b15f7db5e7d775ea"
ALL = [HyperliquidExecutor, AsterExecutor]


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
        sent.append({"method": method, "url": url, "content": content, "json": json_body, "headers": ex._scrub(headers)})
        return sent[-1], answers.pop(0)
    ex._send = send
    return sent


def market_buy(venue, sym="BTC-USD", qty=0.02, px=80_000.0, client_id="steel-42", **kw):
    return Order(venue, sym, "buy", qty, notional_usd=qty * px, client_id=client_id, **kw)


def limit_sell(venue, sym="PEPE-USD", qty=2_500_000.0, px=0.000004, client_id="steel-43", **kw):
    return Order(venue, sym, "sell", qty, price=px, notional_usd=qty * px, reduce_only=True, client_id=client_id, **kw)


def hl(tmp_path, **kw):
    return HyperliquidExecutor(gate(tmp_path, "hyperliquid"), address="0x" + "ab" * 20, agent_key=KEY, **kw)


def aster(tmp_path, **kw):
    return AsterExecutor(gate(tmp_path, "aster"), user=USER, signer=SIGNER, signer_key=KEY, **kw)


def test_descriptions_and_fence(tmp_path):
    for cls in ALL:
        d = cls.describe()
        assert d["venue"] == "perp" and d["file"] == f"{cls.id}-trade.json" and d["testnet"] and cls.HOST.startswith("https://")
        for e in cls.endpoints:
            assert e.startswith("/") and not e.endswith("*")
    ex, ax = hl(tmp_path), aster(tmp_path)
    assert ex.host == HyperliquidExecutor.HOST and hl(tmp_path, testnet=True).host == "https://api.hyperliquid-testnet.xyz"
    assert ax.host == AsterExecutor.HOST and aster(tmp_path, testnet=True).host == "https://fapi.asterdex-testnet.com"
    assert ex.configured and ax.configured
    assert not HyperliquidExecutor(ex.gate, address="0xab", agent_key=KEY).configured          # a malformed address
    assert not HyperliquidExecutor(ex.gate, address=ex.address).configured                     # no key
    assert not AsterExecutor(ax.gate, user=USER, signer=SIGNER).configured
    with pytest.raises(PermissionError):
        run(ex._send("POST", f"{ex.HOST}/exchange/batch"))
    with pytest.raises(PermissionError):
        run(ax._send("GET", f"{ax.HOST}/fapi/v2/balance"))                                     # the reader's path is not an order endpoint
    with pytest.raises(PermissionError):
        run(ax._send("POST", f"{ax.HOST}/fapi/v1/order"))                                      # V1 is not declared either
    assert {c.id for c in ALL} == {"hyperliquid", "aster"}


# Hyperliquid -------------------------------------------------------------------------

def test_hyperliquid_mapping_and_rounding(tmp_path):
    ex = hl(tmp_path)
    assert ex.coin("BTC-USD") == ("BTC", 1.0) and ex.coin("pepe") == ("kPEPE", 1000.0) and ex.coin("SHIB-USD") == ("kSHIB", 1000.0)
    assert ex.canonical("BTC") == ("BTC-USD", 1.0) and ex.canonical("kPEPE") == ("PEPE-USD", 1000.0)
    assert ex.asset("BTC-USD") == (0, 5, 1.0) and ex.asset("PEPE-USD") == (15, 0, 1000.0) and ex.asset("DOGE-USD") == (12, 0, 1.0)
    assert hl(tmp_path, testnet=True).asset("BTC-USD") == (3, 5, 1.0)                        # the testnet's universe is ordered differently
    with pytest.raises(ValueError):
        ex.asset("ZZZ-USD")
    ex.meta = {"ZZZ": (99, 3)}
    assert ex.asset("ZZZ-USD") == (99, 3, 1.0)
    # the SDK's float_to_wire: trailing zeros stripped, rounding error refused
    assert wire(100.0) == "100" and wire(0.0147) == "0.0147" and wire(1670.1) == "1670.1" and wire(80400.0) == "80400" and wire(1e-8) == "0.00000001"
    with pytest.raises(ValueError):
        wire(0.000012312312)
    # sizes are cut to szDecimals, never raised
    assert ex.size(0.0147, 4) == "0.0147" and ex.size(0.123456789, 5) == "0.12345" and ex.size(2_500.7, 0) == "2500" and ex.size(0.00001, 5) == "0.00001"
    with pytest.raises(ValueError):
        ex.size(0.000001, 5)
    # prices: 5 significant figures and at most 6 - szDecimals decimals; a market price rounds like
    # the SDK, a limit to the passive side
    assert ex.price(80_000 * 1.005, 5) == "80400" and ex.price(1891.4321, 4) == "1891.4" and ex.price(123456.0, 5) == "123460"
    assert ex.price(2412.73, 4, "buy") == "2412.7" and ex.price(2412.73, 4, "sell") == "2412.8"
    assert ex.price(0.0045678, 0, "buy") == "0.004567" and ex.price(0.0045678, 0, "sell") == "0.004568"         # kPEPE: 6 decimals cap
    assert ex.price(81_000.0, 5, "sell") == "81000"
    assert ex.cloid("0x00000000000000000000000000000001") == "0x00000000000000000000000000000001"
    assert ex.cloid("steel-42") == "0x00ec2a9eab25e7424654f81c8534c14a" and len(ex.cloid("anything")) == 34


def test_hyperliquid_sdk_signing_vectors(tmp_path):
    """The hyperliquid-python-sdk's tests/signing_test.py vectors, byte for byte."""
    ex = hl(tmp_path)
    order = {"type": "order", "orders": [{"a": 4, "b": True, "p": wire(1670.1), "s": wire(0.0147), "r": False, "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}
    assert action_hash(order, 1677777606040).hex() == "0fcbeda5ae3c4950a548021552a4fea2226858c4453571bf3f24ba017eac2908"
    dummy = {"type": "dummy", "num": 100000000000}
    assert ex.sign(dummy, 0) == {"r": "0x53749d5b30552aeb2fca34b530185976545bb22d0b3ce6f62e31be961a59298",
                                 "s": "0x755c40ba9bf05223521753995abb2f73ab3229be8ec921f350cb447e384d8ed8", "v": 27}
    assert ex.sign(dummy, 0) == ex.sign(dummy, 0)                                               # deterministic (RFC 6979)
    tn = hl(tmp_path, testnet=True)
    assert tn.sign(dummy, 0) == {"r": "0x542af61ef1f429707e3c76c5293c80d01f74ef853e34b76efffcb57e574f9510",
                                 "s": "0x17b8b32f086e8cdede991f1e2c529f5dd5297cbe8128500e00cbaf766204a613", "v": 28}
    gtc = {"type": "order", "orders": [{"a": 1, "b": True, "p": "100", "s": "100", "r": False, "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"}
    assert ex.sign(gtc, 0) == {"r": "0xd65369825a9df5d80099e513cce430311d7d26ddf477f5b3a33d2806b100d78e",
                               "s": "0x2b54116ff64054968aa237c20ca9ff68000f977c93289157748a3162b6ea940e", "v": 28}
    gtc["orders"][0]["c"] = "0x00000000000000000000000000000001"
    assert ex.sign(gtc, 0) == {"r": "0x41ae18e8239a56cacbc5dad94d45d0b747e5da11ad564077fcac71277a946e3",
                               "s": "0x3c61f667e747404fe7eea8f90ab0e76cc12ce60270438b2058324681a00116da", "v": 27}
    assert tn.sign(gtc, 0)["r"] == "0xeba0664bed2676fc4e5a743bf89e5c7501aa6d870bdb9446e122c9466c5cd16d"
    vault = hl(tmp_path, vault=VAULT)
    assert vault.sign(dummy, 0) == {"r": "0x3c548db75e479f8012acf3000ca3a6b05606bc2ec0c29c50c515066a326239",
                                    "s": "0x4d402be7396ce74fbba3795769cda45aec00dc3125a984f2a9f23177b190da2c", "v": 28}
    assert action_hash(dummy, 0, VAULT) != action_hash(dummy, 0) and vault.account == VAULT and ex.account == ex.address


def test_hyperliquid_dry_run(tmp_path):
    ex = hl(tmp_path)
    ex.now = 1_791_720_000.0
    res = run(ex.place(market_buy("hyperliquid")))
    assert res.ok and res.status == "dry-run" and res.request["method"] == "POST" and res.request["content"] is None
    u = urlsplit(res.request["url"])
    assert u.netloc == "api.hyperliquid.xyz" and u.path == "/exchange" and res.request["headers"] == {"Content-Type": "application/json"}
    assert res.request["json"] == {"action": {"type": "order", "orders": [{"a": 0, "b": True, "p": "80400", "s": "0.02", "r": False, "t": {"limit": {"tif": "Ioc"}},
                                                                            "c": "0x00ec2a9eab25e7424654f81c8534c14a"}], "grouping": "na"},
                                   "nonce": 1_791_720_000_000, "signature": "***"}
    res = run(ex.place(limit_sell("hyperliquid", client_id="0x00000000000000000000000000000001")))
    assert res.status == "dry-run" and res.request["json"]["nonce"] == 1_791_720_000_001                # strictly increasing
    assert res.request["json"]["action"] == {"type": "order", "orders": [{"a": 15, "b": False, "p": "0.004", "s": "2500", "r": True, "t": {"limit": {"tif": "Gtc"}},
                                                                          "c": "0x00000000000000000000000000000001"}], "grouping": "na"}
    assert KEY[2:] not in repr(res.request) and "0x" + "ab" * 20 not in repr(res.request)
    assert ex.gate.spent_day[ex.gate.day()] == pytest.approx(0.02 * 80_000 + 10.0)
    # the real envelope carries the signature, which is the SDK's for this key, action and nonce
    body = ex.envelope(res.request["json"]["action"], 1_791_720_000_001)
    assert set(body) == {"action", "nonce", "signature"} and set(body["signature"]) == {"r", "s", "v"} and body["signature"]["v"] in (27, 28)
    assert body["signature"] == ex.sign(body["action"], 1_791_720_000_001)
    assert "vaultAddress" in hl(tmp_path, vault=VAULT).envelope(body["action"], 1)
    bad = run(ex.place(market_buy("hyperliquid", sym="ZZZ-USD")))                               # not in the dry-run's meta: refused, no cooldown
    assert not bad.ok and bad.status == "refused" and "ZZZ" in bad.error and "hyperliquid" not in ex.gate.rejects
    nop = run(ex.place(Order("hyperliquid", "BTC-USD", "buy", 0.0000001, notional_usd=1.0)))
    assert nop.status == "refused" and "rounds to nothing" in nop.error
    # a market order with no reference price: the gate refuses it before the client sees it
    assert run(ex.place(Order("hyperliquid", "BTC-USD", "buy", 0.02))).error == "no reference price for the notional"


def test_hyperliquid_answers(tmp_path):
    ex = hl(tmp_path)
    resting = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 77738308}}]}}, "_status": 200}
    filled = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 77747314}}]}}, "_status": 200}
    too_small = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10."}]}}, "_status": 200}
    no_agent = {"status": "err", "response": "User or API Wallet 0xabababababababababababababababababababab does not exist.", "_status": 200}
    http = {"text": "Failed to deserialize the JSON body into the target type", "_status": 422}
    sent = canned(ex, [{"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}, {"name": "kPEPE", "szDecimals": 0}]},
                       resting, filled, too_small, no_agent, http])
    r = run(ex.place(limit_sell("hyperliquid")))
    assert sent[0]["json"] == {"type": "meta"} and ex.meta == {"BTC": (0, 5), "ETH": (1, 4), "kPEPE": (2, 0)}
    assert sent[1]["json"]["action"]["orders"][0]["a"] == 2                                      # the live index, not the seed's 15
    assert r.ok and r.status == "accepted" and r.order_id == "77738308" and r.filled_qty == 0 and r.raw is resting
    r = run(ex.place(market_buy("hyperliquid", sym="ETH-USD", qty=0.02, px=1890.0)))
    assert r.ok and r.status == "filled" and r.order_id == "77747314" and r.filled_qty == 0.02 and r.avg_price == 1891.4
    r = run(ex.place(market_buy("hyperliquid")))
    assert not r.ok and r.status == "rejected" and r.error == "Order must have minimum value of $10." and ex.gate.rejects["hyperliquid"]
    ex.gate.rejects.clear()
    r = run(ex.place(market_buy("hyperliquid")))
    assert not r.ok and r.status == "rejected" and r.error.startswith("User or API Wallet 0xab")
    ex.gate.rejects.clear()
    r = run(ex.place(market_buy("hyperliquid")))
    assert not r.ok and r.status == "rejected" and r.error.startswith("HTTP 422")
    assert all(s["json"]["signature"] == "***" for s in sent[1:])                               # scrubbed in the logged request
    assert all(KEY[2:] not in repr(s) for s in sent)
    sent = canned(ex, [{"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}},
                       {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled."}]}}},
                       {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}},
                       {"data": [{"coin": "BTC", "limitPx": "29792.0", "oid": 91490942, "side": "A", "sz": "4.0", "origSz": "5.0", "timestamp": 1681247412573},
                                 {"coin": "kPEPE", "limitPx": "0.004", "oid": 91490943, "side": "B", "sz": "2500.0", "origSz": "2500.0", "timestamp": 1681247412573,
                                  "cloid": "0x00000000000000000000000000000001"},
                                 {"coin": "@107", "limitPx": "18.6", "oid": 1, "side": "A", "sz": "1.0", "origSz": "1.0", "timestamp": 1681247412573}]},
                       {"data": [{"closedPnl": "0.0", "coin": "AVAX", "crossed": False, "dir": "Open Long", "hash": "0xa166e3fa63c25663024b03f2e0da011a00307e4017465df020210d3d432e7cb8",
                                  "oid": 90542681, "px": "18.435", "side": "B", "startPosition": "26.86", "sz": "93.53", "time": 1681222254710, "fee": "0.01", "feeToken": "USDC",
                                  "tid": 118906512037719},
                                 {"closedPnl": "0.0", "coin": "kPEPE", "crossed": True, "dir": "Open Short", "hash": "0x00", "oid": 90542682, "px": "0.004", "side": "A",
                                  "startPosition": "0.0", "sz": "2500.0", "time": 1791720000000, "fee": "0.0045", "feeToken": "USDC", "tid": 1},
                                 {"closedPnl": "8722.988077", "coin": "@107", "crossed": True, "dir": "Sell", "hash": "0x22", "oid": 59071663721, "px": "18.62041381",
                                  "side": "A", "sz": "43.84", "time": 1791720000000, "fee": "0.304521", "feeToken": "USDC", "tid": 907359904431134}]}])
    assert run(ex.cancel("77738308", "BTC-USD")).status == "cancelled"
    assert sent[0]["json"]["action"] == {"type": "cancel", "cancels": [{"a": 0, "o": 77738308}]}
    bad = run(ex.cancel("1", "BTC-USD"))
    assert not bad.ok and bad.status == "rejected" and bad.error == "Order was never placed, already canceled, or filled."
    assert run(ex.cancel("0x00000000000000000000000000000001", "PEPE-USD")).ok
    assert sent[2]["json"]["action"] == {"type": "cancelByCloid", "cancels": [{"asset": 2, "cloid": "0x00000000000000000000000000000001"}]}
    oo = run(ex.open_orders())
    assert sent[3]["json"] == {"type": "openOrders", "user": ex.address}
    assert oo == [{"order_id": "91490942", "symbol": "BTC-USD", "side": "sell", "qty": 5.0, "price": 29792.0, "filled": 1.0, "status": "open", "client_id": ""},
                  {"order_id": "91490943", "symbol": "PEPE-USD", "side": "buy", "qty": 2_500_000.0, "price": 4e-06, "filled": 0.0, "status": "open",
                   "client_id": "0x00000000000000000000000000000001"}]
    f = run(ex.fills(1_700_000_000.0))                                                            # the 2023 fill and the spot fill are dropped
    assert sent[4]["json"] == {"type": "userFills", "user": ex.address}
    assert f == [{"order_id": "90542682", "symbol": "PEPE-USD", "side": "sell", "qty": 2_500_000.0, "price": 4e-06, "fee": 0.0045, "t": 1_791_720_000.0}]



# Aster -------------------------------------------------------------------------------

def test_aster_mapping_and_rounding(tmp_path):
    ax = aster(tmp_path)
    assert ax.contract("BTC-USD") == ("BTCUSDT", 1.0) and ax.contract("pepe") == ("1000PEPEUSDT", 1000.0) and ax.contract("NEX-USD") == ("1000NEXUSDT", 1000.0)
    assert ax.canonical("BTCUSDT") == ("BTC-USD", 1.0) and ax.canonical("1000PEPEUSDT") == ("PEPE-USD", 1000.0) and ax.canonical("1000shibusdt") == ("SHIB-USD", 1000.0)
    assert ax.quantity("BTCUSDT", 0.0123456) == "0.012" and ax.quantity("DOGEUSDT", 1234.9) == "1234" and ax.quantity("SOLUSDT", 0.02) == "0.02"
    assert ax.quantity("ZZZUSDT", 0.0123456) == "0.0123456"                                      # no step known: passed through
    with pytest.raises(ValueError):
        ax.quantity("BTCUSDT", 0.0005)
    assert ax.tick("ETHUSDT", 2412.737, "buy") == "2412.73" and ax.tick("ETHUSDT", 2412.731, "sell") == "2412.74" and ax.tick("BTCUSDT", 81000.0, "sell") == "81000"
    assert ax.tick("1000PEPEUSDT", 0.00400004, "buy") == "0.004" and ax.tick("ZZZUSDT", 1.23456789, "buy") == "1.23456789"
    assert ax.chain_id == 1666 and aster(tmp_path, testnet=True).chain_id == 714


def test_aster_payload_and_signature(tmp_path):
    """The docs' worked example (POST /fapi/v3/order, nonce 1748310859508867) carries no signature
    output, so the exact pre-sign string is pinned, plus our own signature for the SDK test key:
    deterministic, 65 bytes, recovering to the signer under the documented domain."""
    ax = aster(tmp_path)
    params = {"symbol": "ASTERUSDT", "type": "LIMIT", "side": "BUY", "timeInForce": "GTC", "quantity": "20", "price": "0.5"}
    payload = ax.payload(params, 1748310859508867)
    assert payload == ("symbol=ASTERUSDT&type=LIMIT&side=BUY&timeInForce=GTC&quantity=20&price=0.5&nonce=1748310859508867"
                       f"&user={USER}&signer={SIGNER}")
    sig = ax.sign(payload)
    assert sig == ("0xa590e0a95488ad70ab5fd4d86e973f86e2358f276f37014de6bbb5007122aa0623a5da9590d7795fcf537b3e4327b491ee1a8e34258a9dde4afb"
                   "69127692445c1b")
    assert sig == ax.sign(payload) and len(bytes.fromhex(sig[2:])) == 65
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"},
                                        {"name": "verifyingContract", "type": "address"}], "Message": [{"name": "msg", "type": "string"}]},
             "primaryType": "Message", "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": 1666, "verifyingContract": "0x" + "0" * 40},
             "message": {"msg": payload}}
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=sig) == SIGNER
    assert aster(tmp_path, testnet=True).sign(payload) != sig                                    # chainId 714 on the testnet
    assert ax.signed(params, 1748310859508867) == f"{payload}&signature={sig}"
    ax.now = 1748310859.508867
    assert ax.nonce() == 1748310859508867 and ax.nonce() == 1748310859508868                     # strictly increasing within the process


def test_aster_dry_run(tmp_path):
    ax = aster(tmp_path)
    ax.now = 1748310859.508867
    res = run(ax.place(market_buy("aster")))
    assert res.ok and res.status == "dry-run" and res.request["method"] == "POST" and res.request["json"] is None and res.request["params"] is None
    u = urlsplit(res.request["url"])
    assert u.netloc == "fapi.asterdex.com" and u.path == "/fapi/v3/order" and u.query == ""
    assert res.request["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
    assert res.request["content"] == (f"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.02&newClientOrderId=steel-42&nonce=1748310859508867&user={USER}"
                                      f"&signer={SIGNER}&signature=***")
    res = run(ax.place(limit_sell("aster")))
    assert res.status == "dry-run" and res.request["content"] == (
        f"symbol=1000PEPEUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=2500&price=0.004&reduceOnly=true&newClientOrderId=steel-43"
        f"&nonce=1748310859508868&user={USER}&signer={SIGNER}&signature=***")
    assert KEY[2:] not in repr(res.request)
    assert ax.gate.spent_day[ax.gate.day()] == pytest.approx(0.02 * 80_000 + 10.0) and ax.recent == {"BTCUSDT", "1000PEPEUSDT"}
    # the bytes that would be sent are the bytes signed: the payload with the real signature appended
    body = ax.signed(ax.order_params(limit_sell("aster")), 1748310859508868)
    assert body.startswith("symbol=1000PEPEUSDT&") and "&signature=0x" in body and len(body.rsplit("=", 1)[1]) == 132
    cid = run(ax.place(market_buy("aster", client_id="a b!c" * 20)))
    assert "newClientOrderId=abcabcabcabcabcabcabcabcabcabcabcabc&" in cid.request["content"]     # 36 of the allowed characters
    small = run(ax.place(Order("aster", "BTC-USD", "buy", 0.0001, notional_usd=8.0)))
    assert small.status == "refused" and "under the BTCUSDT step" in small.error and "aster" not in ax.gate.rejects


def test_aster_answers(tmp_path):
    ax = aster(tmp_path)
    new = {"clientOrderId": "steel-43", "cumQty": "0", "cumQuote": "0", "executedQty": "0", "orderId": 22542179, "avgPrice": "0.00000", "origQty": "2500",
           "price": "0.004", "reduceOnly": True, "side": "SELL", "positionSide": "BOTH", "status": "NEW", "stopPrice": "0", "closePosition": False,
           "symbol": "1000PEPEUSDT", "timeInForce": "GTC", "type": "LIMIT", "origType": "LIMIT", "updateTime": 1566818724722, "workingType": "CONTRACT_PRICE",
           "priceProtect": False, "_status": 200}
    filled = {"clientOrderId": "steel-42", "cumQty": "0.020", "cumQuote": "1600.2", "executedQty": "0.020", "orderId": 22542180, "avgPrice": "80010.0", "origQty": "0.020",
              "price": "0", "reduceOnly": False, "side": "BUY", "positionSide": "BOTH", "status": "FILLED", "symbol": "BTCUSDT", "timeInForce": "GTC", "type": "MARKET",
              "origType": "MARKET", "updateTime": 1566818724722, "workingType": "CONTRACT_PRICE", "priceProtect": False, "_status": 200}
    bad_symbol = {"code": -1121, "msg": "Invalid symbol.", "_status": 400}
    expired = {"code": -4225, "msg": "Nonce Expired", "_status": 400}
    html = {"text": "<html>403 Forbidden</html>", "_status": 403}
    sent = canned(ax, [new, filled, bad_symbol, expired, html])
    r = run(ax.place(limit_sell("aster")))
    assert r.ok and r.status == "accepted" and r.order_id == "22542179" and r.filled_qty == 0 and r.avg_price is None and r.raw is new
    r = run(ax.place(market_buy("aster")))
    assert r.ok and r.status == "filled" and r.order_id == "22542180" and r.filled_qty == 0.02 and r.avg_price == 80010.0
    r = run(ax.place(market_buy("aster")))
    assert not r.ok and r.status == "rejected" and r.error == "Invalid symbol. (-1121)" and ax.gate.rejects["aster"]
    ax.gate.rejects.clear()
    assert run(ax.place(market_buy("aster"))).error == "Nonce Expired (-4225)"
    ax.gate.rejects.clear()
    assert run(ax.place(market_buy("aster"))).error.startswith("HTTP 403")
    assert all(s["method"] == "POST" and s["content"].endswith("&signature=***") for s in sent)                # scrubbed in the logged request
    assert all(KEY[2:] not in repr(s) for s in sent)
    sent = canned(ax, [{"clientOrderId": "steel-43", "orderId": 283194212, "status": "CANCELED", "symbol": "1000PEPEUSDT", "side": "SELL", "origQty": "2500",
                        "executedQty": "0", "price": "0.004", "_status": 200},
                       {"code": -2011, "msg": "Unknown order sent.", "_status": 400},
                       {"data": [{"avgPrice": "0.00000", "clientOrderId": "abc", "cumQuote": "0", "executedQty": "0.005", "orderId": 1917641, "origQty": "0.40", "origType": "LIMIT",
                                  "price": "29792.0", "reduceOnly": False, "side": "BUY", "positionSide": "BOTH", "status": "PARTIALLY_FILLED", "stopPrice": "0",
                                  "closePosition": False, "symbol": "BTCUSDT", "time": 1579276756075, "timeInForce": "GTC", "type": "LIMIT", "updateTime": 1579276756075,
                                  "workingType": "CONTRACT_PRICE", "priceProtect": False}], "_status": 200},
                       {"data": [{"buyer": False, "commission": "-0.07819010", "commissionAsset": "USDT", "id": 698759, "maker": False, "orderId": 25851813, "price": "7819.01",
                                  "qty": "0.002", "quoteQty": "15.63802", "realizedPnl": "-0.91539999", "side": "SELL", "positionSide": "BOTH", "symbol": "BTCUSDT",
                                  "time": 1569514978020}], "_status": 200},
                       {"data": [], "_status": 200}])
    c = run(ax.cancel("283194212", "PEPE-USD"))
    assert c.ok and c.status == "cancelled" and sent[0]["method"] == "DELETE" and sent[0]["content"].startswith("symbol=1000PEPEUSDT&orderId=283194212&nonce=")
    bad = run(ax.cancel("1", "BTC-USD"))
    assert not bad.ok and bad.error == "Unknown order sent. (-2011)"
    assert run(ax.open_orders("BTC-USD")) == [{"order_id": "1917641", "symbol": "BTC-USD", "side": "buy", "qty": 0.4, "price": 29792.0, "filled": 0.005,
                                               "status": "partially_filled", "client_id": "abc"}]
    q = urlsplit(sent[2]["url"])
    assert sent[2]["method"] == "GET" and q.path == "/fapi/v3/openOrders" and q.query.startswith("symbol=BTCUSDT&nonce=") and q.query.endswith("&signature=***")
    ax.recent = {"BTCUSDT", "1000PEPEUSDT"}
    ax.now = 1_791_720_000.0
    ax._last_nonce = 0                       # the nonce is monotonic: earlier calls ran on the real clock, which passed this stamp on 2026-10-12
    f = run(ax.fills(1_791_700_000.0))
    assert f == [{"order_id": "25851813", "symbol": "BTC-USD", "side": "sell", "qty": 0.002, "price": 7819.01, "fee": -0.0781901, "t": 1_569_514_978.02}]
    assert "symbol=1000PEPEUSDT&startTime=1791700000000&limit=1000&nonce=" in sent[3]["url"] and "symbol=BTCUSDT&" in sent[4]["url"]
    ax.recent = {"BTCUSDT"}
    sent = canned(ax, [{"data": [], "_status": 200}])
    run(ax.fills(1_000_000_000.0))
    assert "startTime=1791115200000&" in sent[0]["url"]                                          # clamped to now - 7 days
