"""Asset identity: shared tickers are ambiguous, a venue's name resolves them, the dislocation
monitor pairs an ambiguous name only between venues that resolved to the same coin, and the
manager lets only a resolved venue serve one."""
import asyncio

from steel.dislocation import DislocationMonitor
from steel.feed import Tick
from steel.identity import IdentityBook
from steel.sources import SourceManager
from tests.test_universe_liquid import cfg

ROWS = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
        {"id": "midnight", "symbol": "night", "name": "Midnight", "market_cap_rank": 300},
        {"id": "night-coin", "symbol": "night", "name": "Night Coin", "market_cap_rank": 800},
        {"id": "usd-ai", "symbol": "chip", "name": "USD.AI", "market_cap_rank": 600},
        {"id": "chip-token", "symbol": "chip", "name": "Chip Token", "market_cap_rank": 900}]


def book(tmp_path):
    b = IdentityBook(tmp_path)
    b.load_coins(ROWS)
    b.venue_names["coinbase"] = {"CHIP": "USD.AI", "BTC": "Bitcoin"}
    return b


def test_ambiguity_resolution_and_labels(tmp_path):
    b = book(tmp_path)
    assert not b.ambiguous("BTC-USD") and b.ambiguous("NIGHT-USD") and b.ambiguous("CHIP-USD") and not b.known("NOPE-USD")
    assert b.resolve("binance", "BTC-USD") == "bitcoin"                       # one ranked coin: anyone's listing is it
    assert b.resolve("coinbase", "CHIP-USD") == "usd-ai" and b.resolve("binance", "CHIP-USD") is None
    assert b.resolve("coinbase", "NIGHT-USD") is None                          # Coinbase publishes no name for it here
    lab = b.label("CHIP-USD", "coinbase")
    assert lab["id"] == "usd-ai" and lab["resolved"] and lab["ambiguous"] and lab["others"] == ["Chip Token"]
    guess = b.label("NIGHT-USD", "binance")
    assert guess["id"] == "midnight" and not guess["resolved"] and guess["ambiguous"]           # the best guess, flagged
    assert b.label("NOPE-USD")["id"] is None
    b.t = 1.0; b._save()
    again = IdentityBook(tmp_path)
    assert again.coins == b.coins and again.venue_names == b.venue_names and again.stale and again.status()["ambiguous"] == 2


def test_monitor_pairs_ambiguous_names_only_on_matching_identity():
    m = DislocationMonitor(lambda v: 5.0)
    t = 1_791_000_000.0
    m.update("NIGHT-USD", "htx", 1.00, 1.01, t, quote="USDT", identity=None, ambiguous=True)
    m.update("NIGHT-USD", "okx", 1.50, 1.51, t, quote="USDT", identity=None, ambiguous=True)         # a 50% "gap" between two unknown tokens: no episode
    assert not m.open
    m.update("NIGHT-USD", "coinbase", 1.00, 1.01, t, quote="USD", identity="midnight", ambiguous=True)
    m.update("NIGHT-USD", "kraken", 1.30, 1.31, t, quote="USD", identity="midnight", ambiguous=True)  # both resolved to the same coin: a real gap
    assert ("NIGHT-USD", "USD", "spot") in m.open
    m2 = DislocationMonitor(lambda v: 5.0)
    m2.update("NIGHT-USD", "coinbase", 1.00, 1.01, t, quote="USD", identity="midnight", ambiguous=True)
    m2.update("NIGHT-USD", "kraken", 1.30, 1.31, t, quote="USD", identity="night-coin", ambiguous=True)   # different coins: nothing
    assert not m2.open
    m2.update("BTC-USD", "coinbase", 100.0, 100.01, t, quote="USD")
    m2.update("BTC-USD", "kraken", 100.50, 100.51, t, quote="USD")                                    # an unshared ticker needs no identity
    assert ("BTC-USD", "USD", "spot") in m2.open


def test_manager_serves_an_ambiguous_name_only_from_a_resolved_venue(tmp_path):
    c = cfg()
    c.symbols = ["CHIP-USD", "BTC-USD"]
    b = book(tmp_path)
    m = SourceManager(c, c.symbols, on_tick=lambda t: None)
    m.identity_ok = lambda venue, sym: not b.ambiguous(sym) or b.resolve(venue, sym) is not None
    m.enabled["binance"] = True
    now = 1_791_000_000.0
    m._route("binance", Tick("CHIP-USD", now, 1.0, 1.0, True))
    assert m.active["CHIP-USD"] != "binance"                                   # Binance's CHIP is unresolved: it may not serve the name
    m._route("coinbase", Tick("CHIP-USD", now, 1.0, 1.0, True))
    assert m.active["CHIP-USD"] == "coinbase"
    m._route("binance", Tick("BTC-USD", now, 1.0, 1.0, True))
    assert m.active["BTC-USD"] == "binance"                                    # BTC is one coin everywhere
