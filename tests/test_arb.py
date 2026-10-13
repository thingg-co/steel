"""The arbitrage executor: it acts only on the monitor's openings that clear the edge, with both
venues armed and funded, sends both legs through the gate, flattens an unhedged leg, and unwinds
a perp pair when the episode closes."""
import asyncio
import json
import types

from steel.arb import ArbExecutor
from steel.config import Config
from steel.dislocation import DislocationMonitor
from steel.execution import ExecutionGate, Executor, OrderResult


def cfg():
    c = Config()
    c.execution = True; c.execution_venues = "binance,bybit,coinbase,kraken"; c.arb = True
    c.exec_max_order_usd = 5_000.0; c.exec_max_name_usd = 50_000.0; c.exec_max_venue_usd = 50_000.0; c.exec_max_day_usd = 100_000.0
    c.exec_max_exposure_frac = 0.0; c.arb_min_edge_bps = 5.0; c.arb_max_order_usd = 200.0; c.arb_cooldown_s = 0.0
    return c


def fake(vid, ok=True, fail_side=None):
    class F(Executor):
        id = vid; label = vid; venue = "perp"; fields = []; endpoints = ("/x",); HOST = "https://example.invalid"
        sent: list = []

        async def _place(self, order):
            F.sent.append(order)
            if not ok or (fail_side and order.side == fail_side and not order.reduce_only):
                return OrderResult(False, self.id, status="rejected", error="nope")
            return OrderResult(True, self.id, order_id=f"{vid}-{len(F.sent)}", status="filled", filled_qty=order.qty, avg_price=order.price)
    return F


async def run(coro):
    return await coro


def setup(tmp_path, kinds=None, **ex):
    c = cfg()
    gate = ExecutionGate(c, tmp_path)
    execs = {k: cls(gate, dry_run=False) for k, cls in ex.items()}
    kinds = kinds or {k: "perp" for k in ex}
    arb = ArbExecutor(c, execs, gate, venue_kind=kinds, state_dir=tmp_path)
    mon = DislocationMonitor(lambda v: 5.0, log_path=tmp_path / "d.jsonl")
    mon.on_open, mon.on_close = arb.consider, arb.on_close
    return c, gate, execs, arb, mon


def test_two_legs_on_a_qualifying_episode_and_unwind_on_close(tmp_path):
    B, Y = fake("binance"), fake("bybit")
    c, gate, execs, arb, mon = setup(tmp_path, binance=B, bybit=Y)

    async def go():
        t = 1_791_000_000.0
        mon.update("ETH-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp", ask_size=5.0)
        mon.update("ETH-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp", bid_size=1.0)   # 28 bps gross, 10 fees: 18 net
        await asyncio.sleep(0.05)
        assert arb.stats["considered"] == 1 and arb.stats["filled_pairs"] == 1 and len(arb.pairs) == 1
        legs = sorted(B.sent + Y.sent, key=lambda o: o.side)
        assert [(o.venue, o.side, o.kind, o.reduce_only) for o in legs] == [("binance", "buy", "perp", False), ("bybit", "sell", "perp", False)]
        assert legs[0].qty == legs[1].qty and abs(legs[0].qty * 100.15 - 100.15) < 1e-6          # the bid side's 1.0 top of book, not the $200 cap
        assert legs[0].price == 100.02 and legs[1].price == 100.30
        mon.update("ETH-USD", "binance", 100.30, 100.32, t + 3.0, quote="USDT", kind="perp")           # the gap closes
        await asyncio.sleep(0.05)
        assert not arb.pairs and arb.stats["closed_pairs"] == 1
        unwind = [o for o in B.sent + Y.sent if o.reduce_only]
        assert {(o.venue, o.side) for o in unwind} == {("binance", "sell"), ("bybit", "buy")}
        rows = [json.loads(x) for x in (tmp_path / "arb.jsonl").read_text().splitlines()]
        assert [r["event"] for r in rows] == ["open", "close"] and rows[0]["legs"][0]["ok"] and rows[1]["held_s"] >= 0
        st = arb.status()
        assert st["enabled"] and st["stats"]["mean_edge_bps"] > 17
    asyncio.run(go())


def test_refusals_thin_edge_unarmed_mixed_kinds_and_funding(tmp_path):
    B, Y = fake("binance"), fake("bybit")
    c, gate, execs, arb, mon = setup(tmp_path, kinds={"binance": "perp", "bybit": "spot"}, binance=B, bybit=Y)

    async def go():
        t = 1_791_000_000.0
        mon.update("ETH-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("ETH-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")                  # the monitor groups by its own kind; the executor checks the venues'
        await asyncio.sleep(0.02)
        assert arb.stats["refused"] == 1 and "basis trade" in arb.recent[-1]["why"] and not B.sent
        arb.venue_kind["bybit"] = "perp"
        c.arb_min_edge_bps = 30.0
        mon.update("SOL-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("SOL-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")
        await asyncio.sleep(0.02)
        assert "under the 30 bps minimum" in arb.recent[-1]["why"]
        c.arb_min_edge_bps = 5.0
        c.execution_venues = "binance"
        mon.update("DOGE-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("DOGE-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")
        await asyncio.sleep(0.02)
        assert "bybit is not armed" in arb.recent[-1]["why"]
        c.execution_venues = "binance,bybit"
        arb.portfolio = {"accounts": [{"id": "binance", "cash": 10.0, "positions": []}, {"id": "bybit", "cash": 10_000.0, "positions": []}]}
        mon.update("LINK-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("LINK-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")
        await asyncio.sleep(0.05)
        assert arb.recent[-1]["event"] == "unfunded" and "collateral on binance" in arb.recent[-1]["why"] and not B.sent
        c.arb = False
        mon.update("AVAX-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("AVAX-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")
        await asyncio.sleep(0.02)
        assert arb.stats["considered"] == 4 and not arb.status()["enabled"]                            # off: not even considered
    asyncio.run(go())


def test_an_unhedged_leg_is_flattened(tmp_path):
    B, Y = fake("binance"), fake("bybit", fail_side="sell")
    c, gate, execs, arb, mon = setup(tmp_path, binance=B, bybit=Y)

    async def go():
        t = 1_791_000_000.0
        mon.update("ETH-USD", "binance", 100.00, 100.02, t, quote="USDT", kind="perp")
        mon.update("ETH-USD", "bybit", 100.30, 100.32, t, quote="USDT", kind="perp")
        await asyncio.sleep(0.05)
        assert arb.stats["unhedged"] == 1 and arb.stats["filled_pairs"] == 0 and not arb.pairs
        flat = [o for o in B.sent if o.reduce_only]
        assert len(flat) == 1 and flat[0].side == "sell" and flat[0].venue == "binance"              # the filled buy on binance is flattened there
        assert arb.recent[-1]["flatten"]["ok"]
    asyncio.run(go())
