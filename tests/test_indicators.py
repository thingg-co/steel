"""User indicators (steel/indicators.py): the context's alignment and helpers, the loader's
reload and error handling, running line, dict, marker and failing indicators, save/delete."""
import time
from types import SimpleNamespace

import numpy as np

from steel import indicators as I


def bars(n=40, t0=1000.0, bar_s=300.0):
    out = []
    px = 100.0
    for i in range(n):
        px *= 1.0 + (0.01 if i % 7 else -0.03)
        out.append(SimpleNamespace(ts=t0 + (i + 1) * bar_s, open=px / 1.001, high=px * 1.002, low=px * 0.998, close=px, volume=10.0 + i, buy_volume=6.0, sell_volume=4.0))
    return out


def test_context_aligns_tape_oi_and_liqs():
    b = bars(5)
    cvd = {"t0": 1000.0, "bar_s": 300.0, "delta": [1, 2, 3, 4, 5], "cvd": [1, 3, 6, 10, 15]}
    liqs = {"t0": 1000.0, "bar_s": 300.0, "longs": [0, 10, 0, 0, 7], "shorts": [1, 0, 0, 0, 0]}
    oi = [(1200.0, 100.0), (1700.0, 110.0), (2600.0, 0.0), (2700.0, 130.0)]
    c = I.Context("X-USD", b, cvd=cvd, oi=oi, liqs=liqs, funding=1e-4)
    assert list(c.delta) == [1, 2, 3, 4, 5] and c.cvd[-1] == 15
    assert list(c.liq_long) == [0, 10, 0, 0, 7] and c.liq_short[0] == 1
    assert c.oi[0] == 100.0 and c.oi[1] == 100.0 and c.oi[2] == 110.0 and c.oi[4] == 110.0   # the last reading at or before each close (1300, 1600, 1900, 2500); a zero is skipped
    assert c.buy[0] == 6.0 * c.close[0] and c.funding == 1e-4


def test_helpers():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert np.isnan(I.Context.sma(x, 3)[1]) and I.Context.sma(x, 3)[2] == 2.0
    e = I.Context.ema(x, 3)
    assert e[0] == 1.0 and e[-1] > e[-2]
    r = I.Context.rsi(np.linspace(1, 50, 50), 14)
    assert np.isnan(r[5]) and r[-1] == 100.0
    assert I.Context.cross(np.array([1.0, 3.0]), np.array([2.0, 2.0]), 1) == 1
    assert I.Context.cross(np.array([3.0, 1.0]), np.array([2.0, 2.0]), 1) == -1
    assert I.Context.roll_max(x, 2)[-1] == 6.0 and I.Context.roll_min(x, 2)[-1] == 5.0
    z = I.Context.zscore(x, 4)
    assert np.isnan(z[2]) and abs(z[-1] - 1.3416) < 1e-3


def test_examples_load_and_run(tmp_path):
    hub = I.Hub(tmp_path / "ind")
    hub.ensure_examples()
    assert hub.reload() and set(hub.inds) >= {"oi_rsi", "delta_z", "liq_pressure", "ema_cross", "vwap_dev"} and not hub.errors
    assert not hub.reload()                                   # nothing changed
    b = bars(60)
    ctx = I.Context("X-USD", b, cvd={"t0": 1000.0, "bar_s": 300.0, "delta": list(range(60)), "cvd": list(np.cumsum(range(60)))}, oi=[(1000.0 + 300 * i, 1000.0 + 10 * i) for i in range(61)])
    out = hub.run("oi_rsi", ctx, cache_key=("X-USD", 60, b[-1].ts))
    assert out["n"] == 60 and out["series"]["oi_rsi"][-1] == 100.0 and out["series"]["oi_rsi"][3] is None and out["kind"] == "line"
    assert hub.run("oi_rsi", ctx, cache_key=("X-USD", 60, b[-1].ts)) is out   # cached
    m = hub.run("ema_cross", ctx)
    assert m["kind"] == "marker" and isinstance(m["markers"], list) and all({"t", "v", "text"} <= set(x) for x in m["markers"])
    assert hub.run("vwap_dev", ctx, params={"n": 10})["series"]["vwap_dev"][-1] is not None
    assert "no such" in hub.run("nope", ctx)["error"]
    cat = hub.catalog()
    assert cat["files"] == ["examples.py"] and any(i["doc"].startswith("RSI of aggregate") for i in cat["indicators"])


def test_bad_file_keeps_its_error_and_others_load(tmp_path):
    hub = I.Hub(tmp_path / "ind")
    hub.dir.mkdir()
    (hub.dir / "good.py").write_text("@indicator('two', pane='price')\ndef two(ctx):\n    return ctx.close * 2\n\n@indicator('multi')\ndef multi(ctx):\n    return {'a': ctx.close, 'b': ctx.open}\n")
    (hub.dir / "bad.py").write_text("@indicator('x')\ndef x(ctx):\n    return 1\nraise RuntimeError('boom')\n")
    (hub.dir / "wrongkind.py").write_text("@indicator('y', kind='blob')\ndef y(ctx):\n    return ctx.close\n")
    hub.reload()
    assert set(hub.inds) == {"two", "multi"}
    assert "boom" in hub.errors["bad.py"] and "line 4" in hub.errors["bad.py"] and "kind must be" in hub.errors["wrongkind.py"]
    ctx = I.Context("X-USD", bars(5))
    assert hub.run("two", ctx)["series"]["two"][0] == round(ctx.close[0] * 2, 6)
    assert set(hub.run("multi", ctx)["series"]) == {"a", "b"}
    (hub.dir / "good.py").write_text("@indicator('short')\ndef short(ctx):\n    return ctx.close[:2]\n")
    import os
    os.utime(hub.dir / "good.py", (time.time() + 5, time.time() + 5))
    assert hub.reload() and set(hub.inds) == {"short"}
    assert "returned 2 values" in hub.run("short", ctx)["error"]
    (hub.dir / "bad.py").unlink()
    assert hub.reload() and "bad.py" not in hub.errors


def test_save_and_delete(tmp_path):
    hub = I.Hub(tmp_path / "ind")
    assert "file name" in hub.save("../x.py", "")["error"] and "file name" in hub.save("x.txt", "")["error"]
    res = hub.save("mine.py", "@indicator('m')\ndef m(ctx):\n    return ctx.close\n")
    assert res["saved"] == "mine.py" and res["error"] is None and "m" in hub.inds
    res = hub.save("mine.py", "def m(ctx:\n")
    assert "SyntaxError" in res["error"] and "m" not in hub.inds
    assert hub.delete("mine.py")["deleted"] == "mine.py" and "no such" in hub.delete("mine.py")["error"]
