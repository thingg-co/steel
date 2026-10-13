"""The execution gate: the switch, the arming, the kill switch, every cap, the cooldown, the
audit log, and a client that never sends in dry-run."""
import asyncio
import json
import types

from steel.config import Config
from steel.execution import ExecutionGate, Executor, Order, OrderResult


def cfg(**kw):
    c = Config()
    c.execution = True
    c.execution_venues = "binance,bybit"
    c.exec_max_order_usd = 1_000.0
    c.exec_max_name_usd = 2_500.0
    c.exec_max_venue_usd = 3_000.0
    c.exec_max_day_usd = 5_000.0
    c.exec_max_exposure_frac = 0.5
    c.exec_max_leverage = 2.0
    c.exec_reject_cooldown_s = 300.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def order(venue="binance", sym="BTC-USD", notional=800.0, **kw):
    return Order(venue, sym, "buy", 0.01, notional_usd=notional, **kw)


def test_gate_checks_every_rule_in_order(tmp_path):
    g = ExecutionGate(cfg(execution=False), tmp_path)
    assert "execution is off" in g.check(order())
    g = ExecutionGate(cfg(), tmp_path)
    assert g.check(order()) == ""
    assert "not armed" in g.check(order(venue="okx"))
    assert "malformed" in g.check(Order("binance", "BTC-USD", "hold", 0.01, notional_usd=1.0))
    assert "no reference price" in g.check(order(notional=0.0))
    assert "per-order cap" in g.check(order(notional=1_500.0))
    g2 = ExecutionGate(cfg(exec_max_day_usd=0.0), tmp_path)
    assert "per-day cap is unset" in g2.check(order())
    assert "leverage 3x is over" in g.check(order(), leverage=3.0)
    g.equity_usd, g.exposure_usd = 10_000.0, 4_500.0
    assert "open exposure" in g.check(order(notional=800.0))
    assert g.check(order(notional=800.0, reduce_only=True)) == ""                    # closing is always allowed by exposure
    g.exposure_usd = 0.0
    now = 1_791_676_800.0
    for _ in range(3):
        g.spent(order(), now)                                                        # 2,400 spent on BTC on binance today
    assert "BTC would pass its daily cap" in g.check(order(notional=200.0), now)
    assert g.check(order(sym="ETH-USD", notional=500.0), now) == ""
    g.spent(order(sym="ETH-USD", notional=500.0), now)
    assert "binance would pass its daily cap" in g.check(order(sym="ETH-USD", notional=200.0), now)
    assert g.check(order(venue="bybit", sym="ETH-USD", notional=900.0), now) == ""
    g.spent(order(venue="bybit", sym="ETH-USD", notional=900.0), now)                # 3,800 today
    g.spent(order(venue="bybit", sym="SOL-USD", notional=900.0), now)                # 4,700
    assert "day's cap" in g.check(order(venue="bybit", sym="SOL-USD", notional=400.0), now)
    assert g.check(order(venue="bybit", sym="SOL-USD", notional=400.0), now + 86_400.0) == ""   # a new UTC day
    g.rejects["bybit"] = now
    assert "cooling down" in g.check(order(venue="bybit", sym="DOGE-USD", notional=100.0), now + 10)
    assert g.check(order(venue="bybit", sym="DOGE-USD", notional=100.0), now + 400) == ""


def test_kill_switch_flag_and_file(tmp_path):
    g = ExecutionGate(cfg(), tmp_path)
    assert g.check(order()) == ""
    g.kill("drawdown")
    assert "kill switch" in g.check(order()) and (tmp_path / "KILL").exists() and g.status()["killed"]
    g.revive()
    assert g.check(order()) == "" and not (tmp_path / "KILL").exists()
    (tmp_path / "KILL").write_text("by hand\n")                                      # the file alone is enough
    assert "state/KILL exists" in g.check(order())
    st = g.status()
    assert st["enabled"] and st["armed"] == ["binance", "bybit"] and st["caps"]["order_usd"] == 1_000.0 and st["killed"]


class Fake(Executor):
    id = "binance"
    label = "Binance"
    fields = [{"id": "key", "label": "k"}, {"id": "secret", "label": "s"}]
    endpoints = ("/api/v3/x",)
    HOST = "https://example.invalid"

    async def _place(self, order):
        req, ans = await self._send("POST", f"{self.HOST}/api/v3/x", json_body={"symbol": order.symbol, "qty": order.qty},
                                    headers={"X-MBX-APIKEY": self.key})
        if ans.get("dry_run"):
            return OrderResult(True, self.id, status="dry-run", request=req)
        return OrderResult(ans.get("_status") == 200, self.id, status="accepted", request=req, raw=ans)


def test_dry_run_client_logs_signs_and_never_sends(tmp_path):
    g = ExecutionGate(cfg(), tmp_path)
    ex = Fake(g, dry_run=True, key="k123", secret="s456")
    res = asyncio.run(ex.place(order()))
    assert res.ok and res.status == "dry-run" and res.request["headers"]["X-MBX-APIKEY"] == "***"   # secrets never reach the log
    assert res.request["json"] == {"symbol": "BTC-USD", "qty": 0.01}
    lines = [json.loads(x) for x in (tmp_path / "orders.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["sending", "result"] and lines[0]["dry_run"] is True
    assert g.spent_day[g.day()] == 800.0                                             # a dry run still counts against the caps
    refused = asyncio.run(ex.place(order(venue="okx")))
    assert not refused.ok and refused.status == "refused" and "not armed" in refused.error
    bare = Fake(g, dry_run=True)
    assert "no trade key" in asyncio.run(bare.place(order())).error
    with_bad_path = Fake(g, dry_run=True, key="k", secret="s")

    async def bad(o):
        await with_bad_path._send("POST", f"{with_bad_path.HOST}/api/v3/other", json_body={})
    with_bad_path._place = bad
    res = asyncio.run(with_bad_path.place(order()))
    assert res.status == "failed" and "not a declared order endpoint" in res.error
    assert Fake.describe()["file"] == "binance-trade.json" and Fake.describe()["venue"] == "perp"
