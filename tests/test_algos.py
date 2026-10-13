"""Execution algorithms (steel/execution/algos.py): the planners' arithmetic, and the runner
sending every child through the executor's gate path (a refusal stops the run; the kill
switch stops it; dry-run children are never sent)."""
import asyncio

from steel.execution import ExecutionGate, Order, OrderResult
from steel.execution.algos import AlgoRunner, plan_chase, plan_scale, plan_twap
from tests.test_execution import Fake, cfg


def test_twap_and_scale_plans():
    o = Order("binance", "BTC-USD", "buy", 1.0, notional_usd=100.0)
    tw = plan_twap(o, slices=4, every_s=30.0)
    assert [c["qty"] for c in tw] == [0.25] * 4 and [c["at_s"] for c in tw] == [0.0, 30.0, 60.0, 90.0] and tw[0]["price"] is None
    tw2 = plan_twap(o, slices=3, limit_bps=10.0, ref_price=100.0)
    assert abs(sum(c["qty"] for c in tw2) - 1.0) < 1e-12 and abs(tw2[0]["price"] - 100.1) < 1e-9
    sc = plan_scale(o, start=100.0, end=96.0, levels=5, front=1.0)
    assert [c["price"] for c in sc] == [100.0, 99.0, 98.0, 97.0, 96.0] and sc[0]["qty"] > sc[-1]["qty"] and abs(sum(c["qty"] for c in sc) - 1.0) < 1e-12
    sell = plan_scale(Order("binance", "BTC-USD", "sell", 2.0), start=100.0, end=102.0, levels=3)
    assert [c["price"] for c in sell] == [100.0, 101.0, 102.0] and all(abs(c["qty"] - 2 / 3) < 1e-9 for c in sell) and abs(sum(c["qty"] for c in sell) - 2.0) < 1e-12
    try:
        plan_scale(o, start=100.0, end=104.0)
        raise AssertionError("a buy ladder running up was accepted")
    except ValueError:
        pass
    ch = plan_chase(o, every_s=2.0, max_chase_bps=15.0)
    assert ch["every_s"] == 2.0 and ch["max_chase_bps"] == 15.0 and ch["qty"] == 1.0


async def _noop(_s):
    return None


def test_runner_twap_through_the_gate(tmp_path):
    g = ExecutionGate(cfg(), tmp_path)
    ex = Fake(g, dry_run=True, key="k", secret="s")
    runner = AlgoRunner(g, {"binance": ex}, sleep=_noop)
    o = Order("binance", "BTC-USD", "buy", 0.04, notional_usd=800.0)        # 4 slices of 200 USD: under every cap
    run = asyncio.run(runner.run("twap", o, slices=4, every_s=0.0))
    assert len(run["results"]) == 4 and all(r["status"] == "dry-run" for r in run["results"]) and run["stopped"] == "" and "ended" in run
    assert len(ex.sent) == 0 if hasattr(ex, "sent") else True                    # dry-run: nothing left the box
    # a venue that is not armed: the first child is refused and the run stops there
    o2 = Order("kraken", "BTC-USD", "buy", 0.04, notional_usd=800.0)
    run2 = asyncio.run(runner.run("twap", o2, slices=4, every_s=0.0))
    assert len(run2["results"]) == 1 and run2["results"][0]["status"] == "refused" and "no order client" in run2["stopped"]
    runner2 = AlgoRunner(g, {"kraken": Fake(g, dry_run=True, key="k", secret="s")}, sleep=_noop)
    run3 = asyncio.run(runner2.run("twap", o2, slices=2, every_s=0.0))
    assert run3["results"][0]["status"] == "refused" and "not armed" in run3["stopped"]
    # the kill switch stops a run between slices
    g.kill("test")
    run4 = asyncio.run(runner.run("twap", o, slices=3, every_s=0.0))
    assert run4["stopped"] == "kill switch" and run4["results"] == []
    g.revive()
    assert "unknown" in asyncio.run(runner.run("vwap", o))["stopped"]


def test_runner_chase_repegs_and_stops_past_the_limit(tmp_path):
    g = ExecutionGate(cfg(), tmp_path)
    ex = Fake(g, dry_run=True, key="k", secret="s")
    quotes = iter([(100.0, 100.1), (100.05, 100.15), (100.5, 100.6)])        # the bid runs away: 50 bps on the third peg
    runner = AlgoRunner(g, {"binance": ex}, book=lambda s: next(quotes), sleep=_noop)
    o = Order("binance", "BTC-USD", "buy", 0.005, notional_usd=500.0)
    run = asyncio.run(runner.run("chase", o, every_s=0.0, max_chase_bps=20.0, max_repegs=5, finish_at_market=False))
    assert [r["price"] for r in run["results"]] == [100.0, 100.05] and "past the limit" in run["stopped"]
    assert run["results"][0]["note"] == "chase peg 1"


def test_engine_algo_action_runs_through_the_gate(tmp_path):
    """The API path on a stand-in engine: validation, the trace line on a real channel, the
    run started on the loop, and a child refused by the gate when the venue is not armed."""
    from types import SimpleNamespace

    from steel.engine import Engine
    from steel.trace import Trace

    g = ExecutionGate(cfg(execution=True, execution_venues="binance"), tmp_path)
    fake = SimpleNamespace(cfg=g.cfg, symbols=["BTC-USD"], prices={"BTC-USD": {"price": 100.0, "bid": 99.9, "ask": 100.1}}, execution=g,
                           executors={"kraken": Fake(g, dry_run=True, key="k", secret="s")}, trace=Trace(lambda m: None))
    fake.algo_status = lambda: Engine.algo_status(fake)
    assert "kind must be" in Engine.algo_action(fake, {"kind": "vwap"})["error"]
    assert "a modeled pair" in Engine.algo_action(fake, {"kind": "twap", "pair": "doge", "side": "buy", "usd": 10})["error"]
    assert "qty" in Engine.algo_action(fake, {"kind": "twap", "pair": "btc", "side": "buy"})["error"]

    async def go():
        res = Engine.algo_action(fake, {"kind": "twap", "pair": "btc", "side": "buy", "usd": 100, "venue": "kraken", "params": {"slices": 2, "every_s": 0}})
        await asyncio.sleep(0.05)
        return res
    res = asyncio.run(go())
    assert res["started"] == "twap" and res["order"]["qty"] == 1.0 and res["execution"] is True
    run = fake._algos.runs[-1]
    assert run["results"][0]["status"] == "refused" and "not armed" in run["stopped"]
    assert any("algo twap" in ev.get("text", "") for ev in fake.trace.buffers["operator"])
