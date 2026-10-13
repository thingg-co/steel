"""Series alerts (steel/rules.py): comparisons, crosses, the cooldown, persistence, the API."""
import json

from steel import rules as R


def rows(**kw):
    base = {"pair": "BTC-USD", "price": 80000.0, "chg_1h_bps": 12.0, "delta_1h": -2.5e6, "oi_chg_1h": 0.01, "funding": 0.0001,
            "liq_long_1h": 3e6, "liq_short_1h": 1e6, "book_imb_05": 0.2, "dislocations": 2, "q50": 30.0, "p_up": 0.6}
    base.update(kw)
    return [base, dict(base, pair="ETH-USD", price=2500.0, delta_1h=5e5, liq_long_1h=0.0)]


def test_add_validates_and_canonicalizes(tmp_path):
    rb = R.RuleBook(tmp_path / "rules.json")
    r = rb.add({"pair": "btc", "series": "delta_1h", "op": "<", "value": "-1000000", "cooldown_s": 5})
    assert r["pair"] == "BTC-USD" and r["value"] == -1e6 and r["cooldown_s"] == R.MIN_COOLDOWN
    for bad in ({"series": "nope", "op": ">", "value": 1}, {"series": "price", "op": "~", "value": 1}, {"series": "price", "op": ">", "value": "x"}):
        try:
            rb.add(bad)
            raise AssertionError("accepted a bad rule")
        except ValueError:
            pass
    assert "delta" in R.describe(r)


def test_evaluate_fires_once_per_cooldown_and_persists(tmp_path):
    p = tmp_path / "rules.json"
    rb = R.RuleBook(p)
    rb.add({"pair": "BTC-USD", "series": "delta_1h", "op": "<", "value": -1e6, "cooldown_s": 600})
    fired = rb.evaluate(rows(), now=1000.0)
    assert len(fired) == 1 and fired[0]["pair"] == "BTC-USD" and "tape delta" in fired[0]["text"]
    assert rb.evaluate(rows(), now=1100.0) == []                   # inside the cooldown
    assert len(rb.evaluate(rows(), now=1700.0)) == 1               # past it
    assert rb.rules[0]["hits"] == 2
    saved = json.loads(p.read_text())
    assert saved["rules"][0]["hits"] == 2 and len(saved["fired"]) == 2
    rb2 = R.RuleBook(p)
    assert rb2.rules[0]["id"] == rb.rules[0]["id"]


def test_any_pair_and_liq_sum():
    rb = R.RuleBook()
    rb.add({"pair": "*", "series": "liq_1h", "op": ">", "value": 2e6})
    fired = rb.evaluate(rows(), now=1.0)
    assert [f["pair"] for f in fired] == ["BTC-USD"]                  # ETH has 1M, under the line


def test_crosses_need_a_previous_value():
    rb = R.RuleBook()
    rb.add({"pair": "BTC-USD", "series": "price", "op": "crosses_above", "value": 81000})
    assert rb.evaluate(rows(price=82000.0), now=1.0) == []           # no previous value yet
    assert rb.evaluate(rows(price=80500.0), now=70.0) == []
    assert len(rb.evaluate(rows(price=81500.0), now=140.0)) == 1
    assert rb.evaluate(rows(price=81600.0), now=2000.0) == []       # still above: no new cross
    rb.add({"pair": "BTC-USD", "series": "price", "op": "crosses_below", "value": 81000})
    rb.evaluate(rows(price=81600.0), now=2100.0)
    f = rb.evaluate(rows(price=80900.0), now=2200.0)
    assert len(f) == 1 and f[0]["series"] == "price"


def test_toggle_and_remove():
    rb = R.RuleBook()
    r = rb.add({"pair": "BTC-USD", "series": "p_up", "op": ">=", "value": 0.6})
    assert len(rb.evaluate(rows(), now=1.0)) == 1
    rb.toggle(r["id"], False)
    assert rb.evaluate(rows(), now=5000.0) == []
    assert rb.remove(r["id"]) and not rb.remove(r["id"])
    assert rb.status()["n"] == 0 and "price" in rb.status()["series"]


def test_engine_action_and_loop(tmp_path, monkeypatch):
    """The engine's rule API and one pass of its loop, on a stand-in engine (the loop reads
    `status`, the alerts, the trace and the screener; a wrong attribute here killed every
    background task on the first smoke run)."""
    import asyncio
    import types

    from steel.engine import Engine

    sent, traced, published = [], [], []
    fake = types.SimpleNamespace(rules=R.RuleBook(tmp_path / "rules.json"), symbols=["BTC-USD", "ETH-USD"], status="live",
                                 alerts=types.SimpleNamespace(send=lambda kind, text, **k: sent.append((kind, text))),
                                 trace=types.SimpleNamespace(emit=lambda *a, **k: traced.append(a)), _publish=lambda m: published.append(m),
                                 screener=lambda: {"rows": rows()})
    fake.rules_status = lambda: Engine.rules_status(fake)
    res = Engine.rules_action(fake, {"action": "add", "pair": "*", "series": "price", "op": ">", "value": 1, "cooldown_s": 60})
    assert res["n"] == 1 and res["pairs"] == ["BTC-USD", "ETH-USD"]
    assert "unknown series" in Engine.rules_action(fake, {"action": "add", "pair": "*", "series": "nope", "op": ">", "value": 1})["error"]
    assert "unknown action" in Engine.rules_action(fake, {"action": "fly"})["error"]
    rid = res["rules"][0]["id"]
    Engine.rules_action(fake, {"action": "test", "id": rid})
    assert sent and sent[-1][0] == "rule" and published[-1]["type"] == "rule"

    calls = {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("stop")

    import steel.engine as eng_mod
    monkeypatch.setattr(eng_mod.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(Engine._rules_loop(fake))
    except RuntimeError:
        pass
    assert len(sent) == 3 and all(k == "rule" for k, _ in sent)      # the test plus one hit per pair
    assert traced and traced[-1][1].startswith("alert: ")
    assert Engine.rules_status(fake)["rules"][0]["hits"] == 2
    assert Engine.rules_action(fake, {"action": "remove", "id": rid})["n"] == 0
