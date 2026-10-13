"""The cross-venue dislocation monitor: episodes open when a bid on one venue clears an ask on
another net of both fees, close when the gap is gone, measure their window, respect the quote
currency and kind, ignore stale quotes, and reach the log and the manager's hook."""
import json

from steel.dislocation import DislocationMonitor
from steel.feed import Tick
from steel.sources import SourceManager
from tests.test_universe_liquid import cfg

FEES = {"coinbase": 40.0, "kraken": 40.0, "binance": 10.0, "bybit": 5.5, "hyperliquid": 4.5, "gemini": 40.0}


def monitor(tmp_path=None, **kw):
    return DislocationMonitor(lambda v: FEES.get(v, 10.0), log_path=(tmp_path / "d.jsonl") if tmp_path else None, **kw)


def test_episode_opens_closes_and_measures_its_window(tmp_path):
    m = monitor(tmp_path)
    t = 1_791_000_000.0
    assert m.update("BTC-USD", "binance", 100.00, 100.01, t, quote="USDT") is None               # one venue: nothing to compare
    assert m.update("BTC-USD", "bybit", 100.20, 100.21, t + 0.1, quote="USDT") is None          # bybit bid 100.20 > binance ask 100.01: 19 bps gross, 15.5 fees -> open
    assert len(m.open) == 1 and m.open[("BTC-USD", "USDT", "spot")]["buy"] == "binance" and m.open[("BTC-USD", "USDT", "spot")]["sell"] == "bybit"
    assert m.update("BTC-USD", "bybit", 100.25, 100.26, t + 0.6, quote="USDT") is None          # wider: still open, peak grows
    ep = m.open[("BTC-USD", "USDT", "spot")]
    assert ep["samples"] == 2 and 8.0 < ep["peak_bps"] < 9.0                                  # 24 bps gross less 15.5 of fees
    closed = m.update("BTC-USD", "binance", 100.24, 100.25, t + 2.1, quote="USDT")              # binance catches up: gap gone
    assert closed and closed["buy"] == "binance" and closed["sell"] == "bybit" and closed["window_ms"] == 2000
    assert closed["fees_bps"] == 15.5 and closed["peak_bps"] == round(ep["peak_bps"], 2) and closed["samples"] == 2
    assert not m.open and m.n_total == 1 and json.loads((tmp_path / "d.jsonl").read_text().strip())["sym"] == "BTC-USD"
    s = m.stats()
    assert s["overall"]["n"] == 1 and s["overall"]["window_ms_median"] == 2000 and s["pairs"]["binance>bybit"]["n"] == 1 and s["venues"] == ["binance", "bybit"]


def test_fees_quote_currency_kind_and_staleness_rule_the_comparison():
    m = monitor()
    t = 1_791_000_000.0
    m.update("ETH-USD", "coinbase", 100.0, 100.02, t, quote="USD")
    m.update("ETH-USD", "kraken", 100.50, 100.52, t, quote="USD")                               # 50 bps gross, 80 bps of fees: no episode
    assert not m.open
    m.update("ETH-USD", "gemini", 101.00, 101.02, t, quote="USD")                               # 100 bps gross vs coinbase's ask: 20 net -> open
    assert m.open[("ETH-USD", "USD", "spot")]["sell"] == "gemini" and m.open[("ETH-USD", "USD", "spot")]["buy"] == "coinbase"
    m.update("ETH-USD", "binance", 101.50, 101.51, t, quote="USDT")                             # a USDT book: another group, no leg against USD
    assert ("ETH-USD", "USDT", "spot") not in m.open and m.open[("ETH-USD", "USD", "spot")]["sell"] == "gemini"
    m.update("ETH-USD", "hyperliquid", 102.0, 102.01, t, quote="USD", kind="perp")              # a perp: its own kind
    assert ("ETH-USD", "USD", "perp") not in m.open and len(m.open) == 1
    late = t + 30.0
    closed = m.update("ETH-USD", "kraken", 100.50, 100.52, late, quote="USD")                  # 30 s on, coinbase's and gemini's quotes are stale: only kraken is fresh
    assert closed is not None and closed["sell"] == "gemini" and not m.open
    m2 = monitor(min_edge_bps=30.0)
    m2.update("X-USD", "coinbase", 100.0, 100.02, t, quote="USD")
    m2.update("X-USD", "gemini", 101.00, 101.02, t, quote="USD")                                # 20 net < the 30 bps minimum
    assert not m2.open


def test_same_venue_cannot_be_both_legs_and_sweep_closes_stale_episodes():
    m = monitor()
    t = 1_791_000_000.0
    m.update("SOL-USD", "bybit", 100.30, 100.31, t, quote="USDT")                               # the best bid and the best ask are both bybit's
    m.update("SOL-USD", "binance", 100.00, 100.35, t, quote="USDT")                             # ... so the legs are bybit's bid vs binance's ask? no: 100.30 < 100.35; binance bid vs bybit ask? 100.00 < 100.31
    assert not m.open
    m.update("SOL-USD", "binance", 100.00, 100.05, t, quote="USDT")                             # now bybit's bid clears binance's ask: 25 bps gross, 15.5 fees
    assert m.open and m.open[("SOL-USD", "USDT", "spot")]["buy"] == "binance"
    assert m.sweep(t + 5.0) == [] and m.open
    closed = m.sweep(t + 20.0)                                                                  # nothing heard for 20 s: closed at its last sample
    assert len(closed) == 1 and closed[0]["window_ms"] == 0 and not m.open


def test_manager_hands_every_live_venues_book_to_the_hook():
    c = cfg()
    books = []
    m = SourceManager(c, c.symbols, on_tick=lambda t: None, on_book=lambda v, t: books.append((v, t.symbol, t.bid, t.ask)))
    now = 1_791_000_000.0
    m._route("coinbase", Tick("BTC-USD", now, 100.0, 1.0, True, 99.9, 100.1))
    m._route("binance", Tick("BTC-USD", now, 100.0, 1.0, True, 99.95, 100.05))                 # standby: dropped as a price, kept as a book
    m._route("kraken", Tick("BTC-USD", now, 100.0, 1.0, True))                                  # a trade without a book: nothing
    m._route("sim", Tick("BTC-USD", now, 60000.0, 1.0, True, 59990.0, 60010.0))                 # the simulator's quotes are fiction
    assert books == [("coinbase", "BTC-USD", 99.9, 100.1), ("binance", "BTC-USD", 99.95, 100.05)]
