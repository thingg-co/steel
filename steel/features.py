"""Turn aligned bar rows into standardized model inputs."""
from __future__ import annotations

import math
from collections import deque

import numpy as np

from .bars import Bar
from .clock import CALENDAR, calendar_features

# Base market microstructure features, then exogenous signals appended (news, social chatter, derivatives
# positioning, the market-wide crypto backdrop), then the calendar (crypto's own rhythm: the hour, the
# day of the week, the funding clock, the CME session, the options expiry; see clock.CALENDAR).
# The exogenous and calendar slots are already bounded to [-1, 1] / [0, 1], so they pass through
# standardization untouched.
EXOGENOUS = ["news_sent", "news_attn", "social_sent", "social_attn", "fng", "funding", "oi_chg", "longshort", "basis", "dvol", "btc_dom", "breadth", "stable_flow"]
MICRO = ["ret", "range", "logvol", "imbalance", "spread", "logtrades", "closepos", "mom", "rvol"]
FEATURES = MICRO + EXOGENOUS + list(CALENDAR)
NORMALIZE = np.array([1, 1, 1, 0, 1, 1, 0, 1, 1] + [0] * (len(EXOGENOUS) + len(CALENDAR)), dtype=bool)
N_FEATURES = len(FEATURES)


class RunningNorm:
    """Per-symbol, per-feature running mean and variance.

    Cumulative for the first samples, then exponentially weighted so the
    standardization tracks slow drifts in volatility and volume.
    """

    def __init__(self, n_assets: int, n_features: int, alpha: float = 0.003):
        self.alpha = alpha
        self.mean = np.zeros((n_assets, n_features), dtype=np.float64)
        self.var = np.ones((n_assets, n_features), dtype=np.float64)
        self.count = 0

    def update(self, x: np.ndarray) -> None:
        if self.count == 0:
            self.mean[:] = x
            self.count = 1
            return
        a = max(self.alpha, 1.0 / (self.count + 1))
        delta = x - self.mean
        self.mean += a * delta
        self.var = (1 - a) * (self.var + a * delta * delta)
        self.count += 1

    def apply(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mean) / np.sqrt(self.var + 1e-8)
        z = np.where(NORMALIZE[None, :], z, x)
        return np.clip(z, -5.0, 5.0).astype(np.float32)

    def state(self, symbols: list[str] | None = None) -> dict:
        """The running stats, with the symbol per row when given, so a universe change can map
        them by name on the way back in."""
        out = {"mean": self.mean.copy(), "var": self.var.copy(), "count": self.count}
        if symbols is not None:
            out["symbols"] = list(symbols)
        return out

    def load(self, st: dict, symbols: list[str] | None = None) -> int:
        """Restore the stats. With the saved and the current symbol lists, rows are mapped by
        name: a name that was there keeps its stats, a new one starts fresh (zero mean, unit
        variance, which `update` overwrites on its first bar). Without names, the shapes must
        match; a mismatch (the universe changed under an old checkpoint) keeps the fresh stats
        rather than failing the start, since the stats re-learn within a few hundred bars. Returns
        the number of rows restored."""
        mean, var = np.asarray(st["mean"], dtype=np.float64), np.asarray(st["var"], dtype=np.float64)
        saved = list(st.get("symbols") or [])
        if saved and symbols is not None and mean.shape[0] == len(saved) and mean.shape[1:] == self.mean.shape[1:]:
            idx = {s: i for i, s in enumerate(saved)}
            n = 0
            for row, sym in enumerate(symbols):
                i = idx.get(sym)
                if i is not None:
                    self.mean[row] = mean[i]
                    self.var[row] = var[i]
                    n += 1
            self.count = int(st["count"]) if n else 0
            return n
        if mean.shape != self.mean.shape or var.shape != self.var.shape:
            return 0
        self.mean[:] = mean
        self.var[:] = var
        self.count = int(st["count"])
        return self.mean.shape[0]


class FeatureBuilder:
    def __init__(self, symbols: list[str], window: int, mom_bars: int = 12):
        self.symbols = symbols
        self.window_len = window
        self.norm = RunningNorm(len(symbols), N_FEATURES)
        self.prev_close: dict[str, float] = {}
        self.rets: dict[str, deque[float]] = {s: deque(maxlen=mom_bars) for s in symbols}
        self.window: deque[np.ndarray] = deque(maxlen=window)
        self.news: dict[str, tuple[float, float]] = {s: (0.0, 0.0) for s in symbols}
        # exogenous signal slots (everything after news), held constant between refreshes
        self.exo_keys = EXOGENOUS[2:]
        self.exo: dict[str, dict[str, float]] = {s: {k: 0.0 for k in self.exo_keys} for s in symbols}

    def set_news(self, symbol: str, sentiment: float, attention: float) -> None:
        """Exogenous news state, held constant until the next skim."""
        self.news[symbol] = (float(np.clip(sentiment, -1, 1)), float(np.clip(attention, 0, 1)))

    def set_exo(self, symbol: str, values: dict) -> None:
        """Exogenous signal vector (social chatter, Fear & Greed, perp positioning, DVOL, the market backdrop), held between refreshes."""
        cur = self.exo[symbol]
        for k in self.exo_keys:
            if k in values and values[k] is not None:
                cur[k] = float(np.clip(values[k], -1, 1))

    def push(self, row: dict[str, Bar]) -> np.ndarray | None:
        """Add one bar row. Returns the (assets, window, features) input once the window is full."""
        raw = np.zeros((len(self.symbols), N_FEATURES), dtype=np.float64)
        cal = calendar_features(float(next(iter(row.values())).ts)) if row else [0.0] * len(CALENDAR)
        for i, s in enumerate(self.symbols):
            b = row[s]
            prev = self.prev_close.get(s, b.open)
            ret = math.log(b.close / prev) * 1e4 if prev > 0 else 0.0
            rng = math.log(b.high / b.low) * 1e4 if b.low > 0 else 0.0
            tot = b.buy_volume + b.sell_volume
            imb = (b.buy_volume - b.sell_volume) / tot if tot > 0 else 0.0
            spread = b.spread_bps if math.isfinite(b.spread_bps) else self.norm.mean[i, 4]
            span = b.high - b.low
            closepos = (b.close - b.low) / span - 0.5 if span > 0 else 0.0
            r = self.rets[s]
            r.append(ret)
            mom = float(sum(r))
            rvol = float(np.std(r)) if len(r) >= 2 else 0.0
            sent, attn = self.news[s]
            ex = self.exo[s]
            raw[i] = [ret, rng, math.log1p(b.volume), imb, spread, math.log1p(b.trades), closepos, mom, rvol,
                      sent, attn] + [ex[k] for k in self.exo_keys] + cal
            self.prev_close[s] = b.close
        self.norm.update(raw)
        self.window.append(self.norm.apply(raw))
        if len(self.window) < self.window_len:
            return None
        return np.stack(self.window, axis=1)

    def peek_window(self) -> np.ndarray | None:
        """A predict-only window from whatever history exists, left-edge padded to
        window_len. Seeds forecasts before a full real window has accumulated; never
        used as a training sample, so training data stays real."""
        if not self.window:
            return None
        frames = list(self.window)
        if len(frames) < self.window_len:
            frames = [frames[0]] * (self.window_len - len(frames)) + frames
        return np.stack(frames, axis=1)
