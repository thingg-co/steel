"""Alerts (queue, throttle, delivery), the panic button (cancel, flatten, demote), the dead-man
rule, the clock rule in the gate, and backup/restore round trips."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from steel import backup
from steel.alerts import Alerts
from steel.execution import Order, OrderResult
from steel.execution.base import ExecutionGate, Executor
from steel.live import LiveBook
from steel.trace import Trace


def test_alerts_queue_throttle_and_delivery(monkeypatch):
    a = Alerts({"discord_webhook": "https://discord.test/hook", "webhook_url": "https://hook.test/x"}, host="box")
    assert a.configured and a.status()["channels"] == ["discord", "webhook"]
    assert a.send("signal", "BUY BTC", key="BTC-USD", now=100.0)
    assert not a.send("signal", "BUY BTC again", key="BTC-USD", now=200.0)          # throttled per kind+key
    assert a.send("signal", "SELL ETH", key="ETH-USD", now=200.0)
    assert a.send("kill", "kill", now=201.0, level="error") and a.send("kill", "kill", now=201.5, level="error")    # never throttled
    posts = []

    async def fake_post(self, url, json=None):
        posts.append((url, json))
        return SimpleNamespace(status_code=204)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(a.flush()) == 4 and not a.queue and a.sent == 4
    assert {u for u, _ in posts} == {"https://discord.test/hook", "https://hook.test/x"}
    disc = next(j for u, j in posts if "discord" in u)
    assert disc["content"].startswith("Steel @ box") and "!! kill" in disc["content"] and "BUY BTC" in disc["content"]
    hook = next(j for u, j in posts if "hook.test" in u)
    assert len(hook["alerts"]) == 4 and hook["alerts"][0]["kind"] == "signal"
    # unconfigured: nothing queues; the trace hook alerts on error only
    b = Alerts({})
    assert not b.send("kill", "x") and not b.configured
    tr = Trace(lambda ev: None)
    tr.on_emit = a.on_trace
    tr.emit("system", "KILL SWITCH on", "error"); tr.emit("system", "fine", "info")
    assert len(a.queue) == 1 and a.queue[0]["level"] == "error"


def test_alerts_delivery_failure_counts(monkeypatch):
    a = Alerts({"telegram_token": "t", "telegram_chat": "c"})
    a.send("fill", "x")

    async def bad_post(self, url, json=None):
        return SimpleNamespace(status_code=500)
    monkeypatch.setattr(httpx.AsyncClient, "post", bad_post)
    assert asyncio.run(a.flush()) == 0 and a.failed == 1 and "HTTP 500" in a.last_error


class FakeEx(Executor):
    id = "fakex"; label = "Fake"; venue = "perp"; fields = []; endpoints = []; HOST = "https://x"

    def __init__(self, gate, opens=None):
        super().__init__(gate, dry_run=True)
        self.opens = opens or []
        self.placed = []
        self.cancelled = []

    @property
    def configured(self):
        return True

    async def _place(self, order):
        self.placed.append(order)
        return OrderResult(True, self.id, order_id="o1", status="filled", filled_qty=order.qty, avg_price=100.0)

    async def open_orders(self, symbol=None):
        return self.opens

    async def _cancel(self, order_id, symbol):
        self.cancelled.append(order_id)
        return OrderResult(True, self.id, order_id=order_id, status="cancelled")


def live_cfg(**kw):
    return SimpleNamespace(execution=True, execution_venues="fakex", exec_max_order_usd=1e6, exec_max_name_usd=1e6, exec_max_venue_usd=1e6,
                           exec_max_day_usd=1e6, exec_max_exposure_frac=0.0, exec_max_leverage=2.0, exec_reject_cooldown_s=0.0,
                           trade_venue_spot="fakex", trade_venue_perp="fakex", live_scale=1.0, live_drift_tol=0.25, live_max_drawdown=0.05,
                           clock_max_offset_s=1.0, **kw)


def test_flatten_cancels_closes_and_demotes(tmp_path):
    cfg = live_cfg()
    gate = ExecutionGate(cfg, tmp_path)
    ex = FakeEx(gate, opens=[{"order_id": "rest1", "symbol": "BTC-USD"}])
    said = []
    book = LiveBook(cfg, {"fakex": ex}, gate, tmp_path, trace=SimpleNamespace(emit=lambda ch, t, lvl="info", data=None: said.append((lvl, t))))
    book.live = True
    book.expected = {"BTC-USD": 0.5, "ETH-USD": -2.0}
    book.kinds = {"BTC-USD": "perp", "ETH-USD": "perp"}
    assert book.stale() == 0.0
    res = asyncio.run(book.flatten({"BTC-USD": {"price": 100.0}, "ETH-USD": {"price": 10.0}}, reason="test"))
    assert res["ok"] and res["left"] == [] and ex.cancelled == ["rest1"]
    sides = {o.symbol: (o.side, o.qty, o.reduce_only) for o in ex.placed}
    assert sides == {"BTC-USD": ("sell", 0.5, True), "ETH-USD": ("buy", 2.0, True)}
    assert not book.live and book.demoted.startswith("flattened (test)") and book.expected == {}
    assert any("FLATTEN" in t for _, t in said)
    saved = json.loads((tmp_path / "live.json").read_text())
    assert saved["heartbeat"] > 0 and saved["kinds"] == {}
    # the heartbeat drives the dead-man rule
    book.heartbeat = time.time() - 3600
    assert 59 < book.stale() < 61


def test_gate_refuses_on_clock_drift(tmp_path):
    cfg = live_cfg()
    gate = ExecutionGate(cfg, tmp_path)
    o = Order("fakex", "BTC-USD", "buy", 0.1, notional_usd=100.0)
    assert gate.check(o) == ""
    gate.clock = {"offset_s": 2.5, "venues": {"binance": 2.5}, "checked": time.time()}
    assert "clock" in gate.check(o) and not gate.status()["clock"]["ok"]
    gate.clock["offset_s"] = -0.4
    assert gate.check(o) == "" and gate.status()["clock"]["ok"]


def test_clock_measure_takes_the_median(monkeypatch):
    from steel import clockcheck as C
    now = time.time()
    answers = {"binance": {"serverTime": int((now - 0.5) * 1000)}, "bybit": {"result": {"timeSecond": str(now - 0.6)}}, "coinbase": {"epoch": now + 9.0}}

    async def fake_get(self, url):
        name = next(n for n, u, _ in C.SOURCES if u == url)
        return SimpleNamespace(json=lambda: answers[name])
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    r = asyncio.run(C.measure())
    assert set(r["venues"]) == {"binance", "bybit", "coinbase"} and 0.3 < r["offset_s"] < 0.9     # the median ignores the odd one


def test_backup_and_restore_round_trip(tmp_path, monkeypatch):
    state = tmp_path / "state"; state.mkdir()
    (state / "model.pt").write_bytes(b"model")
    (state / "vault.key").write_text("k")
    (state / "replay.npz").write_bytes(b"replay")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "coinbase.json").write_text('{"sealed": 1}')
    out = tmp_path / "bk"
    p1 = backup.make(state, out, now=1_700_000_000)
    names = backup.contents(p1)
    assert "state/model.pt" in names and "state/vault.key" in names and "keys/coinbase.json" in names and "state/replay.npz" not in names
    p2 = backup.make(state, out, replay=True, keep=1, now=1_700_000_100)
    assert "state/replay.npz" in backup.contents(p2) and sorted(out.glob("steel-*.tar.gz")) == [p2]
    dest = tmp_path / "restored"; keys = tmp_path / "keys"; keys.mkdir()
    written = backup.restore(p2, dest, keys)
    assert (dest / "model.pt").read_bytes() == b"model" and (keys / "coinbase.json").read_text() == '{"sealed": 1}' and len(written) == 4
    # a hostile member is refused
    import tarfile, io
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("state/../evil"); info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        backup.restore(bad, dest)


def test_account_history_and_csv_exports(tmp_path):
    from steel.engine import Engine
    from steel.paper import PaperBook
    from collections import deque
    e = SimpleNamespace(cfg=SimpleNamespace(state_dir=str(tmp_path)), _history_at=0.0)
    e.account_history = lambda days=30.0: Engine.account_history(e, days)
    pf = {"accounts": [{"id": "coinbase", "liquidation": 1000.0, "cash": 100.0}, {"id": "kraken", "liquidation": 500.0, "cash": 50.0, "error": ""},
                       {"id": "bybit", "error": "no key"}]}
    Engine._record_account_history(e, pf)
    Engine._record_account_history(e, pf)                                     # five-minute throttle: one row
    h = Engine.account_history(e, days=1)
    assert len(h["rows"]) == 1 and h["rows"][0]["total"] == 1500.0 and h["venues"] == ["coinbase", "kraken"]
    e.paper = PaperBook(1000.0)
    e.paper.trades = deque([{"t": 2.0, "sym": "BTC-USD", "side": "sell", "shares": 0.1, "price": 110.0, "notional": 11.0},
                            {"t": 1.0, "sym": "BTC-USD", "side": "buy", "shares": 0.1, "price": 100.0, "notional": 10.0}])
    name, text = Engine.export_csv(e, "paper-trades")
    lines = text.strip().splitlines()
    assert name.startswith("steel-paper-trades-") and lines[0].startswith("time,utc,symbol,side") and lines[1].split(",")[3] == "buy" and lines[2].split(",")[3] == "sell"
    name, text = Engine.export_csv(e, "account-history")
    assert "coinbase_equity" in text.splitlines()[0] and "1500.0" in text.splitlines()[1]
    (tmp_path / "orders.jsonl").write_text(json.dumps({"t": 5.0, "event": "refused", "order": {"venue": "bybit", "qty": 1}, "result": {"ok": False}}) + "\n")
    name, text = Engine.export_csv(e, "orders")
    head = text.splitlines()[0].split(",")
    assert "order.venue" in head and "result.ok" in head and text.splitlines()[1].startswith("1970-01-01T00:00:05Z")
    assert Engine.export_csv(e, "nope") is None


def test_fee_tiers_replace_the_table_and_the_book_follows():
    from steel.engine import Engine
    from steel.paper import PaperBook
    from steel.sources.fees import fee_profile
    e = SimpleNamespace(cfg=SimpleNamespace(spot_fee_bps=0.0, perp_fee_bps=0.0), fees={"spot": fee_profile("coinbase"), "perp": fee_profile("hyperliquid")},
                        paper=PaperBook(1000.0, cost_bps=40.0, perp_fee_bps=4.5), fee_tiers={})
    changed = Engine.apply_fee_tier(e, "coinbase", {"spot_taker_bps": 25.0, "spot_maker_bps": 15.0}, when=5.0)
    assert changed == {"spot": {"spot_taker_bps": (40.0, 25.0), "spot_maker_bps": (25.0, 15.0)}}
    assert e.fees["spot"].spot_taker_bps == 25.0 and e.paper.cost_bps == 25.0 and e.paper.perp_fee_bps == 4.5 and e.fee_tiers["coinbase"]["t"] == 5.0
    assert Engine.apply_fee_tier(e, "coinbase", {"spot_taker_bps": 25.0}) == {}                 # nothing new
    assert Engine.apply_fee_tier(e, "bybit", {"perp_taker_bps": 3.0}) == {} and "bybit" in e.fee_tiers   # not a trade venue: recorded, not applied
    e.cfg.perp_fee_bps = 9.0                                                                  # an explicit control wins over the tier
    Engine.apply_fee_tier(e, "hyperliquid", {"perp_taker_bps": 3.5})
    assert e.fees["perp"].perp_taker_bps == 3.5 and e.paper.perp_fee_bps == 4.5


def test_monitor_and_ticket():
    import numpy as np
    from collections import deque
    from steel.engine import Engine
    from steel.sources.fees import fee_profile
    outs = {"BTC-USD": deque([{"hit": True, "inside": True, "y": 10.0, "q50": 8.0, "pnl": 2.0}, {"hit": False, "inside": False, "y": -30.0, "q50": 5.0, "pnl": -4.0}]),
            "ETH-USD": deque([{"hit": None}])}
    x = np.zeros((300, 2, 3), dtype=np.float32); x[:, :, 1] = 3.0; x[:, :, 2] = np.random.default_rng(0).normal(0, 1, (300, 2))
    e = SimpleNamespace(outcomes=outs, learner=SimpleNamespace(size=300, store=SimpleNamespace(x=x)), cfg=SimpleNamespace(bars_per_day=288, state_dir="/nonexistent"),
                        metrics=SimpleNamespace(as_dict=lambda: {"trusted": True}))
    import steel.engine as E
    old = E.FEATURES
    E.FEATURES = ["ret", "vol", "flow"]
    try:
        m = Engine.monitor(e)
    finally:
        E.FEATURES = old
    assert m["calibration"] == [{"symbol": "BTC-USD", "n": 2, "hit": 0.5, "inside": 0.5, "mae_bps": 18.5, "pnl_bps": -2.0}]
    flags = {d["feature"]: d["flag"] for d in m["drift"]}
    assert flags == {"ret": "dead", "vol": "shifted", "flow": ""} and m["scorecard"] == []
    # the net's input weight per feature rides along when the learner has a model
    import torch
    e.learner.model = SimpleNamespace(inp=SimpleNamespace(weight=torch.tensor([[1.0, 0.0, 2.0], [0.0, 0.0, 2.0]])))
    E.FEATURES = ["ret", "vol", "flow"]
    try:
        m2 = Engine.monitor(e)
    finally:
        E.FEATURES = old
    w = {d["feature"]: d["weight"] for d in m2["drift"]}
    assert w["vol"] == 0.0 and abs(w["ret"] + w["vol"] + w["flow"] - 1.0) < 1e-6 and w["flow"] > w["ret"]
    # the ticket
    e2 = SimpleNamespace(cfg=SimpleNamespace(long_mode="perp", trade_venue_perp="hyperliquid", trade_venue_spot="coinbase", live_scale=0.1, stop_loss_bps=300.0,
                                             take_profit_bps=600.0, max_hold_bars=12, bar_seconds=300.0, short_leverage=2.0, long_leverage=1.0, paper_start=100000.0),
                         latest={"BTC-USD": {"side": -1, "action": "SELL", "size": 0.1, "venue": "hyperliquid", "q": [-50, -30, -20, -5, 10], "p_up": 0.3, "p_down": 0.65, "why": "leaning in", "liq": 150.0}},
                         prices={"BTC-USD": {"price": 100.0, "bid": 99.9, "ask": 100.1}}, executors={}, _book_equity=50_000.0, fees={"perp": fee_profile("hyperliquid")},
                         metrics=SimpleNamespace(trusted=True))
    t = Engine.ticket(e2, "BTC-USD")
    assert t["venue"] == "hyperliquid" and t["kind"] == "perp" and t["leverage"] == 2.0 and t["qty"] == 50.0 and t["paper_notional_usd"] == 5000.0 and t["live_notional_usd"] == 500.0
    assert t["limit"] == 99.9 and t["stop"] == 103.0 and t["target"] == 94.0 and t["liquidation"] == 150.0 and t["time_limit_s"] == 3600.0
    assert "SELL / SHORT BTC-USD on hyperliquid (perp, 2x isolated)" in t["text"] and "limit 99.9" in t["text"] and "stop 103" in t["text"]
    e2.latest["BTC-USD"] = {"side": 0, "action": "HOLD", "size": 0, "why": "holding: thin"}
    assert "no order: holding: thin" in Engine.ticket(e2, "BTC-USD")["text"]
    assert Engine.ticket(e2, "ZZZ-USD")["error"]


def test_period_levels_from_the_replays_closes():
    import numpy as np, time
    from steel.engine import Engine
    now = time.time()
    day = int(now // 86400) * 86400
    t = np.array([day - 86400 + k * 300 for k in range(288)] + [day + k * 300 for k in range(12)], dtype=float)   # yesterday whole, today's first hour
    closes = np.concatenate([np.linspace(100, 110, 288), np.linspace(111, 112, 12)])
    e = SimpleNamespace(symbols=["BTC-USD"], learner=SimpleNamespace(size=len(t), store=SimpleNamespace(t=t, close=closes.reshape(-1, 1))))
    lv = Engine.levels(e, "BTC-USD")
    by = {x["id"]: x["px"] for x in lv["levels"]}
    assert by["do"] == 111.0 and by["dh"] == 112.0 and by["dl"] == 111.0 and by["pdh"] == 110.0 and by["pdl"] == 100.0 and by["pdc"] == 110.0 and by["pdeq"] == 105.0
    assert "wo" in by and "mo" in by and lv["note"] == "from 5-minute closes"
    assert Engine.levels(SimpleNamespace(symbols=["BTC-USD"], learner=SimpleNamespace(size=0, store=SimpleNamespace(t=np.zeros(0), close=np.zeros((0, 1))))), "BTC-USD")["levels"] == []
