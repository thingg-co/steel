"""The live book: the promotion checklist, mirroring paper fills as gated orders, the ledger of
what was sent, reconciliation against the venues' positions, and automatic demotion."""
import asyncio
import time
import types

from steel.config import Config
from steel.execution import ExecutionGate, Executor, OrderResult
from steel.live import LiveBook, Promotion


def cfg():
    c = Config()
    c.execution = True; c.execution_venues = "hyperliquid,coinbase"
    c.exec_max_order_usd = 5_000.0; c.exec_max_name_usd = 20_000.0; c.exec_max_venue_usd = 50_000.0; c.exec_max_day_usd = 100_000.0
    c.exec_max_exposure_frac = 0.0; c.live_scale = 0.1
    return c


def test_promotion_checklist_reads_the_backtest_and_the_paper_book():
    c = cfg()
    p = Promotion(c)
    bt = {"totals": {"days": 12, "net_bps_per_day": 3.1, "hit_rate": 0.55}}
    paper = {"started": time.time() - 10 * 86400, "sharpe": 0.2, "max_drawdown": 2.5, "return_pct": 1.2}
    ok = p.check(bt, paper, True)
    assert ok["eligible"] and len(ok["rows"]) == 8
    bad = p.check({"totals": {"days": 3, "net_bps_per_day": -1.0, "hit_rate": 0.5}}, {**paper, "max_drawdown": 9.0, "return_pct": -0.5}, False)
    assert not bad["eligible"] and [r["name"] for r in bad["rows"] if not r["ok"]] == \
        ["model trusted", "backtest scored days", "backtest net bps / day", "backtest hit rate", "paper max drawdown", "paper return"]
    assert not p.check(None, None, True)["eligible"]


class Fake(Executor):
    id = "hyperliquid"; label = "HL"; venue = "perp"
    fields = [{"id": "address", "label": "a"}]
    endpoints = ("/x",)
    HOST = "https://example.invalid"
    sent: list = []

    async def _place(self, order):
        Fake.sent.append(order)
        return OrderResult(True, self.id, order_id=f"o{len(Fake.sent)}", status="filled", filled_qty=order.qty,
                           avg_price=100.2 if order.side == "buy" else 99.8)


def test_mirror_sends_gated_orders_and_keeps_the_ledger(tmp_path):
    c = cfg()
    gate = ExecutionGate(c, tmp_path)
    ex = Fake(gate, dry_run=False, address="0x" + "ab" * 20)
    book = LiveBook(c, {"hyperliquid": ex}, gate, tmp_path)
    paper = {"started": time.time() - 10 * 86400, "sharpe": 0.2, "max_drawdown": 2.5, "return_pct": 1.2}
    check = Promotion(c).check({"totals": {"days": 12, "net_bps_per_day": 3.1, "hit_rate": 0.55}}, paper, True)
    assert "not eligible" in book.promote(Promotion(c).check(None, None, False))["error"]
    assert book.promote(check)["ok"] and book.live
    trades = [{"t": 2.0, "sym": "ETH-USD", "side": "open short", "shares": 5.0, "price": 100.0, "notional": 500.0},
              {"t": 1.0, "sym": "BTC-USD", "side": "open long", "shares": 2.0, "price": 100.0, "notional": 200.0},
              {"t": 0.5, "sym": "SOL-USD", "side": "liquidated", "shares": 1.0, "price": 100.0}]   # newest first, as the paper deque keeps them
    res = asyncio.run(book.mirror(trades))
    assert [o.symbol for o in Fake.sent] == ["BTC-USD", "ETH-USD"] and Fake.sent[0].qty == 0.2 and Fake.sent[0].side == "buy" and Fake.sent[1].side == "sell"
    assert Fake.sent[0].kind == "perp" and not Fake.sent[0].reduce_only and Fake.sent[0].notional_usd == 20.0
    assert len(res) == 2 and all(r["ok"] for r in res)
    assert book.expected == {"BTC-USD": 0.2, "ETH-USD": -0.5} and len(book.slippage) == 2 and book.slippage[0] == 20.0 and book.slippage[1] == 20.0
    asyncio.run(book.mirror([{"t": 3.0, "sym": "ETH-USD", "side": "close short", "shares": 5.0, "price": 100.0}]))
    assert Fake.sent[-1].reduce_only and Fake.sent[-1].side == "buy" and "ETH-USD" not in book.expected
    spot = asyncio.run(book.mirror([{"t": 4.0, "sym": "DOGE-USD", "side": "buy", "shares": 10.0, "price": 1.0}]))
    assert spot[0]["status"] == "refused" and "coinbase has no order client" in spot[0]["error"]          # spot venue armed but no client
    again = LiveBook(c, {"hyperliquid": ex}, gate, tmp_path)                                               # state survives
    assert again.live and again.expected == {"BTC-USD": 0.2} and len(again.slippage) == 3
    gate2 = ExecutionGate(cfg(), tmp_path / "other")
    gate2.cfg.execution = False
    assert "execution is off" in LiveBook(gate2.cfg, {}, gate2, tmp_path / "other").promote(check)["error"]


def test_reconcile_measures_drift_and_demotes_on_breach(tmp_path):
    c = cfg()
    gate = ExecutionGate(c, tmp_path)
    book = LiveBook(c, {}, gate, tmp_path)
    book.live, book.expected = True, {"BTC-USD": 0.2, "ETH-USD": -0.5}
    pf = {"accounts": [{"id": "hyperliquid", "liquidation": 10_000.0, "positions": [{"symbol": "BTC-USD", "qty": 0.2}, {"symbol": "ETH-USD", "qty": -0.48}]},
                       {"id": "kraken", "liquidation": 99_999.0, "positions": [{"symbol": "BTC-USD", "qty": 5.0}]}]}   # not a trade venue: ignored
    r = book.reconcile(pf)
    assert r["equity"] == 10_000.0 and r["drift"] == {"BTC-USD": 0.0, "ETH-USD": 0.04} and r["breach"] == "" and book.live
    r = book.reconcile({"accounts": [{"id": "hyperliquid", "liquidation": 9_000.0, "positions": [{"symbol": "BTC-USD", "qty": 0.2}, {"symbol": "ETH-USD", "qty": -0.5}]}]})
    assert "drawdown 10.0%" in r["breach"] and not book.live and "drawdown" in book.demoted
    book.live, book.peak_equity = True, 0.0
    r = book.reconcile({"accounts": [{"id": "hyperliquid", "liquidation": 10_000.0, "positions": [{"symbol": "BTC-USD", "qty": 0.05}, {"symbol": "ETH-USD", "qty": -0.5}]}]})
    assert "drift 75% on BTC" in r["breach"] and not book.live
    book.live = True
    gate.kill("test")
    assert book.reconcile(pf)["breach"] == "kill switch" and not book.live
    st = book.status({"eligible": False, "rows": []})
    assert st["live"] is False and st["demoted"] == "kill switch" and st["checklist"]["eligible"] is False
