"""Hyperliquid, BloFin, BitMEX and ApeX Omni account readers: signatures (BitMEX against its
documented vector, the others against the exact pre-sign string with a fixed timestamp), asset
codes, balance parsing from captured response shapes, `configured` and `describe()`. No network."""
import asyncio
import base64
import hashlib
import hmac

import pytest

from steel.accounts.apex import ApeXAccount
from steel.accounts.bitmex import BitMEXAccount
from steel.accounts.blofin import BloFinAccount
from steel.accounts.hyperliquid import HyperliquidAccount

READERS = [HyperliquidAccount, BloFinAccount, BitMEXAccount, ApeXAccount]


def test_every_reader_describes_itself_and_gates_its_paths():
    for cls in READERS:
        d = cls.describe()
        assert d["id"] == cls.id and d["name"] == cls.label and d["file"] == f"{cls.id}.json" and d["kind"] == "json_multi"
        assert d["url"].startswith("https://") and "read-only" in d["note"] and d["venue"] in ("spot", "perp", "mixed")
        assert d["fields"] and all({"id", "label"} <= set(f) for f in d["fields"])
        a = cls()
        assert not a.configured and a.endpoints
        for e in cls.endpoints:
            assert a.allows(f"{cls.HOST}{e}?x=1")
        assert not a.allows(f"{cls.HOST}/api/v1/market/candles")
        with pytest.raises(PermissionError):
            asyncio.run(a._request("GET", f"{cls.HOST}/api/v1/market/candles"))


# Hyperliquid ----------------------------------------------------------------------

def test_hyperliquid_address_only():
    a = HyperliquidAccount(address=" 0xdfc24b077bc1425ad1dea75bcb6f8158e10df303 ")
    assert a.configured and a.values() == {"address": "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"} and a.venue == "mixed"
    assert not HyperliquidAccount(address="0xabc").configured and not HyperliquidAccount().configured
    assert "no API key" in HyperliquidAccount.note and HyperliquidAccount.endpoints == ("/info",)
    assert HyperliquidAccount.asset("kPEPE") == "PEPE" and HyperliquidAccount.asset("UBTC") == "BTC" and HyperliquidAccount.asset("PURR") == "PURR"


def test_hyperliquid_balances_from_the_documented_shape():
    a = HyperliquidAccount(address="0xdfc24b077bc1425ad1dea75bcb6f8158e10df303")
    perp = {"assetPositions": [{"position": {"coin": "ETH", "cumFunding": {"allTime": "514.085417", "sinceChange": "0.0", "sinceOpen": "0.0"}, "entryPx": "2986.3",
                                             "leverage": {"rawUsd": "-95.059824", "type": "isolated", "value": 20}, "liquidationPx": "2866.26936529", "marginUsed": "4.967826",
                                             "maxLeverage": 50, "positionValue": "100.02765", "returnOnEquity": "-0.0026789", "szi": "0.0335", "unrealizedPnl": "-0.0134"}, "type": "oneWay"},
                               {"position": {"coin": "kPEPE", "entryPx": "0.004", "leverage": {"type": "cross", "value": 3}, "positionValue": "201.75", "szi": "-50000.0", "unrealizedPnl": "-1.75"}, "type": "oneWay"}],
            "crossMaintenanceMarginUsed": "0.0", "crossMarginSummary": {"accountValue": "13104.514502", "totalMarginUsed": "0.0", "totalNtlPos": "0.0", "totalRawUsd": "13104.514502"},
            "marginSummary": {"accountValue": "13109.482328", "totalMarginUsed": "4.967826", "totalNtlPos": "100.02765", "totalRawUsd": "13009.454678"}, "time": 1708622398623, "withdrawable": "13104.514502"}
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "1019.97983005", "hold": "0.0", "entryNtl": "0.0"}, {"coin": "PURR", "token": 1, "total": "6.52278", "hold": "1.0", "entryNtl": "1.43"},
                         {"coin": "UBTC", "token": 142, "total": "0.5", "hold": "0.0", "entryNtl": "40000"}, {"coin": "HFUN", "token": 2, "total": "0.0", "hold": "0.0", "entryNtl": "0"}]}
    rows = a.parse(perp, spot)
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["kind"] == "perp" and eth["qty"] == 0.0335 and eth["entry"] == 2986.3 and eth["leverage"] == 20 and eth["upnl"] == -0.0134
    assert eth["price"] == pytest.approx(100.02765 / 0.0335) and eth["id"] == "****f303"
    pepe = next(r for r in rows if r["asset"] == "PEPE")
    assert pepe["qty"] == -50_000_000.0 and pepe["entry"] == pytest.approx(4e-6) and pepe["price"] == pytest.approx(201.75 / 50_000_000) and pepe["leverage"] == 3
    usdc = [r for r in rows if r["asset"] == "USDC" and "kind" not in r]
    assert len(usdc) == 2 and usdc[0]["qty"] == pytest.approx(13109.482328 + 0.0134 + 1.75) and usdc[0]["hold"] == 4.967826    # collateral less the open PnL the rows carry
    assert usdc[1]["qty"] == 1019.97983005
    assert {r["asset"]: r["qty"] for r in rows if r["asset"] in ("PURR", "BTC")} == {"PURR": 6.52278, "BTC": 0.5}
    assert next(r for r in rows if r["asset"] == "PURR")["hold"] == 1.0 and not any(r["asset"] == "HFUN" for r in rows)


# BloFin ---------------------------------------------------------------------------

def test_blofin_signature_and_headers():
    a = BloFinAccount(key="k1", secret="s3cret", passphrase="pp")
    assert a.configured and not BloFinAccount(key="k1", secret="s3cret").configured
    path = "/api/v1/asset/balances?accountType=futures"
    h = a.headers("GET", path, ts=1597026383085, nonce="7c3a1f1e-0d9b-4c7a-9a2b-1f2e3d4c5b6a")
    expect = base64.b64encode(hmac.new(b"s3cret", (path + "GET" + "1597026383085" + "7c3a1f1e-0d9b-4c7a-9a2b-1f2e3d4c5b6a").encode(), hashlib.sha256).hexdigest().encode()).decode()
    assert h["ACCESS-SIGN"] == expect and h["ACCESS-KEY"] == "k1" and h["ACCESS-TIMESTAMP"] == "1597026383085" and h["ACCESS-PASSPHRASE"] == "pp"
    assert h["ACCESS-NONCE"] == "7c3a1f1e-0d9b-4c7a-9a2b-1f2e3d4c5b6a" and h["Content-Type"] == "application/json"
    assert a.sign("POST", "/api/v1/account/positions", "1", "n", '{"a":1}') == base64.b64encode(
        hmac.new(b"s3cret", b'/api/v1/account/positionsPOST1n{"a":1}', hashlib.sha256).hexdigest().encode()).decode()
    live = a.headers("GET", path)
    assert len(live["ACCESS-TIMESTAMP"]) == 13 and len(live["ACCESS-NONCE"]) == 36
    assert BloFinAccount.asset("1000PEPE-USDT") == "PEPE" and BloFinAccount.asset("BTC-USDT") == "BTC"


def test_blofin_balances_from_the_documented_shape():
    a = BloFinAccount(key="abcdef1234", secret="s", passphrase="p")
    futures = {"ts": "1697021343571", "totalEquity": "10011254.07", "isolatedEquity": "862.00",
               "details": [{"currency": "USDT", "equity": "10014042.988958", "balance": "10013119.885958", "available": "9996399.47", "frozen": "15805.149672", "orderFrozen": "14920.99", "bonus": "0"},
                           {"currency": "USDC", "equity": "0", "balance": "0", "available": "0", "frozen": "0", "bonus": "0"}]}
    funding = [{"currency": "USDT", "balance": "250.5", "available": "250.5", "frozen": "0", "bonus": "0"}, {"currency": "BTC", "balance": "0.02", "available": "0.01", "frozen": "0.01", "bonus": "0"}]
    positions = [{"positionId": "7982", "instId": "ETH-USDT", "instType": "SWAP", "marginMode": "isolated", "positionSide": "net", "adl": "5", "leverage": "3", "positions": "1", "availablePositions": "1",
                  "averagePrice": "1591.8", "markPrice": "1591.69", "marginRatio": "72.45", "liquidationPrice": "1066.10", "unrealizedPnl": "-0.011", "unrealizedPnlRatio": "-0.000207", "maintenanceMargin": "0.636676"},
                 {"positionId": "5483", "instId": "BCH-USDT", "instType": "SWAP", "marginMode": "cross", "positionSide": "net", "adl": "5", "leverage": "3", "positions": "-290", "availablePositions": "-290",
                  "averagePrice": "1890", "markPrice": "1889", "unrealizedPnl": "2.9", "unrealizedPnlRatio": "0.001587", "initialMargin": "1826.033333", "maintenanceMargin": "54.781"},
                 {"positionId": "9", "instId": "1000PEPE-USDT", "marginMode": "isolated", "positionSide": "short", "leverage": "2", "positions": "40", "averagePrice": "0.0040", "markPrice": "0.0040", "unrealizedPnl": "0"},
                 {"positionId": "10", "instId": "SOL-USDT", "marginMode": "cross", "positionSide": "net", "leverage": "5", "positions": "0", "averagePrice": "150", "markPrice": "151", "unrealizedPnl": "0"}]
    rows = a.parse(futures, funding, positions)
    spot = [(r["asset"], r["qty"], r["hold"]) for r in rows if "kind" not in r]
    assert spot == [("USDT", 10013119.885958, 15805.149672), ("USDT", 250.5, 0.0), ("BTC", 0.02, 0.01)]           # the wallet balance, not equity; zero rows skipped
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["kind"] == "perp" and eth["qty"] == pytest.approx(0.1) and eth["price"] == 1591.69 and eth["entry"] == 1591.8 and eth["leverage"] == 3 and eth["upnl"] == -0.011
    assert eth["note"] is None and eth["id"] == "****1234"                                                       # 1 contract = 0.1 ETH, backed out of the PnL
    bch = next(r for r in rows if r["asset"] == "BCH")
    assert bch["qty"] == pytest.approx(-2.9, rel=1e-6) and bch["note"] is None                                   # cross: initialMargin x leverage / mark, short
    pepe = next(r for r in rows if r["asset"] == "PEPE")
    assert pepe["qty"] == -40_000.0 and pepe["note"] == "size in contracts" and pepe["price"] == pytest.approx(4e-6)   # opened at the mark, isolated: nothing to back it out of
    assert not any(r["asset"] == "SOL" for r in rows)


# BitMEX ---------------------------------------------------------------------------

def test_bitmex_signature_matches_the_documented_vectors():
    # The "Full sample calculation" from BitMEX's API key docs (apiKeysUsage): a GET and a GET with a URL-encoded query.
    a = BitMEXAccount(key="LAqUlngMIQkIUjXMUreyu3qn", secret="chNOOS4KvNXR_Xq4k4c9qsfoKWvnDecLATCRlcBwyKDYnWgO")
    assert a.configured and not BitMEXAccount(key="LAqUlngMIQkIUjXMUreyu3qn").configured
    assert a.sign("GET", "/api/v1/instrument", 1518064236) == "c7682d435d0cfe87c16098df34ef2eb5a549d4c5a3c2b1f0f77b8af73423bf00"
    assert a.sign("GET", "/api/v1/instrument?filter=%7B%22symbol%22%3A+%22XBTM15%22%7D", 1518064237) == "e2f422547eecb5b3cb29ade2127e21b858b235b386bfa45e1c1756eb3383919f"
    h = a.headers("GET", "/api/v1/instrument", expires=1518064236)
    assert h == {"api-expires": "1518064236", "api-key": "LAqUlngMIQkIUjXMUreyu3qn", "api-signature": "c7682d435d0cfe87c16098df34ef2eb5a549d4c5a3c2b1f0f77b8af73423bf00"}
    assert int(a.headers("GET", "/api/v1/user/margin")["api-expires"]) > 1_700_000_000


def test_bitmex_asset_codes_and_balances():
    asset = BitMEXAccount.asset
    assert asset("XBt") == "BTC" and asset("USDt") == "USDT" and asset("XBTUSDT") == "BTC" and asset("ETHUSD") == "ETH" and asset("1000000MOGUSDT") == "MOG" and asset("SOLUSDT") == "SOL"
    a = BitMEXAccount(key="abcdef1234", secret="s")
    margins = [{"account": 1, "currency": "XBt", "walletBalance": 150000000, "marginBalance": 150000000, "availableMargin": 100000000, "unrealisedPnl": 0},
               {"account": 1, "currency": "USDt", "walletBalance": 2500000000, "marginBalance": 2600000000, "availableMargin": 2500000000, "unrealisedPnl": 100000000},
               {"account": 1, "currency": "Gwei", "walletBalance": 0, "marginBalance": 0, "availableMargin": 0, "unrealisedPnl": 0}]
    positions = [{"symbol": "XBTUSDT", "currency": "USDt", "currentQty": -1000000, "homeNotional": -1.0, "avgEntryPrice": 80000, "markPrice": 79000, "leverage": 5, "unrealisedPnl": 1000000000, "isOpen": True},
                 {"symbol": "ETHUSD", "currency": "XBt", "currentQty": 10, "homeNotional": 0.01, "avgEntryPrice": 2500, "markPrice": 2600, "leverage": 10, "unrealisedPnl": 1200, "isOpen": True},
                 {"symbol": "SOLUSDT", "currency": "USDt", "currentQty": 0, "homeNotional": 0, "isOpen": False}]
    rows = a.parse(margins, positions, {"XBT": 8, "USDT": 6, "Gwei": 9})
    assert [(r["asset"], r["qty"], r["hold"]) for r in rows if "kind" not in r] == [("BTC", 1.5, 0.5), ("USDT", 2500.0, 0.0)]       # satoshi and micro-dollars scaled
    btc = next(r for r in rows if r["asset"] == "BTC" and r.get("kind") == "perp")
    assert btc["qty"] == -1.0 and btc["price"] == 79000 and btc["entry"] == 80000 and btc["leverage"] == 5 and btc["upnl"] == 1000.0 and btc["id"] == "****1234"
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["qty"] == 0.01 and eth["upnl"] == pytest.approx(1200e-8 * 2600)                                   # a quanto's PnL settles in bitcoin: marked to dollars
    assert not any(r["asset"] == "SOL" for r in rows)


# ApeX Omni ------------------------------------------------------------------------

def test_apex_signature_and_headers():
    a = ApeXAccount(key="k1", secret="s3cret", passphrase="pp")
    assert a.configured and not ApeXAccount(key="k1", secret="s3cret").configured
    path = "/api/v3/account-balance"
    h = a.headers("GET", path, ts=1661415017232)
    key = base64.standard_b64encode(b"s3cret")                                   # the base64 of the secret keys the HMAC, as the venue's own SDK does
    expect = base64.standard_b64encode(hmac.new(key, b"1661415017232GET/api/v3/account-balance", hashlib.sha256).digest()).decode()
    assert h == {"APEX-SIGNATURE": expect, "APEX-TIMESTAMP": "1661415017232", "APEX-API-KEY": "k1", "APEX-PASSPHRASE": "pp"}
    assert a.sign("1", "POST", "/api/v3/x", "a=1&b=2") == base64.standard_b64encode(hmac.new(key, b"1POST/api/v3/xa=1&b=2", hashlib.sha256).digest()).decode()
    assert len(a.headers("GET", path)["APEX-TIMESTAMP"]) == 13
    assert ApeXAccount.asset("1000PEPE-USDT") == "PEPE" and ApeXAccount.asset("BTCUSDT") == "BTC" and ApeXAccount.asset("BTC-USDT") == "BTC"


def test_apex_balances_from_the_documented_shape():
    a = ApeXAccount(key="abcdef1234", secret="s", passphrase="p")
    account = {"id": "1", "ethereumAddress": "0xabc", "contractWallets": [{"asset": "USDT", "balance": "1000.5", "pendingWithdrawAmount": "10", "pendingTransferOutAmount": "0"},
                                                                          {"asset": "USDC", "balance": "0"}],
               "spotWallets": [{"tokenId": "ETH", "balance": "0.25"}],
               "openPositions": [{"symbol": "BTC-USDT", "side": "LONG", "size": "0.5", "entryPrice": "80000", "fee": "1", "fundingFee": "0"},
                                 {"symbol": "1000PEPE-USDT", "side": "SHORT", "size": "3", "entryPrice": "0.004"},
                                 {"symbol": "SOL-USDT", "side": "LONG", "size": "0", "entryPrice": "150"}]}
    balance = {"totalEquityValue": "42000", "availableBalance": "1000", "symbolToOraclePrice": {"BTC-USDT": {"oraclePrice": "82000", "createdTime": 1}}}
    rows = a.parse(account, balance)
    assert [(r["asset"], r["qty"], r["hold"]) for r in rows if "kind" not in r] == [("USDT", 1000.5, 10.0), ("ETH", 0.25, 0.0)]
    btc = next(r for r in rows if r["asset"] == "BTC")
    assert btc["kind"] == "perp" and btc["qty"] == 0.5 and btc["price"] == 82000 and btc["entry"] == 80000 and btc["upnl"] == 1000.0 and btc["id"] == "****1234"
    pepe = next(r for r in rows if r["asset"] == "PEPE")
    assert pepe["qty"] == -3000.0 and pepe["entry"] == pytest.approx(4e-6) and pepe["price"] == pytest.approx(4e-6) and pepe["upnl"] == 0.0   # no oracle mark: marked at entry
    assert not any(r["asset"] == "SOL" for r in rows)
    assert a.parse({"positions": [{"symbol": "ETH-USDT", "side": "SELL", "size": "2", "entryPrice": "2500"}]}, {})[0]["qty"] == -2.0
