"""The trade tape: a ring per pair under a budget, bands, delta, CVD and the venue share."""
import time

import numpy as np

from steel.feed import Tick
from steel.sources.manager import SourceManager
from steel.tape import BANDS, Tape


def test_tape_rings_bands_and_cvd():
    t = Tape(["BTC-USD", "ETH-USD"], mb=0.1, keep_s=3600)       # tiny budget: a small ring per pair
    now = time.time()
    assert t.cap >= 1000 and t.status()["pairs"] == 2
    assert t.add("coinbase", "BTC-USD", now - 100, 100.0, 1.0, True)        # 100 USD buy
    assert t.add("kraken", "BTC-USD", now - 50, 100.0, 60.0, False)          # 6,000 USD sell
    assert t.add("coinbase", "BTC-USD", now - 10, 101.0, 300.0, True)        # 30,300 USD buy
    assert not t.add("coinbase", "BTC-USD", now, 101.0, 0.0, True)           # a quote-sized print never lands
    assert not t.add("coinbase", "ZZZ-USD", now, 1.0, 1.0, True)             # an unknown pair never lands
    rec = t.recent("BTC-USD", n=10)
    assert [r["usd"] for r in rec] == [30300.0, 6000.0, 100.0] and rec[0]["venue"] == "coinbase" and rec[1]["side"] == -1
    assert [r["usd"] for r in t.recent("BTC-USD", min_usd=1000)] == [30300.0, 6000.0]
    b = t.bands("BTC-USD", since=now - 3600)
    assert b["n"] == 3 and b["buy"] == 30400.0 and b["sell"] == 6000.0 and b["delta"] == 24400.0
    by = {x["band"]: x for x in b["bands"]}
    assert by["<1k"]["buy"] == 100.0 and by["5k"]["sell"] == 6000.0 and by["25k"]["buy"] == 30300.0 and by["5M+"]["n"] == 0
    assert b["venues"] == {"coinbase": round(30400 / 36400, 3), "kraken": round(6000 / 36400, 3)}
    assert list(Tape.band_index(np.array([0, 999, 1000, 4999, 5_000_000, 9e9]))) == [0, 0, 1, 1, 10, 10]
    c = t.cvd("BTC-USD", 60.0, 5)
    assert len(c["cvd"]) == 5 and c["cvd"][-1] == 24400.0 and sum(c["buy"]) == 30400.0
    big = t.cvd("BTC-USD", 60.0, 5, min_usd=10_000)
    assert big["cvd"][-1] == 30300.0
    # the ring wraps and keeps the newest
    for i in range(t.cap + 5):
        t.add("coinbase", "ETH-USD", now - 1 + i * 1e-4, 10.0, 1.0, True)
    assert t.size["ETH-USD"] == t.cap and t.rows("ETH-USD")["t"][-1] > t.rows("ETH-USD")["t"][0]
    # old prints age out of reads
    t.add("coinbase", "BTC-USD", now - 7200, 100.0, 5.0, True)
    assert t.bands("BTC-USD")["n"] == 3
    # a universe change keeps the pairs that stay
    t.resize(["BTC-USD", "SOL-USD"])
    assert t.bands("BTC-USD")["n"] == 3 and t.size["SOL-USD"] == 0 and "ETH-USD" not in t.rings


def test_manager_feeds_every_live_venues_prints_to_the_tape():
    from tests.test_sources import cfg
    got = []
    m = SourceManager(cfg(sources_on="coinbase,kraken"), ["BTC-USD"], on_tick=lambda t: None, on_trade=lambda v, t: got.append((v, t.price)))
    now = time.time()
    m._route("coinbase", Tick("BTC-USD", now, 100.0, 1.0, True))
    m._route("kraken", Tick("BTC-USD", now, 100.5, 1.0, False))               # standby venue: not the provider, still on the tape
    m._route("kraken", Tick("BTC-USD", now, 100.6, 0.0, None, 100.5, 100.7, quote=True))   # a quote never lands
    m._route("sim", Tick("BTC-USD", now, 60000.0, 1.0, True))                 # the simulator never lands
    assert got == [("coinbase", 100.0), ("kraken", 100.5)]


def test_tape_candles_by_timeframe_and_venue():
    t = Tape(["BTC-USD"], mb=0.1, keep_s=86400)
    base = (int(time.time() // 60) - 5) * 60.0                         # five minutes ago, on the minute
    for i, (v, px, sz, buy) in enumerate([("coinbase", 100.0, 1.0, True), ("kraken", 101.0, 2.0, False), ("coinbase", 99.0, 1.0, True), ("coinbase", 102.0, 0.5, True)]):
        t.add(v, "BTC-USD", base + i * 10, px, sz, buy)                 # all inside one minute
    t.add("kraken", "BTC-USD", base + 70, 103.0, 1.0, False)            # the next minute
    c = t.candles("BTC-USD", 60.0, 10)
    assert len(c) == 2 and c[0]["t"] == base and c[0]["o"] == 100.0 and c[0]["h"] == 102.0 and c[0]["l"] == 99.0 and c[0]["c"] == 102.0
    assert c[0]["v"] == 4.5 and c[0]["vb"] == 100.0 + 99.0 + 51.0 and c[0]["vs"] == 202.0 and c[0]["n"] == 4
    assert c[1]["o"] == c[1]["c"] == 103.0 and c[1]["n"] == 1
    only = t.candles("BTC-USD", 60.0, 10, venues={"coinbase"})
    assert len(only) == 1 and only[0]["h"] == 102.0 and only[0]["vs"] == 0.0
    assert t.candles("BTC-USD", 60.0, 2, to=base - 100) == []


def test_footprint_rows_poc_value_area_imbalances_and_bubbles():
    t = Tape(["BTC-USD"], mb=0.1, keep_s=86400)
    assert Tape.nice_bucket(83_600.0, 2.0) == 20.0 and Tape.nice_bucket(0.0023, 5.0) == 1e-6 and Tape.nice_bucket(100.0, 2.0) == 0.02
    base = (int(time.time() // 60) - 3) * 60.0
    prints = [(100.00, 10.0, True), (100.00, 1.0, False), (100.02, 30.0, True), (100.02, 2.0, False), (100.04, 5.0, True), (99.98, 1.0, False), (99.98, 20.0, False),
              (100.06, 2000.0, True)]
    for i, (px, sz, buy) in enumerate(prints):
        t.add("coinbase", "BTC-USD", base + i, px, sz, buy)
    fp = t.footprint("BTC-USD", 60.0, 5, bucket_bps=2.0, imb_ratio=3.0, bubble_usd=100_000.0)
    assert fp["bucket"] == 0.02 and len(fp["candles"]) == 1
    c = fp["candles"][0]
    rows = {r["px"]: r for r in c["rows"]}
    assert set(rows) == {99.98, 100.0, 100.02, 100.04, 100.06}
    assert rows[100.02]["a"] == round(30 * 100.02, 2) and rows[100.02]["b"] == round(2 * 100.02, 2) and rows[99.98]["b"] == round(21 * 99.98, 2)
    assert c["poc"] == 100.06 and c["va"][0] <= 100.06 <= c["va"][1]                   # the 2,000-unit print is the point of control
    assert {"px": 100.06, "side": 1} in c["imb"]                                        # buys at 100.06 against the sells at 100.04 (none): stacked buy imbalance
    assert c["buy"] > c["sell"] and c["delta"] == round(c["buy"] - c["sell"], 2) and c["delta_min"] <= c["delta"] <= c["delta_max"]
    assert c["big"] == [{"px": 100.06, "usd": round(2000 * 100.06, 2), "side": 1, "t": base + 7}] and c["max_print"] == round(2000 * 100.06, 2)
    assert len(c["cvd_bands"]) == 11 and c["cvd_bands"][6] == round(2000 * 100.06, 2)   # the 100k band holds the bubble alone
    assert t.footprint("BTC-USD", 60.0, 5, to=base - 600)["candles"] == []


def test_engine_premium_and_basis():
    """Engine.premium on a stand-in: each venue's VWAP against the reference in bps, and the
    perp-against-spot basis (the first live call hit an undefined name here)."""
    from types import SimpleNamespace

    from steel.engine import Engine

    vw = {"t0": 0.0, "tf": 60.0, "n": 3, "venues": {"binance": [100.0, 100.0, None], "coinbase": [100.1, 99.9, 100.0], "hyperliquid": [100.2, 100.2, 100.2]}}
    fake = SimpleNamespace(tape=SimpleNamespace(venue_vwap=lambda pair, tf, n: vw),
                           sources=SimpleNamespace(sources={"binance": SimpleNamespace(venue="spot"), "coinbase": SimpleNamespace(venue="spot"), "hyperliquid": SimpleNamespace(venue="perp")}))
    out = Engine.premium(fake, "BTC-USD", tf=60.0, n=3, reference="binance")
    assert abs(out["premium_bps"]["coinbase"][0] - 10.0) < 0.05 and abs(out["premium_bps"]["coinbase"][1] + 10.0) < 0.05 and out["premium_bps"]["coinbase"][2] is None
    assert "binance" not in out["premium_bps"] and out["kinds"]["hyperliquid"] == "perp"
    assert abs(out["basis_bps"][0] - 15.0) < 0.05 and abs(out["basis_bps"][2] - 20.0) < 0.05   # log of the perp mean over the spot mean, in bps


def test_candles_payload_carries_fills_mirrors_and_transfers():
    """Engine.candles on a stand-in: the paper book's fills for the pair in the window, the
    live mirrors with slippage from the venue's fill against the paper price, and the transfer
    waits (in flight and landed)."""
    from types import SimpleNamespace

    from steel.engine import Engine

    now = 1_000_000.0
    rows = [{"t": now - 600, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vb": 0, "vs": 0, "n": 1}]
    fake = SimpleNamespace(cfg=SimpleNamespace(bar_seconds=300.0, horizon=12), tape=SimpleNamespace(candles=lambda *a, **k: rows, keep_s=86400.0, venues=["coinbase"]),
                           bars={}, liqs=SimpleNamespace(per_bar=lambda *a, **k: {}), outcomes={}, latest={}, prices={}, _quote_of=lambda p: "USD",
                           paper=SimpleNamespace(trades=[{"t": now - 100, "sym": "BTC-USD", "side": "open long", "price": 100.0, "notional": 1000.0, "note": "1x"},
                                                         {"t": now - 100, "sym": "ETH-USD", "side": "buy", "price": 2.0, "notional": 10.0},
                                                         {"t": now - 90000, "sym": "BTC-USD", "side": "buy", "price": 90.0, "notional": 1.0}],
                                                 in_flight=[{"sent": now - 50, "lands_at": now + 100, "amount": 500.0, "rail": "solana", "to": "perp", "asset": "USDC"}],
                                                 transfers=[{"sent": now - 2000, "lands_at": now - 1900, "amount": 200.0, "rail": "solana", "to": "spot", "asset": "USDC", "event": "landed"},
                                                            {"sent": now - 3000, "amount": 200.0, "event": "sent"}]),
                           live=SimpleNamespace(orders=[{"t": now - 100, "ok": True, "venue": "hyperliquid", "status": "filled", "avg_price": 100.1, "paper_price": 100.0, "order": {"symbol": "BTC-USD", "side": "buy"}},
                                                        {"t": now - 100, "ok": False, "venue": "hyperliquid", "status": "refused", "error": "not armed", "avg_price": None, "paper_price": 100.0, "order": {"symbol": "BTC-USD", "side": "sell"}},
                                                        {"t": now - 100, "ok": True, "venue": "x", "avg_price": 1.0, "paper_price": 1.0, "order": {"symbol": "ETH-USD", "side": "buy"}}]))
    out = Engine.candles(fake, "BTC-USD", tf=300.0, n=10, to=now)
    assert [f["side"] for f in out["fills"]] == ["open long"]                      # the pair's fill inside the window only
    assert len(out["mirrors"]) == 2 and abs(out["mirrors"][0]["slippage_bps"] - 10.0) < 0.05 and out["mirrors"][1]["ok"] is False and out["mirrors"][1]["price"] == 100.0
    assert [tr.get("landed", False) for tr in out["transfers"]] == [False, True] and out["transfers"][0]["lands_at"] == now + 100
    assert out["forecast"] is None
