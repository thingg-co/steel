"""User indicators in Python (M22): a file in `state/indicators/` defines one or more indicators
with a decorator; the engine computes them on demand over a pair's bars (with the tape's
delta and CVD, open interest, liquidations and funding aligned per bar), the chart draws them
(a line or a histogram in their own pane or on the price pane, or markers on the candles) and
the Research tab edits them in place. Files reload when their modification time changes; a
file that fails to load keeps its error in the catalog and its indicators out of it.

    from steel.indicators import indicator          # implicit: `indicator` is in scope

    @indicator("oi_rsi", pane="own", kind="line", params={"n": 14}, color="#bb9af7")
    def oi_rsi(ctx):
        "RSI of open interest: crowding into one side of the perp book."
        return ctx.rsi(ctx.oi, ctx.params["n"])

`ctx` carries numpy arrays over the bars, oldest first: t (bar close), open, high, low, close,
volume (base units), buy, sell (USD, from the bars), delta, cvd (USD, from the tape), oi (USD,
nan where no perp venue reported), liq_long, liq_short (USD per bar), funding (the latest
aggregate rate), pair, params; and the helpers ema, sma, std, rsi, zscore, diff, roll_max,
roll_min, cross. A function returns one array as long as `t` (nan for undefined), a dict of
name -> array (several lines in one pane), or, for `kind="marker"`, a list of (t, price, text).
Nothing here reaches the model: an indicator is a view. Code in these files runs with the
engine's privileges, like any plugin; the dashboard's editor is behind the operator token.
"""
from __future__ import annotations

import math
import time
import traceback
from pathlib import Path

import numpy as np

KINDS = ("line", "hist", "marker")
PANES = ("own", "price")

EXAMPLES = '''"""Example indicators. Edit, or add a file beside this one; the engine reloads on save."""


@indicator("oi_rsi", pane="own", kind="line", params={"n": 14}, color="#bb9af7")
def oi_rsi(ctx):
    "RSI of aggregate open interest: 70+ says the perp book is crowding into positions."
    return ctx.rsi(ctx.oi, ctx.params["n"])


@indicator("delta_z", pane="own", kind="hist", params={"n": 48}, color="#7aa2f7")
def delta_z(ctx):
    "The bar's tape delta as a z-score over the last n bars: the unusual bars stand out."
    return ctx.zscore(ctx.delta, ctx.params["n"])


@indicator("liq_pressure", pane="own", kind="hist", params={"n": 12}, color="#f7768e")
def liq_pressure(ctx):
    "Longs liquidated minus shorts liquidated, smoothed: positive means longs are being flushed."
    return ctx.ema(ctx.liq_long - ctx.liq_short, ctx.params["n"])


@indicator("ema_cross", pane="price", kind="marker", params={"fast": 9, "slow": 21})
def ema_cross(ctx):
    "A marker where the fast EMA crosses the slow one."
    f, s = ctx.ema(ctx.close, ctx.params["fast"]), ctx.ema(ctx.close, ctx.params["slow"])
    out = []
    for i in range(1, len(ctx.t)):
        if ctx.cross(f, s, i) > 0:
            out.append((float(ctx.t[i]), float(ctx.low[i]), "up"))
        elif ctx.cross(f, s, i) < 0:
            out.append((float(ctx.t[i]), float(ctx.high[i]), "down"))
    return out


@indicator("vwap_dev", pane="own", kind="line", params={"n": 96}, color="#e0af68")
def vwap_dev(ctx):
    "Price's distance from the rolling volume-weighted average, in bps."
    pv = ctx.sma(ctx.close * ctx.volume, ctx.params["n"])
    v = ctx.sma(ctx.volume, ctx.params["n"])
    vwap = pv / np.where(v > 0, v, np.nan)
    return (ctx.close / vwap - 1.0) * 1e4
'''


class Context:
    """Everything an indicator may read, aligned per bar, oldest first."""

    def __init__(self, pair: str, bars: list, *, cvd: dict | None = None, oi: list | None = None, liqs: dict | None = None,
                 funding: float | None = None, params: dict | None = None, book: dict | None = None):
        self.pair = pair
        self.params = dict(params or {})
        n = len(bars)
        self.t = np.array([float(b.ts) for b in bars])
        self.open = np.array([float(b.open) for b in bars])
        self.high = np.array([float(b.high) for b in bars])
        self.low = np.array([float(b.low) for b in bars])
        self.close = np.array([float(b.close) for b in bars])
        self.volume = np.array([float(getattr(b, "volume", 0.0) or 0.0) for b in bars])
        self.buy = np.array([float(getattr(b, "buy_volume", 0.0) or 0.0) for b in bars]) * self.close
        self.sell = np.array([float(getattr(b, "sell_volume", 0.0) or 0.0) for b in bars]) * self.close
        self.delta = np.full(n, np.nan)
        self.cvd = np.full(n, np.nan)
        if cvd and cvd.get("delta"):
            bar_s = float(cvd["bar_s"])
            closes = {round(cvd["t0"] + (i + 1) * bar_s, 3): i for i in range(len(cvd["delta"]))}
            for j, t in enumerate(self.t):
                i = closes.get(round(float(t), 3))
                if i is not None:
                    self.delta[j] = cvd["delta"][i]
                    self.cvd[j] = cvd["cvd"][i]
        self.oi = np.full(n, np.nan)
        if oi:
            arr = np.array(sorted((float(a), float(b)) for a, b in oi if b))
            if len(arr):
                for j, t in enumerate(self.t):                     # the last reading at or before the bar's close
                    k = int(np.searchsorted(arr[:, 0], t, side="right")) - 1
                    if k >= 0:
                        self.oi[j] = arr[k, 1]
        self.liq_long = np.zeros(n)
        self.liq_short = np.zeros(n)
        if liqs and liqs.get("longs"):
            bar_s = float(liqs["bar_s"])
            closes = {round(liqs["t0"] + (i + 1) * bar_s, 3): i for i in range(len(liqs["longs"]))}
            for j, t in enumerate(self.t):
                i = closes.get(round(float(t), 3))
                if i is not None:
                    self.liq_long[j] = liqs["longs"][i]
                    self.liq_short[j] = liqs["shorts"][i]
        self.funding = float(funding) if funding is not None else math.nan
        self.book = dict(book or {})

    # helpers ---------------------------------------------------------------------------
    @staticmethod
    def ema(x, n: int):
        x = np.asarray(x, dtype=float)
        out = np.full(len(x), np.nan)
        a = 2.0 / (max(1, int(n)) + 1)
        prev = np.nan
        for i, v in enumerate(x):
            if np.isnan(v):
                out[i] = prev
                continue
            prev = v if np.isnan(prev) else prev + a * (v - prev)
            out[i] = prev
        return out

    @staticmethod
    def sma(x, n: int):
        x = np.asarray(x, dtype=float)
        n = max(1, int(n))
        out = np.full(len(x), np.nan)
        for i in range(n - 1, len(x)):
            w = x[i - n + 1:i + 1]
            if not np.isnan(w).any():
                out[i] = w.mean()
        return out

    @staticmethod
    def std(x, n: int):
        x = np.asarray(x, dtype=float)
        n = max(2, int(n))
        out = np.full(len(x), np.nan)
        for i in range(n - 1, len(x)):
            w = x[i - n + 1:i + 1]
            if not np.isnan(w).any():
                out[i] = w.std()
        return out

    @classmethod
    def zscore(cls, x, n: int):
        x = np.asarray(x, dtype=float)
        m, s = cls.sma(x, n), cls.std(x, n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(s > 0, (x - m) / s, np.nan)

    @staticmethod
    def diff(x, k: int = 1):
        x = np.asarray(x, dtype=float)
        out = np.full(len(x), np.nan)
        if len(x) > k:
            out[k:] = x[k:] - x[:-k]
        return out

    @classmethod
    def rsi(cls, x, n: int = 14):
        x = np.asarray(x, dtype=float)
        d = cls.diff(x)
        up = np.where(d > 0, d, 0.0)
        dn = np.where(d < 0, -d, 0.0)
        up[np.isnan(d)] = np.nan
        dn[np.isnan(d)] = np.nan
        au, ad = cls.ema(up, 2 * n - 1), cls.ema(dn, 2 * n - 1)    # Wilder's smoothing as an EMA
        with np.errstate(invalid="ignore", divide="ignore"):
            rs = au / ad
            out = np.where(ad > 0, 100.0 - 100.0 / (1.0 + rs), np.where(au > 0, 100.0, np.nan))
        out[:n] = np.nan
        return out

    @staticmethod
    def roll_max(x, n: int):
        x = np.asarray(x, dtype=float)
        return np.array([np.nanmax(x[max(0, i - n + 1):i + 1]) if i >= n - 1 else np.nan for i in range(len(x))])

    @staticmethod
    def roll_min(x, n: int):
        x = np.asarray(x, dtype=float)
        return np.array([np.nanmin(x[max(0, i - n + 1):i + 1]) if i >= n - 1 else np.nan for i in range(len(x))])

    @staticmethod
    def cross(a, b, i: int) -> int:
        """+1 when a crossed above b at index i, -1 below, 0 otherwise."""
        if i < 1 or any(np.isnan(v) for v in (a[i], b[i], a[i - 1], b[i - 1])):
            return 0
        if a[i - 1] <= b[i - 1] and a[i] > b[i]:
            return 1
        if a[i - 1] >= b[i - 1] and a[i] < b[i]:
            return -1
        return 0


class Indicator:
    def __init__(self, name: str, fn, *, pane: str, kind: str, color: str | None, params: dict, file: str):
        self.name, self.fn, self.pane, self.kind, self.color, self.params, self.file = name, fn, pane, kind, color, dict(params or {}), file
        self.doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""

    def describe(self) -> dict:
        return {"name": self.name, "pane": self.pane, "kind": self.kind, "color": self.color, "params": self.params, "file": self.file, "doc": self.doc}


class Hub:
    """The indicator files, their registry and their errors."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.inds: dict[str, Indicator] = {}
        self.errors: dict[str, str] = {}
        self.mtimes: dict[str, float] = {}
        self.loaded_at = 0.0
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def ensure_examples(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not any(self.dir.glob("*.py")):
                (self.dir / "examples.py").write_text(EXAMPLES)
        except OSError:
            pass

    def files(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.dir.glob("*.py")):
            try:
                out[p.name] = p.read_text()
            except OSError:
                continue
        return out

    def reload(self, force: bool = False) -> bool:
        """Load every changed file; True when anything changed."""
        changed = False
        seen = set()
        for p in sorted(self.dir.glob("*.py")) if self.dir.exists() else []:
            seen.add(p.name)
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if not force and self.mtimes.get(p.name) == mt:
                continue
            self.mtimes[p.name] = mt
            changed = True
            self._load(p)
        for name in list(self.mtimes):                       # files that went away
            if name not in seen:
                changed = True
                self.mtimes.pop(name, None)
                self.errors.pop(name, None)
                self.inds = {k: v for k, v in self.inds.items() if v.file != name}
        if changed:
            self.loaded_at = time.time()
            self._cache.clear()
        return changed

    def _load(self, p: Path) -> None:
        found: list[Indicator] = []

        def indicator(name: str | None = None, *, pane: str = "own", kind: str = "line", color: str | None = None, params: dict | None = None):
            if pane not in PANES:
                raise ValueError(f"pane must be one of {PANES}")
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {KINDS}")

            def deco(fn):
                found.append(Indicator(str(name or fn.__name__), fn, pane=pane, kind=kind, color=color, params=params or {}, file=p.name))
                return fn
            return deco

        ns = {"indicator": indicator, "np": np, "math": math, "__name__": f"steel_indicators_{p.stem}", "__file__": str(p)}
        try:
            code = compile(p.read_text(), str(p), "exec")
            exec(code, ns)  # noqa: S102  (the operator's own files, like any plugin)
        except Exception as e:  # noqa: BLE001
            tb = traceback.extract_tb(e.__traceback__)
            line = next((f.lineno for f in reversed(tb) if f.filename == str(p)), None)
            self.errors[p.name] = f"{type(e).__name__}: {e}" + (f" (line {line})" if line else "")
            self.inds = {k: v for k, v in self.inds.items() if v.file != p.name}
            return
        self.errors.pop(p.name, None)
        self.inds = {k: v for k, v in self.inds.items() if v.file != p.name}
        for ind in found:
            self.inds[ind.name] = ind

    def catalog(self) -> dict:
        return {"indicators": [i.describe() for i in self.inds.values()], "errors": dict(self.errors), "files": sorted(self.mtimes), "dir": str(self.dir), "loaded_at": self.loaded_at}

    def save(self, file: str, source: str) -> dict:
        name = Path(str(file or "")).name
        if not name.endswith(".py") or name.startswith(".") or "/" in str(file) or len(name) > 64:
            return {"error": "a file name like my_indicators.py"}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_text(str(source))
        except OSError as e:
            return {"error": f"could not write: {e}"}
        self.reload(force=True)
        return {"saved": name, "error": self.errors.get(name)}

    def delete(self, file: str) -> dict:
        name = Path(str(file or "")).name
        p = self.dir / name
        if not p.exists():
            return {"error": "no such file"}
        p.unlink()
        self.reload()
        return {"deleted": name}

    def run(self, name: str, ctx: Context, params: dict | None = None, cache_key: tuple | None = None, ttl: float = 10.0) -> dict:
        ind = self.inds.get(name)
        if not ind:
            return {"name": name, "error": "no such indicator"}
        key = cache_key and (name, *cache_key, tuple(sorted((params or {}).items())))
        if key and key in self._cache and time.time() - self._cache[key][0] < ttl:
            return self._cache[key][1]
        ctx.params = {**ind.params, **(params or {})}
        out = {**ind.describe(), "pair": ctx.pair, "t": [float(x) for x in ctx.t], "n": int(len(ctx.t))}
        try:
            res = ind.fn(ctx)
            if ind.kind == "marker":
                out["markers"] = [{"t": float(t), "v": float(v), "text": str(x)} for t, v, x in (res or [])]
            elif isinstance(res, dict):
                out["series"] = {str(k): _clean(v, len(ctx.t)) for k, v in res.items()}
            else:
                out["series"] = {ind.name: _clean(res, len(ctx.t))}
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        if key:
            self._cache[key] = (time.time(), out)
        return out


def _clean(v, n: int) -> list:
    arr = np.asarray(v, dtype=float).reshape(-1)
    if len(arr) != n:
        raise ValueError(f"returned {len(arr)} values for {n} bars")
    return [None if (math.isnan(x) or math.isinf(x)) else round(float(x), 6) for x in arr]
