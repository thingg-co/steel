"""Read-only account access: request signing, asset code mapping, balance assembly."""
import asyncio
import base64
import hashlib
import hmac
import time

import pytest

from steel.accounts import Account, CoinbaseAccount, KrakenAccount, fetch_balances, held_symbols


def test_kraken_signature_matches_the_documented_vector():
    # The example from Kraken's API docs (their path; nothing here calls it).
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    k = KrakenAccount("key", secret)
    data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD", "price": 37500, "type": "buy", "volume": 1.25}
    sig = k.sign("/0/private/" + "Add" + "Order", data)
    assert sig == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    assert not KrakenAccount("", "").configured and k.configured


def test_kraken_asset_codes():
    a = KrakenAccount.asset
    assert a("XXBT") == "BTC" and a("XETH") == "ETH" and a("ZUSD") == "USD" and a("XXDG") == "DOGE" and a("XDG") == "DOGE"
    assert a("SOL") == "SOL" and a("XRP") == "XRP" and a("XTZ") == "XTZ" and a("DOT.S") == "DOT" and a("ETH2.S") == "ETH2"


def test_coinbase_jwt_claims():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import jwt as pyjwt
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    acct = CoinbaseAccount("organizations/abc/apiKeys/def", pem.replace("\n", "\\n"))   # the escaped form a one-line field carries
    assert acct.configured and acct.private_key.startswith("-----BEGIN EC PRIVATE KEY-----\n")
    now = time.time()
    tok = acct.jwt("GET", "/api/v3/brokerage/accounts", now=now)
    pub = key.public_key()
    header = pyjwt.get_unverified_header(tok)
    claims = pyjwt.decode(tok, pub, algorithms=["ES256"])
    assert header["kid"] == "organizations/abc/apiKeys/def" and len(header["nonce"]) == 32 and header["alg"] == "ES256"
    assert claims["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts" and claims["sub"] == acct.key_name
    assert claims["iss"] == "cdp" and claims["exp"] - claims["nbf"] == 120
    assert not CoinbaseAccount("", "").configured


def test_fetch_balances_marks_at_live_prices_and_reports_errors():
    class CB(Account):
        id, label, venue = "coinbase", "Coinbase", "spot"
        configured = True
        async def balances(self):
            return [{"asset": "BTC", "qty": 0.5, "hold": 0.0, "id": "****1234"}, {"asset": "USD", "qty": 1000.0, "hold": 0.0, "id": "x"},
                    {"asset": "USDC", "qty": 250.0, "hold": 0.0, "id": "y"}, {"asset": "NEWCOIN", "qty": 3.0, "hold": 0.0, "id": "z"}]

    class KR(Account):
        id, label = "kraken", "Kraken"
        configured = True
        async def balances(self):
            raise RuntimeError("EAPI:Invalid key")

    class HL(Account):
        id, label, venue = "hyperliquid", "Hyperliquid", "perp"
        configured = True
        async def balances(self):
            return [{"asset": "USDC", "qty": 5_000.0}, {"asset": "ETH", "kind": "perp", "qty": -2.0, "price": 4_000.0, "entry": 4_100.0, "leverage": 3.0, "upnl": 200.0},
                    {"asset": "SOL", "kind": "perp", "qty": 10.0, "price": 150.0, "entry": 140.0, "leverage": 2.0}]

    class OFF(Account):
        id, label = "off", "Off"
        configured = False
    prices = {"BTC-USD": {"price": 80_000.0}, "SOL-USD": {"price": 160.0}}
    pf = asyncio.run(fetch_balances([CB(), KR(), HL(), OFF(), None], prices))
    assert [a["id"] for a in pf["accounts"]] == ["coinbase", "hyperliquid", "kraken"]        # by value; the unconfigured one is skipped
    cb = next(a for a in pf["accounts"] if a["name"] == "Coinbase")
    assert cb["cash"] == 1250.0 and cb["liquidation"] == pytest.approx(41_250.0) and cb["venue"] == "spot"
    btc = next(p for p in cb["positions"] if p["symbol"] == "BTC-USD")
    assert btc["value"] == 40_000.0 and btc["marked"] is True and btc["kind"] == "spot"
    new = next(p for p in cb["positions"] if p["symbol"] == "NEWCOIN-USD")
    assert new["value"] == 0.0 and new["marked"] is False
    kr = next(a for a in pf["accounts"] if a["name"] == "Kraken")
    assert "Invalid key" in kr["error"] and kr["positions"] == []
    hl = next(a for a in pf["accounts"] if a["id"] == "hyperliquid")
    eth = next(p for p in hl["positions"] if p["symbol"] == "ETH-USD")
    sol = next(p for p in hl["positions"] if p["symbol"] == "SOL-USD")
    assert eth["kind"] == "perp" and eth["qty"] == -2.0 and eth["price"] == 4_000.0 and eth["value"] == -8_000.0   # the venue's mark when the name is not modeled
    assert eth["entry"] == 4_100.0 and eth["leverage"] == 3.0 and eth["upnl"] == 200.0
    assert sol["price"] == 160.0 and sol["value"] == 1_600.0                                   # the live price when it is
    assert hl["cash"] == 5_000.0 and hl["liquidation"] == 5_000.0 and hl["perp_notional"] == pytest.approx(-6_400.0)   # perp notional is not equity
    assert held_symbols(pf) == {"BTC-USD", "SOL-USD"}       # the unpriced dust, the stables and the short are not holdings to model
    assert asyncio.run(fetch_balances([], prices))["accounts"] == []


def test_the_gate_refuses_undeclared_endpoints_and_writes():
    class A(Account):
        id, label = "a", "A"
        endpoints = ("/v1/balances", "/api/v5/account/*")
    a = A()
    assert a.allows("https://x.test/v1/balances?x=1") and a.allows("https://x.test/api/v5/account/balance")
    assert not a.allows("https://x.test/v1/order") and not a.allows("https://x.test/api/v5/trade/order")
    with pytest.raises(PermissionError):
        asyncio.run(a._request("GET", "https://x.test/v1/order"))
    with pytest.raises(PermissionError):
        asyncio.run(a._request("DELETE", "https://x.test/v1/balances"))
    with pytest.raises(PermissionError):
        asyncio.run(a._request("PUT", "https://x.test/v1/balances"))


def test_account_fields_configure_and_describe():
    k = KrakenAccount()
    assert not k.configured and k.values() == {"key": "", "secret": ""}
    k.configure({"key": " abc ", "secret": ""})
    assert k.key == "abc" and not k.configured
    k.configure({"secret": "s"})
    assert k.configured
    d = KrakenAccount.describe()
    assert d["id"] == "kraken" and d["name"] == "Kraken" and d["file"] == "kraken.json" and d["kind"] == "json_multi" and d["url"].startswith("https://")
    cb = CoinbaseAccount()
    cb.configure({"name": "org/k", "privateKey": "-----BEGIN EC PRIVATE KEY-----\\nabc\\n-----END EC PRIVATE KEY-----\\n"})
    assert cb.configured and cb.private_key == "-----BEGIN EC PRIVATE KEY-----\nabc\n-----END EC PRIVATE KEY-----\n" and cb.key_name == "org/k"
