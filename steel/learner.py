"""Online training: a replay of bars and a small optimizer loop.

The replay is a bar store, not a window ring. Each entry is one bar: the universe's feature row
(n_assets x features), the label of the window that ends on that bar (the forward return in
bps per name, with its mask), the bar's timestamp, and links to the bars one interval before
and after it. A training sample is a bar with a label whose `window` predecessors are all
present and time-contiguous; its window is gathered by index at batch time. Consecutive
windows share all but one bar, so this holds 64x more history than storing windows did: a year
of 400 names in under 4 GB. The deep backfill, the universe migration, the dedupe across
restarts and the held-out slice all work on bars.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from .autotune import autocast, configure_backend, pick_device
from .model import SteelNet, steel_loss
from .target import estimate_beta, index_columns, prior_beta, relative

log = logging.getLogger(__name__)
HOLDOUT_EVERY = 20       # every 20th bar by timestamp is validation-only: 5% the trainer never sees
REPLAY_DIR = "replay"    # one .npy per array plus meta.json, memory-mapped on load; replay.npz is the pre-0.5.12 form
FORMAT = "bars"          # meta.json "format"; a directory without it holds the old window ring


def _key(ts: float) -> int:
    return int(round(float(ts)))


def read_replay(directory: str | Path) -> dict | None:
    """The saved replay as arrays plus its metadata: x, y, mask, t (memory-mapped from the
    replay/ directory; read fully from a legacy replay.npz), has (features present per bar and
    name; ones when the file predates it), ptr, n, the symbol order the columns were saved in
    (empty for the legacy file: the checkpoint's list applies) and `format`: "bars" (one row per
    bar) or "windows" (the old ring, x four-dimensional). `as_bars` turns the latter into the
    former."""
    d = Path(directory)
    rd = d / REPLAY_DIR
    if (rd / "meta.json").exists():
        meta = json.loads((rd / "meta.json").read_text())
        out = {k: np.load(rd / f"{k}.npy", mmap_mode="r") for k in ("x", "y", "mask", "t")}
        has = rd / "has.npy"
        out["has"] = np.load(has, mmap_mode="r") if has.exists() else np.ones_like(out["mask"], dtype=bool)
        for extra in ("lab", "close", "real"):
            f_extra = rd / f"{extra}.npy"
            if f_extra.exists():
                out[extra] = np.load(f_extra, mmap_mode="r")
        out.update({"ptr": int(meta.get("ptr", 0)), "n": int(meta.get("n", len(out["y"]))),
                    "symbols": list(meta.get("symbols") or []), "features": list(meta.get("features") or []),
                    "format": str(meta.get("format") or ("bars" if out["x"].ndim == 3 else "windows"))})
        return out
    f = d / "replay.npz"
    if f.exists():
        z = np.load(f)
        n = len(z["y"])
        mask = z["mask"] if "mask" in z else np.ones_like(z["y"])
        return {"x": z["x"], "y": z["y"], "mask": mask, "has": np.ones_like(mask, dtype=bool),
                "t": z["t"] if "t" in z else np.zeros(n), "ptr": int(z["ptr"]), "n": n, "symbols": [],
                "format": "bars" if z["x"].ndim == 3 else "windows"}
    return None


def as_bars(rep: dict, dt: float) -> dict:
    """A saved window ring as a bar store: every window's bars are placed by timestamp (the
    window ends on its `t`, earlier bars sit `dt` apart before it) and written once, newest
    copy last; the label and mask stay with the window's end bar. Untimed windows cannot be
    placed and are dropped. A bars replay comes back unchanged."""
    if rep.get("format", "bars") == "bars" or np.asarray(rep["x"]).ndim == 3:
        return rep
    x, y, mask, t = rep["x"], rep["y"], rep["mask"], np.asarray(rep["t"], dtype=np.float64)
    n = int(rep["n"])
    w = int(x.shape[2])
    order = [int(i) for i in np.argsort(t[:n], kind="stable") if t[i] > 0]
    slots: dict[int, int] = {}
    bx, by, bm, bt = [], [], [], []
    for i in order:
        end = float(t[i])
        xi = np.asarray(x[i])
        for k in range(w):
            tk = end - (w - 1 - k) * dt
            key = _key(tk)
            if key in slots:
                continue
            slots[key] = len(bt)
            bx.append(xi[:, k, :].copy())
            by.append(np.zeros(x.shape[1], dtype=np.float32))
            bm.append(np.zeros(x.shape[1], dtype=np.float32))
            bt.append(tk)
        s = slots[_key(end)]
        by[s] = np.asarray(y[i], dtype=np.float32).copy()
        bm[s] = np.asarray(mask[i], dtype=np.float32).copy()
    if not bt:
        a = int(x.shape[1]); f = int(x.shape[-1])
        return {"x": np.zeros((0, a, f), np.float32), "y": np.zeros((0, a), np.float32), "mask": np.zeros((0, a), np.float32),
                "has": np.zeros((0, a), bool), "t": np.zeros(0), "ptr": 0, "n": 0, "symbols": list(rep.get("symbols") or []), "format": "bars"}
    out = {"x": np.stack(bx), "y": np.stack(by), "mask": np.stack(bm), "t": np.asarray(bt, dtype=np.float64)}
    out["has"] = np.ones_like(out["mask"], dtype=bool)
    out.update({"ptr": len(bt), "n": len(bt), "symbols": list(rep.get("symbols") or []), "format": "bars"})
    return out


class BarStore:
    """The replay's arrays and their time links. Slots are a ring in insertion order; the
    chain (`prev`/`next`) follows timestamps, so a window is `window` consecutive links back
    from its end bar, valid only when every link exists. `lab` marks end bars that carry a
    label; `has` marks the names whose features a bar really holds (a name promoted into the
    universe later has zeros and `has` False until the deep backfill fills it)."""

    def __init__(self, cap: int, n_assets: int, n_features: int, window: int, dt: float):
        self.cap, self.n_assets, self.n_features, self.window, self.dt = int(cap), int(n_assets), int(n_features), int(window), float(dt)
        self.x = np.zeros((self.cap, n_assets, n_features), dtype=np.float32)
        self.y = np.zeros((self.cap, n_assets), dtype=np.float32)
        self.mask = np.zeros((self.cap, n_assets), dtype=np.float32)
        self.has = np.zeros((self.cap, n_assets), dtype=bool)
        self.close = np.zeros((self.cap, n_assets), dtype=np.float32)   # the bar's closes (0 = unknown): the longer horizons' labels come from these
        self.real = np.zeros((self.cap, n_assets), dtype=bool)          # the name traded on this bar (a label needs both ends real)
        self.t = np.zeros(self.cap, dtype=np.float64)
        self.lab = np.zeros(self.cap, dtype=bool)
        self.hold = np.zeros(self.cap, dtype=bool)
        self.prev = np.full(self.cap, -1, dtype=np.int64)
        self.next = np.full(self.cap, -1, dtype=np.int64)
        self.slot: dict[int, int] = {}              # timestamp key -> slot
        self.size = 0
        self.ptr = 0

    @property
    def n_labeled(self) -> int:
        return int(self.lab[:self.size].sum())

    @classmethod
    def from_replay(cls, rep: dict, window: int, dt: float) -> "BarStore":
        """A read-only store over a saved bars replay (`read_replay` output, converted by
        `as_bars` if it was a ring), for the backtest command."""
        x = np.asarray(rep["x"])
        n = int(rep["n"])
        st = cls(max(1, n), x.shape[1], x.shape[2], window, dt)
        st.x[:n] = x[:n]
        st.y[:n] = np.asarray(rep["y"])[:n]
        st.mask[:n] = np.asarray(rep["mask"])[:n]
        st.has[:n] = np.asarray(rep.get("has", np.ones_like(st.mask[:n], dtype=bool)))[:n]
        st.t[:n] = np.asarray(rep["t"], dtype=np.float64)[:n]
        lab = rep.get("lab")
        st.lab[:n] = np.asarray(lab)[:n] if lab is not None else st.mask[:n].any(axis=1)
        st.size = n
        st.ptr = n % st.cap
        st.relink()
        return st

    def held_out(self, ts: float) -> bool:
        """Every HOLDOUT_EVERY-th bar by timestamp. Derived, not stored, so a reload or a replay
        saved before this existed gets the same slice; untimed bars are never held out."""
        return ts > 0 and (_key(ts) // max(1, int(self.dt))) % HOLDOUT_EVERY == 0

    def _free(self, s: int) -> None:
        """Forget what slot `s` holds before it is overwritten: its timestamp and its links."""
        old = self.t[s]
        if old:
            self.slot.pop(_key(old), None)
        p, n = self.prev[s], self.next[s]
        if p >= 0:
            self.next[p] = -1
        if n >= 0:
            self.prev[n] = -1
        self.prev[s] = self.next[s] = -1
        self.lab[s] = False
        self.hold[s] = False
        self.has[s] = False
        self.mask[s] = 0.0
        self.y[s] = 0.0
        self.close[s] = 0.0
        self.real[s] = False

    def _link(self, s: int, ts: float) -> None:
        p = self.slot.get(_key(ts - self.dt))
        if p is not None and p != s:
            self.prev[s] = p
            self.next[p] = s
        n = self.slot.get(_key(ts + self.dt))
        if n is not None and n != s:
            self.next[s] = n
            self.prev[n] = s

    def put(self, ts: float, feats: np.ndarray, y: np.ndarray | None = None, mask: np.ndarray | None = None,
            closes: np.ndarray | None = None, real: np.ndarray | None = None) -> int:
        """Write a bar: features for every name, and the label with its mask (and the bar's
        closes and traded flags) when this is a window's end bar. A bar already stored at that
        timestamp keeps its slot and takes the label if it had none. Returns the slot."""
        key = _key(ts) if ts else None
        s = self.slot.get(key) if key is not None else None
        fresh = s is None
        if fresh:
            s = self.ptr
            self._free(s)
            self.ptr = (self.ptr + 1) % self.cap
            self.size = min(self.size + 1, self.cap)
            self.t[s] = float(ts or 0.0)
            self.x[s] = feats
            self.has[s] = True
            if key is not None:
                self.slot[key] = s
                self._link(s, float(ts))
            self.hold[s] = self.held_out(float(ts or 0.0))
        if y is not None and (fresh or not self.lab[s]):
            self.y[s] = y
            self.mask[s] = 1.0 if mask is None else mask
            self.lab[s] = True
            if closes is not None:
                self.close[s] = closes
                self.real[s] = True if real is None else np.asarray(real, dtype=bool)
        return s

    def ahead(self, idx: np.ndarray, h: int) -> np.ndarray:
        """The slot `h` bars after each of `idx` along the time links, -1 where the chain breaks."""
        cur = np.asarray(idx, dtype=np.int64)
        for _ in range(int(h)):
            cur = np.where(cur >= 0, self.next[np.maximum(cur, 0)], -1)
        return cur

    def labels_at(self, idx: np.ndarray, h: int, cap_bps: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Labels for horizon `h` from the stored closes: log(close[t+h] / close[t]) in bps,
        with a mask that needs both bars' closes known and real (and the label under the cap).
        (B, assets) each."""
        idx = np.asarray(idx, dtype=np.int64)
        fwd = self.ahead(idx, h)
        ok = fwd >= 0
        f = np.where(ok, fwd, 0)
        c0, c1 = self.close[idx], self.close[f]
        with np.errstate(divide="ignore", invalid="ignore"):
            y = np.log(c1 / c0) * 1e4
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        mk = ok[:, None] & (c0 > 0) & (c1 > 0) & self.real[idx] & self.real[f]
        if cap_bps > 0:
            mk &= np.abs(y) <= cap_bps
        return y, mk.astype(np.float32)

    def chains(self, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(slots (B, window), valid (B,)): the window ending on each slot, following the time
        links back; valid where every link exists."""
        idx = np.asarray(idx, dtype=np.int64)
        out = np.empty((len(idx), self.window), dtype=np.int64)
        out[:, -1] = idx
        cur = idx
        for k in range(self.window - 2, -1, -1):
            cur = np.where(cur >= 0, self.prev[np.maximum(cur, 0)], -1)
            out[:, k] = cur
        return out, (out >= 0).all(axis=1)

    def gather(self, idx: np.ndarray) -> np.ndarray:
        """Windows (B, n_assets, window, features) for end slots `idx`, which must be valid."""
        slots, _ = self.chains(idx)
        return np.ascontiguousarray(self.x[slots].transpose(0, 2, 1, 3))

    def eligible(self, idx: np.ndarray, *, include_held: bool = False) -> np.ndarray:
        """Which of `idx` can be trained on: labeled, a complete window, and (unless asked) not held out."""
        idx = np.asarray(idx, dtype=np.int64)
        ok = self.lab[idx] & self.chains(idx)[1]
        return ok if include_held else ok & ~self.hold[idx]

    def order(self) -> np.ndarray:
        """Slots in insertion order (oldest first), the ring unwound."""
        if self.size < self.cap:
            return np.arange(self.size)
        return (self.ptr + np.arange(self.cap)) % self.cap

    def labeled_rows(self) -> np.ndarray:
        o = self.order()
        return o[self.lab[o]]

    def relink(self) -> None:
        """Rebuild the timestamp index and the links from the arrays (after a load)."""
        self.slot = {}
        self.prev[:] = -1
        self.next[:] = -1
        for s in range(self.size):
            if self.t[s]:
                self.slot[_key(self.t[s])] = s
        for s in range(self.size):
            if self.t[s]:
                self._link(s, float(self.t[s]))
            self.hold[s] = self.held_out(float(self.t[s]))

    def clear(self) -> None:
        self.t[:] = 0.0
        self.lab[:] = False
        self.hold[:] = False
        self.has[:] = False
        self.mask[:] = 0.0
        self.y[:] = 0.0
        self.prev[:] = -1
        self.next[:] = -1
        self.close[:] = 0.0
        self.real[:] = False
        self.slot.clear()
        self.size = self.ptr = 0


class WindowSet:
    """Pre-built windows (x four-dimensional) behind the BarStore's read interface, for the
    backtest's tests and any caller that still hands it a window array."""

    def __init__(self, x: np.ndarray, y: np.ndarray, mask: np.ndarray, t: np.ndarray):
        self.x, self.y, self.mask, self.t = x, y, mask, np.asarray(t, dtype=np.float64)
        self.size = len(y)
        self.n_assets, self.window, self.n_features = x.shape[1], x.shape[2], x.shape[3]
        self.lab = np.ones(self.size, dtype=bool)

    def gather(self, idx: np.ndarray) -> np.ndarray:
        return np.asarray(self.x[np.asarray(idx, dtype=np.int64)])

    def eligible(self, idx: np.ndarray, *, include_held: bool = True) -> np.ndarray:
        return np.ones(len(idx), dtype=bool)

    def labeled_rows(self) -> np.ndarray:
        return np.arange(self.size)


@dataclass(slots=True)
class Prediction:
    q: np.ndarray      # (assets, quantiles) forecast of the forward return, bps
    p_up: np.ndarray   # (assets,) probability the forward return exceeds +threshold
    p_down: np.ndarray # (assets,) probability the forward return is below -threshold
    gate: np.ndarray   # (experts,) regime mixture weights
    attn: np.ndarray   # (assets, assets) cross-asset attention
    horizons: dict = None  # {bars: {"q": (assets, quantiles), "p_up": (assets,), "p_down": (assets,)}} for the extra horizons


class OnlineLearner:
    def __init__(self, cfg, n_features: int, features: list[str] | None = None):
        torch.set_num_threads(cfg.torch_threads)
        self.features = list(features or [])                 # the feature names, saved with the replay so a feature-set change can map columns
        torch.manual_seed(0)
        self.cfg = cfg
        dev = pick_device(cfg.device)
        configure_backend(dev)
        self.device = torch.device(dev)
        self.horizons = tuple(cfg.horizons) if hasattr(cfg, "horizons") else (int(cfg.horizon),)
        self.model = SteelNet(n_features, cfg.n_assets, cfg.n_quantiles, cfg.d_model, cfg.dilations,
                              cfg.n_experts, cfg.n_heads, cfg.dropout, n_horizons=len(self.horizons)).to(self.device)
        self.opt = torch.optim.AdamW(self.model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
        self.compile_broken = False       # set when a compiled step fails; eager for the rest of the run
        self.train_model = self._compiled(self.model)
        self.lock = threading.Lock()
        self.n_features = n_features
        # The target. The replay keeps absolute labels; with the relative target every consumer
        # (training, validation, live scoring) sees the residual against the modeled index names
        # instead, so the switch needs no reset. No columns means absolute, also when no index
        # is modeled.
        self.cols = (index_columns(cfg.symbols, getattr(cfg, "index_symbols", "BTC-USD"))
                     if str(getattr(cfg, "target", "absolute")).lower() == "relative" else [])
        self.beta = prior_beta(cfg.n_assets, self.cols)
        self._beta_at = -10**9                           # labels count at the last beta fit
        self._alloc()
        self.migrated_new: list[str] = []                # names the last load() added fresh (no history in the replay yet)
        self._replay_at = 0.0                            # when the replay was last written, and with how many labels
        self._replay_labels = -1
        self.steps = 0
        self.labels = 0
        self._virtual = -1.0e9                           # timestamps handed to untimed windows (tests, legacy callers): negative, so never held out, deduplicated or backtested
        self.loss_ema: float | None = None
        self.pinball_ema: float | None = None
        self.bce_ema: float | None = None
        self.rng = np.random.default_rng(0)

    # The replay --------------------------------------------------------------------
    def _alloc(self) -> None:
        cfg = self.cfg
        cap = cfg.replay_bars(self.n_features) if hasattr(cfg, "replay_bars") else cfg.replay_size
        self.store = BarStore(cap, cfg.n_assets, self.n_features, cfg.window, cfg.bar_seconds)
        self._released = False

    @property
    def capacity(self) -> int:
        return self.store.cap

    @property
    def size(self) -> int:
        """Bars held."""
        return self.store.size

    @property
    def ptr(self) -> int:
        return self.store.ptr

    @property
    def n_labeled(self) -> int:
        """Bars with a label: what can be trained on (less the held-out slice and incomplete windows)."""
        return self.store.n_labeled

    @property
    def rt(self) -> np.ndarray:
        return self.store.t

    @property
    def ry(self) -> np.ndarray:
        return self.store.y

    @property
    def rmask(self) -> np.ndarray:
        return self.store.mask

    @property
    def rhold(self) -> np.ndarray:
        return self.store.hold

    @property
    def bx(self) -> np.ndarray:
        return self.store.x

    def release_replay(self) -> None:
        """Free the replay arrays ahead of a rebuild that allocates a new buffer of its own; the
        next load() re-allocates and reads the saved replay back."""
        with self.lock:
            self.store = BarStore(0, self.cfg.n_assets, self.n_features, self.cfg.window, self.cfg.bar_seconds)
            self._released = True

    def reset(self, clear_replay: bool = False) -> None:
        cfg = self.cfg
        with self.lock:
            torch.manual_seed(int(time.time()) % 100000)
            self.model = SteelNet(self.n_features, cfg.n_assets, cfg.n_quantiles, cfg.d_model, cfg.dilations,
                                  cfg.n_experts, cfg.n_heads, cfg.dropout, n_horizons=len(self.horizons)).to(self.device)
            self.opt = torch.optim.AdamW(self.model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
            self.train_model = self._compiled(self.model)
            self.steps = 0
            self.loss_ema = self.pinball_ema = self.bce_ema = None
            if clear_replay:
                self.store.clear()
                self.labels = 0

    def _compiled(self, model):
        """torch.compile wrapper for the training step (shares weights with `model`, so
        checkpoints and predict are untouched). Eager on non-CUDA devices, when disabled, or
        once a compile has failed: torch.compile is lazy, so the wrap here always succeeds and
        the real compile runs on the first forward, where train_steps catches it."""
        if not getattr(self.cfg, "compile", False) or self.device.type != "cuda" or self.compile_broken:
            return model
        try:
            t = time.time()
            compiled = torch.compile(model, dynamic=False)
            log.info("torch.compile enabled for the training step (first steps compile; ~%.0fs setup)", time.time() - t)
            return compiled
        except Exception as e:  # noqa: BLE001
            log.warning("torch.compile unavailable (%s: %s); training eager", type(e).__name__, str(e)[:120])
            return model

    def wait_idle(self) -> None:
        """Return once no training step is in flight: the lock is held for the length of a
        train_steps call, so taking and dropping it waits that call out. Called from a thread."""
        with self.lock:
            pass

    def release_cache(self) -> None:
        """Hand the CUDA caching allocator's idle blocks back so another net (the weekend
        backtest) can use them. The weights and optimizer state stay."""
        if self.device.type == "cuda":
            torch.cuda.empty_cache()

    @property
    def n_params(self) -> int:
        return sum(p.numel() for p in self.model.parameters())

    def predict(self, x: np.ndarray) -> Prediction:
        with self.lock, torch.no_grad():
            self.model.eval()
            q, up_logit, down_logit, gate, attn = self.model(torch.from_numpy(x)[None].to(self.device))
        q, up, dn = q[0].cpu().numpy(), torch.sigmoid(up_logit[0]).cpu().numpy(), torch.sigmoid(down_logit[0]).cpu().numpy()
        if q.ndim == 3:                                   # (assets, horizons, quantiles): the first horizon is the policy's
            extra = {int(h): {"q": q[:, k], "p_up": up[:, k], "p_down": dn[:, k]} for k, h in enumerate(self.horizons) if k > 0}
            return Prediction(q[:, 0], up[:, 0], dn[:, 0], gate[0].cpu().numpy(), attn[0].cpu().numpy(), extra)
        return Prediction(q, up, dn, gate[0].cpu().numpy(), attn[0].cpu().numpy(), {})

    def _label_mask(self, y: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        m = np.ones(self.cfg.n_assets, dtype=np.float32) if mask is None else np.asarray(mask, dtype=np.float32)
        cap = self.cfg.label_cap_bps
        if cap > 0:                                      # a label past the cap is a broken feed, not a move: masked, not trained on
            m = m * (np.abs(y) <= cap).astype(np.float32)
        return m

    def add(self, x: np.ndarray, y: np.ndarray, mask: np.ndarray | None = None, ts: float | None = None,
            closes: np.ndarray | None = None, real: np.ndarray | None = None) -> bool:
        """Store one labeled window ending on bar `ts`: its end bar with the label, and any of
        its earlier bars the store does not hold yet (the lead-in of a fresh run of history,
        unlabeled). A bar already labeled is skipped (returns False), so the startup backfill
        after a restart, which walks the same days again, cannot stack copies of recent
        history. Without a timestamp the whole window is stored on virtual (negative)
        timestamps of its own: trainable, never deduplicated, held out or backtested."""
        if self._released:
            self._alloc()
        st = self.store
        x = np.asarray(x, dtype=np.float32)
        y = np.asarray(y, dtype=np.float32)
        if ts:
            s = st.slot.get(_key(ts))
            if s is not None and st.lab[s]:
                return False
            w = x.shape[1]
            for k in range(w - 1):
                tk = float(ts) - (w - 1 - k) * st.dt
                if _key(tk) not in st.slot:
                    st.put(tk, x[:, k, :])
            st.put(float(ts), x[:, -1, :], y, self._label_mask(y, mask), closes, real)
        else:                                            # untimed: a window of its own on virtual timestamps, trainable, never deduplicated
            w = x.shape[1]
            end = self._virtual
            self._virtual -= (w + 1) * st.dt
            for k in range(w - 1):
                st.put(end - (w - 1 - k) * st.dt, x[:, k, :])
            st.put(end, x[:, -1, :], y, self._label_mask(y, mask), closes, real)
        self.labels += 1
        return True

    def fill(self, ts: float | None, x: np.ndarray, y: np.ndarray, mask: np.ndarray | None = None) -> int:
        """Write the columns of a window already in the store (same end bar) that are still
        empty: the names added to the universe after that history was stored. The deep
        backfill re-walks history after a universe change and fills them this way, so a
        promoted name gets its past without the rest of the replay being rebuilt. Returns the
        number of end-bar columns filled."""
        if not ts:
            return 0
        st = self.store
        s = st.slot.get(_key(ts))
        if s is None:
            return 0
        x = np.asarray(x, dtype=np.float32)
        w = x.shape[1]
        for k in range(w - 1):                           # the lead-in bars' missing columns too, features only
            sk = st.slot.get(_key(float(ts) - (w - 1 - k) * st.dt))
            if sk is not None:
                cols = np.flatnonzero(~st.has[sk])
                if len(cols):
                    st.x[sk, cols] = x[cols, k, :]
                    st.has[sk, cols] = True
        m = self._label_mask(np.asarray(y, dtype=np.float32), mask)
        cols = np.flatnonzero(~st.has[s])
        if len(cols) == 0:
            return 0
        st.x[s, cols] = x[cols, -1, :]
        st.has[s, cols] = True
        if st.lab[s]:
            st.y[s, cols] = y[cols]
            st.mask[s, cols] = m[cols]
        return int(len(cols))

    def _held_out(self, ts: float | None) -> bool:
        return self.store.held_out(float(ts or 0.0))

    @property
    def target_mode(self) -> str:
        return "relative" if self.cols else "absolute"

    def target(self, y):
        """Labels as the net is trained to predict them: absolute, or relative to the indices."""
        return relative(y, self.beta, self.cols)

    def refresh_beta(self) -> np.ndarray:
        """Refit each name's index loadings from the labels held, oldest first; a no-op on the
        absolute target. Called once a session from the training step and after a load."""
        if not self.cols:
            return self.beta
        rows = self.store.labeled_rows()
        if len(rows):
            self.beta = estimate_beta(self.store.y[rows], self.store.mask[rows], self.cols,
                                      recent=int(getattr(self.cfg, "beta_windows", 6000)),
                                      shrink=float(getattr(self.cfg, "beta_shrink", 400.0)))
        self._beta_at = self.labels
        return self.beta

    def _draw(self, batch: int) -> np.ndarray:
        """End slots for a batch: a share from the newest bars, the rest uniform over the
        store, redrawn where a slot is unlabeled, held out or has no complete window."""
        st = self.store
        n_recent = min(self.cfg.recent_n, st.size)
        k_recent = int(batch * self.cfg.recent_frac)
        recent = (st.ptr - 1 - self.rng.integers(0, max(1, n_recent), k_recent)) % st.cap
        uniform = self.rng.integers(0, st.size, batch - k_recent)
        idx = np.concatenate([recent, uniform])
        bad = ~st.eligible(idx)
        for _ in range(12):
            if not bad.any():
                break
            idx[bad] = self.rng.integers(0, st.size, int(bad.sum()))
            bad = ~st.eligible(idx)
        return idx[~bad]

    def _labels(self, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The training targets for end slots `idx`: the stored label on the policy's horizon
        and, with extra horizons, the closes-derived ones stacked behind it ((B, A, H) each),
        every horizon on the configured target."""
        st = self.store
        y0, m0 = self.target(st.y[idx]), st.mask[idx]
        if len(self.horizons) <= 1:
            return y0.astype(np.float32), m0.astype(np.float32)
        ys, ms = [y0], [m0]
        for h in self.horizons[1:]:
            yh, mh = st.labels_at(idx, h, float(self.cfg.label_cap_bps or 0.0))
            ys.append(self.target(yh))
            ms.append(mh)
        return np.stack(ys, axis=-1).astype(np.float32), np.stack(ms, axis=-1).astype(np.float32)

    def _sample(self, batch: int):
        idx = self._draw(batch)
        if len(idx) == 0:
            return None
        st = self.store
        y, mk = self._labels(idx)
        return (torch.from_numpy(st.gather(idx)), torch.from_numpy(y), torch.from_numpy(mk))

    def validate(self) -> dict | None:
        """Pinball and direction loss on the held-out bars, which no training step ever
        samples: the honest overfitting signal. In-sample loss rises whenever the deep backfill
        adds unseen history and falls as it is fit, so it cannot tell fitting from overfitting."""
        st = self.store
        cand = np.flatnonzero(st.hold[:st.size] & st.lab[:st.size])
        idx = cand[st.chains(cand)[1]] if len(cand) else cand
        if len(idx) < 8:
            return None
        pin = bce = 0.0
        with self.lock, torch.no_grad():
            self.model.eval()
            for k in range(0, len(idx), 8):                  # small batches: an eval pass must not widen the allocator's footprint
                j = idx[k:k + 8]
                x = torch.from_numpy(st.gather(j)).to(self.device)
                yj, mj = self._labels(j)
                y = torch.from_numpy(yj).to(self.device)
                mk = torch.from_numpy(mj).to(self.device)
                q, up, dn, gate, _ = self.model(x)
                _, parts = steel_loss(q.float(), up.float(), dn.float(), gate.float(), y, self.cfg.quantiles,
                                      label_smoothing=self.cfg.label_smoothing, threshold=self.cfg.direction_threshold_bps, mask=mk)
                pin += float(parts["pinball"]) * len(j)
                bce += float(parts["bce"]) * len(j)
        self.release_cache()                                 # eval shapes differ from training shapes; do not keep both cached
        return {"pinball": pin / len(idx), "bce": bce / len(idx), "n": int(len(idx))}

    def holdout_scorecard(self, recent_bars: int = 0, last_per_name: int = 40) -> dict | None:
        """How the net does on the held-out bars it has never trained on, over the last
        `recent_bars` of them: the direction hit rate over every judged call (a nonzero median
        against a nonzero label), the pinball loss, the raw 10-90 coverage, and per name the
        last `last_per_name` hits in time order. The same yardsticks the live labels use, so
        trust can rest on history before the first live label matures. None under 8 windows."""
        st = self.store
        cand = np.flatnonzero(st.hold[:st.size] & st.lab[:st.size])
        if len(cand) and recent_bars > 0:
            cand = cand[st.t[cand] >= float(st.t[cand].max()) - recent_bars * st.dt]
        idx = cand[st.chains(cand)[1]] if len(cand) else cand
        idx = idx[np.argsort(st.t[idx], kind="stable")]
        if len(idx) < 8:
            return None
        qs = []
        with self.lock, torch.no_grad():
            self.model.eval()
            for k in range(0, len(idx), 8):
                j = idx[k:k + 8]
                q, _up, _dn, _gate, _ = self.model(torch.from_numpy(st.gather(j)).to(self.device))
                qs.append((q[:, :, 0] if q.dim() == 4 else q).float().cpu().numpy())
        self.release_cache()
        q = np.concatenate(qs)                                              # (n, assets, quantiles) on the policy's horizon
        y = self.target(st.y[idx]).astype(np.float64)
        mk = st.mask[idx] > 0
        q50 = q[:, :, q.shape[-1] // 2].astype(np.float64)
        judged = mk & (y != 0.0) & (q50 != 0.0)
        hits = (q50 > 0) == (y > 0)
        taus = np.asarray(self.cfg.quantiles, dtype=np.float64)
        diff = y[:, :, None] - q.astype(np.float64)
        pin = np.maximum(taus * diff, (taus - 1) * diff).mean(axis=-1)
        inside = (q[:, :, 0] <= y) & (y <= q[:, :, -1])
        calls = int(judged.sum())
        per_name = {}
        for i in range(st.n_assets):
            rows = np.flatnonzero(judged[:, i])[-last_per_name:]
            per_name[i] = [bool(h) for h in hits[rows, i]]
        return {"n": int(len(idx)), "calls": calls, "hit": float(hits[judged].mean()) if calls else None,
                "pinball": float(pin[mk].mean()) if mk.any() else None, "coverage": float(inside[mk].mean()) if mk.any() else None,
                "t0": float(st.t[idx[0]]), "t1": float(st.t[idx[-1]]), "per_name": per_name}

    def train_steps(self, n: int) -> dict | None:
        """Run n optimizer steps. Safe to call from a worker thread."""
        if self.n_labeled < 8:
            return None
        batch = min(self.cfg.batch_size, self.n_labeled)
        parts = None
        if self.cols and self.labels - self._beta_at >= 288:             # about one UTC day of labels
            self.refresh_beta()
        with self.lock:
            self.model.train()
            for _ in range(n):
                sample = self._sample(batch)
                if sample is None:                                       # nothing trainable yet (every window incomplete)
                    break
                x, y, mask = sample
                x = x.to(self.device)
                y = y.to(self.device)
                mask = mask.to(self.device)
                if self.cfg.input_noise > 0:
                    x = x + self.cfg.input_noise * torch.randn_like(x)
                with autocast(self.device.type):
                    try:
                        q, up_logit, down_logit, gate, _ = self.train_model(x)
                    except Exception:  # noqa: BLE001
                        if self.train_model is self.model:
                            raise
                        # The first compiled forward is where torch.compile really compiles. A
                        # toolchain problem there (a missing Python.h for Triton's CUDA helper,
                        # a driver mismatch) must cost speed, not the engine: log it once and
                        # train eager from here. An error in the eager retry propagates as before.
                        log.warning("torch.compile failed on its first train step; training eager for the rest "
                                    "of this run (the STEEL_COMPILE speedup is lost). Cause:", exc_info=True)
                        self.compile_broken = True
                        self.train_model = self.model
                        q, up_logit, down_logit, gate, _ = self.train_model(x)
                loss, parts = steel_loss(q.float(), up_logit.float(), down_logit.float(), gate.float(), y, self.cfg.quantiles,
                                         label_smoothing=self.cfg.label_smoothing,
                                         threshold=self.cfg.direction_threshold_bps, mask=mask)
                self.opt.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                self.opt.step()
                self.steps += 1
                a = 0.05
                total = loss.item()
                self.loss_ema = total if self.loss_ema is None else (1 - a) * self.loss_ema + a * total
                self.pinball_ema = parts["pinball"] if self.pinball_ema is None else (1 - a) * self.pinball_ema + a * parts["pinball"]
                self.bce_ema = parts["bce"] if self.bce_ema is None else (1 - a) * self.bce_ema + a * parts["bce"]
        return {"loss": self.loss_ema, "pinball": self.pinball_ema, "bce": self.bce_ema, "steps": self.steps, **(parts or {})}

    # Persistence -----------------------------------------------------------------

    def save(self, directory: str | Path, extra: dict | None = None, *, force_replay: bool = False) -> None:
        """Checkpoint the model every call; the replay only when it has new bars and
        replay_save_minutes have passed, or when forced (shutdown). This runs under the
        training lock, and features compress poorly, so the replay is written uncompressed."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        with self.lock:
            torch.save({
                "model": self.model.state_dict(),
                "opt": self.opt.state_dict(),
                "steps": self.steps,
                "labels": self.labels,
                "emas": (self.loss_ema, self.pinball_ema, self.bce_ema),
                "shape": (self.cfg.n_assets, self.cfg.window, self.n_features),
                "symbols": list(self.cfg.symbols),
                "horizons": list(self.horizons),
                "extra": extra or {},
            }, d / "model.pt.tmp")
            os.replace(d / "model.pt.tmp", d / "model.pt")                       # atomic: never leave a half-written file
            every = float(getattr(self.cfg, "replay_save_minutes", 30.0)) * 60.0
            if (self.labels != self._replay_labels and not self._released
                    and (force_replay or time.time() - self._replay_at >= every)):
                self._write_replay(d)
                self._replay_at, self._replay_labels = time.time(), self.labels

    def _write_replay(self, d: Path) -> None:
        """One .npy per array in replay.tmp/, then swapped into replay/: a crash leaves the old
        directory whole. The legacy replay.npz goes once the directory exists."""
        tmp, old, final = d / (REPLAY_DIR + ".tmp"), d / (REPLAY_DIR + ".old"), d / REPLAY_DIR
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        st = self.store
        n = st.size
        np.save(tmp / "x.npy", st.x[:n])
        np.save(tmp / "y.npy", st.y[:n])
        np.save(tmp / "mask.npy", st.mask[:n])
        np.save(tmp / "has.npy", st.has[:n])
        np.save(tmp / "t.npy", st.t[:n])
        np.save(tmp / "lab.npy", st.lab[:n])
        np.save(tmp / "close.npy", st.close[:n])
        np.save(tmp / "real.npy", st.real[:n])
        (tmp / "meta.json").write_text(json.dumps({"format": FORMAT, "n": n, "ptr": st.ptr, "symbols": list(self.cfg.symbols),
                                                   "features": list(self.features), "window": int(self.cfg.window),
                                                   "bar_seconds": float(self.cfg.bar_seconds), "saved_at": time.time()}))
        shutil.rmtree(old, ignore_errors=True)
        if final.exists():
            os.replace(final, old)
        os.replace(tmp, final)
        shutil.rmtree(old, ignore_errors=True)
        with contextlib.suppress(OSError):
            (d / "replay.npz").unlink()

    def load(self, directory: str | Path) -> dict | None:
        d = Path(directory)
        f = d / "model.pt"
        if not f.exists():
            return None
        try:
            ck = torch.load(f, map_location="cpu", weights_only=False)
        except Exception as e:  # noqa: BLE001 -- a corrupt/partial checkpoint must never crash startup
            log.warning("could not read checkpoint %s (%s); starting fresh", f, e)
            return None
        if self._released:
            self._alloc()
        saved_shape = tuple(ck.get("shape", ()))
        saved_syms = list(ck.get("symbols") or [])
        cur_shape = (self.cfg.n_assets, self.cfg.window, self.n_features)
        cur_syms = list(self.cfg.symbols)
        # Only the symbol list changed (same window and features, some names in common): keep every
        # retained name's embedding row and replay columns, give the new names fresh ones. Before
        # 0.5.12 any universe change threw the model and hours of paged history away.
        migrate = (len(saved_shape) == 3 and saved_shape[1:] == cur_shape[1:] and saved_syms != cur_syms
                   and bool(set(saved_syms) & set(cur_syms)))
        self.migrated_new = []
        # The feature set changed (same window; the names are known from the replay's meta): the net cannot
        # load, but the replay can, column by column, so the fresh net trains on the history at once.
        refeature = False
        if len(saved_shape) == 3 and saved_shape[1] == cur_shape[1] and saved_shape[2] != cur_shape[2] and self.features:
            try:
                meta = json.loads((d / REPLAY_DIR / "meta.json").read_text())
                saved_feats = list(meta.get("features") or [])
                if not saved_feats and saved_shape[2] < cur_shape[2]:
                    saved_feats = list(self.features[:saved_shape[2]])      # before names were recorded, features were only ever appended
                refeature = bool(saved_feats) and set(saved_feats) <= set(self.features) and bool(set(saved_syms) & set(cur_syms))
            except (OSError, ValueError):
                refeature = False
        if refeature:
            log.warning("feature set changed (%d -> %d columns); fresh net, the replay's columns mapped by name", saved_shape[2], cur_shape[2])
            with contextlib.suppress(OSError):
                shutil.copyfile(f, d / "model.pt.bak")
        if (saved_shape != cur_shape or saved_syms != cur_syms) and not migrate and not refeature:
            log.warning("checkpoint at %s does not match the current config "
                        "(saved shape=%s / %d symbols; current shape=%s / %d symbols) — "
                        "backing up to model.pt.bak and starting fresh. If this was not an intended "
                        "config change, restore the .bak to avoid losing training.",
                        d, saved_shape, len(saved_syms), cur_shape, len(cur_syms))
            with contextlib.suppress(OSError):        # preserve the trained model instead of clobbering it on next save
                os.replace(f, d / "model.pt.bak")
                if (d / "replay.npz").exists():
                    os.replace(d / "replay.npz", d / "replay.npz.bak")
                if (d / REPLAY_DIR).exists():
                    shutil.rmtree(d / (REPLAY_DIR + ".bak"), ignore_errors=True)
                    os.replace(d / REPLAY_DIR, d / (REPLAY_DIR + ".bak"))
            return None
        model_fresh = False
        try:
            if refeature:
                raise RuntimeError("feature set changed")
            with self.lock:
                if migrate:
                    self._migrate_model(ck, saved_syms)
                else:
                    self.model.load_state_dict(ck["model"])
                    self.opt.load_state_dict(ck["opt"])
                self.steps = int(ck["steps"])
                self.labels = int(ck["labels"])
                self.loss_ema, self.pinball_ema, self.bce_ema = ck["emas"]
        except Exception as e:  # noqa: BLE001
            # The weights do not fit this net (the autotune moved to another preset, say). The
            # replay is data, not architecture: keep it, migrated if the universe changed too,
            # and start the net fresh on it. Before 0.5.14 this path dropped hours of paged history.
            log.warning("could not restore model state (%s); fresh model on the saved replay", e)
            model_fresh = True
            with self.lock:
                self.model = SteelNet(self.n_features, self.cfg.n_assets, self.cfg.n_quantiles, self.cfg.d_model, self.cfg.dilations,
                                      self.cfg.n_experts, self.cfg.n_heads, self.cfg.dropout, n_horizons=len(self.horizons)).to(self.device)
                self.opt = torch.optim.AdamW(self.model.parameters(), lr=self.cfg.lr, weight_decay=self.cfg.weight_decay)
                self.train_model = self._compiled(self.model)
                self.steps = 0
                self.loss_ema = self.pinball_ema = self.bce_ema = None
            self.migrated_new = [s for s in cur_syms if s not in set(saved_syms)]
        try:
            rep = read_replay(d)
            if rep is not None and rep.get("format") == "windows":
                log.warning("replay at %s is the old window ring (%d windows); converting to bars", d, int(rep["n"]))
                rep = as_bars(rep, float(self.cfg.bar_seconds))
        except Exception as e:  # noqa: BLE001 -- keep the model, just drop an unreadable replay buffer
            log.warning("replay buffer unreadable (%s); keeping model with an empty buffer", e)
            rep = None
        if rep is not None:
            try:
                with self.lock:
                    total = int(rep["n"])
                    n = min(total, self.store.cap)
                    skip = total - n                                      # a smaller buffer keeps the newest bars
                    src_syms = list(rep["symbols"] or saved_syms or cur_syms)
                    self._copy_replay(rep, n, src_syms, skip)
                    if src_syms != cur_syms:                              # columns migrated: the new names need the re-page too
                        self.migrated_new = [s for s in cur_syms if s not in set(src_syms)]
                    labeled = self.store.n_labeled
                    if model_fresh or self.labels < labeled:
                        self.labels = labeled                             # the counter can never read below the labels held
                    self._replay_labels = self.labels                    # what is on disk is what is in memory
            except Exception as e:  # noqa: BLE001 -- keep the model, just drop an unreadable replay buffer
                log.warning("replay buffer unreadable (%s); keeping model with an empty buffer", e)
                self.store.clear()
        extra = ck.get("extra", {}) or {}
        if model_fresh:                                              # the book and the feature normalization outlive the net; its metrics do not
            extra = {k: v for k, v in extra.items() if k in ("norm", "paper")}
        self.refresh_beta()
        saved_target = str(extra.get("target") or "absolute")
        if not model_fresh and saved_target != self.target_mode:
            log.warning("checkpoint was trained on the %s target and this run uses the %s one; the net adapts "
                        "online, but its first forecasts carry the old target", saved_target, self.target_mode)
        return extra

    def _copy_replay(self, rep: dict, n: int, src_syms: list[str], skip: int = 0) -> None:
        """Copy n saved bars into the store in 1024-row chunks (the source is memory-mapped),
        remapping asset columns when the saved symbol order differs from the current one (a
        name no longer modeled is dropped, a new one gets zero features and `has` False until
        the deep backfill fills it in, fill()) and feature columns by name when the saved
        feature list differs (a new feature is zero, a calendar one is recomputed from the
        bar's timestamp). Then the timestamp index and links are rebuilt."""
        st = self.store
        cur = list(self.cfg.symbols)
        lab = rep.get("lab")
        if lab is None:
            lab = np.asarray(rep["mask"]).any(axis=1) if len(rep["mask"]) else np.zeros(0, bool)
        same = list(src_syms) == cur
        pos = {s: i for i, s in enumerate(src_syms)}
        pairs = [(i, pos[s]) for i, s in enumerate(cur) if s in pos]
        ni = np.asarray([p[0] for p in pairs], dtype=np.int64)
        oi = np.asarray([p[1] for p in pairs], dtype=np.int64)
        saved_feats = list(rep.get("features") or [])
        if not saved_feats and self.features and np.asarray(rep["x"]).shape[-1] < len(self.features):
            saved_feats = list(self.features[:np.asarray(rep["x"]).shape[-1]])    # an unnamed replay: a prefix of today's list
        refeat = bool(saved_feats) and bool(self.features) and saved_feats != self.features and np.asarray(rep["x"]).shape[-1] == len(saved_feats)
        fpos = {f: i for i, f in enumerate(saved_feats)}
        fpairs = [(i, fpos[f]) for i, f in enumerate(self.features) if f in fpos] if refeat else []
        nf = np.asarray([p[0] for p in fpairs], dtype=np.int64)
        of = np.asarray([p[1] for p in fpairs], dtype=np.int64)
        st.x[:n] = 0.0
        st.y[:n] = 0.0
        st.mask[:n] = 0.0
        st.has[:n] = False
        for k in range(0, n, 1024):
            j = slice(k, min(n, k + 1024))
            s = slice(skip + k, skip + min(n, k + 1024))
            x = np.asarray(rep["x"][s])
            if refeat:
                xx = np.zeros((x.shape[0], x.shape[1], st.n_features), dtype=np.float32)
                xx[:, :, nf] = x[:, :, of]
                x = xx
            close = np.asarray(rep["close"][s]) if rep.get("close") is not None else None
            real = np.asarray(rep["real"][s], dtype=bool) if rep.get("real") is not None else None
            if same:
                st.x[j] = x
                st.y[j] = rep["y"][s]
                st.mask[j] = rep["mask"][s]
                st.has[j] = np.asarray(rep["has"][s], dtype=bool)
                if close is not None:
                    st.close[j] = close
                if real is not None:
                    st.real[j] = real
            else:
                st.x[j, ni] = x[:, oi]
                st.y[j, ni] = np.asarray(rep["y"][s])[:, oi]
                st.mask[j, ni] = np.asarray(rep["mask"][s])[:, oi]
                st.has[j, ni] = np.asarray(rep["has"][s], dtype=bool)[:, oi]
                if close is not None:
                    st.close[j, ni] = close[:, oi]
                if real is not None:
                    st.real[j, ni] = real[:, oi]
        st.t[:n] = np.asarray(rep["t"][skip:skip + n], dtype=np.float64)
        st.lab[:n] = np.asarray(lab[skip:skip + n], dtype=bool)
        if refeat:
            self._recompute_calendar(n)
        st.size = n
        st.ptr = (int(rep["ptr"]) % st.cap if (n == st.cap and skip == 0) else n % st.cap)
        st.relink()

    def _recompute_calendar(self, n: int) -> None:
        """The calendar features are a function of the bar's timestamp: fill them for every
        timed bar after a feature-set migration, so the new net sees them in old history too."""
        from .clock import CALENDAR, calendar_features  # noqa: PLC0415
        cols = [(i, f) for i, f in enumerate(self.features) if f in CALENDAR]
        if not cols:
            return
        st = self.store
        order = [CALENDAR.index(f) for _, f in cols]
        idx = np.asarray([i for i, _ in cols], dtype=np.int64)
        for s in range(n):
            if st.t[s] > 0:
                vals = calendar_features(float(st.t[s]))
                st.x[s, :, idx] = np.asarray([vals[o] for o in order], dtype=np.float32)[:, None]

    def _migrate_model(self, ck: dict, saved_syms: list[str]) -> None:
        """Load a checkpoint saved for another symbol list. Every parameter is shared across
        assets except the per-asset embedding table (and its Adam moments), whose rows are
        copied for retained names and left at their fresh initialisation for new ones."""
        cur = list(self.cfg.symbols)
        pos = {s: i for i, s in enumerate(saved_syms)}
        pairs = [(i, pos[s]) for i, s in enumerate(cur) if s in pos]
        ni = torch.tensor([p[0] for p in pairs], dtype=torch.long)
        oi = torch.tensor([p[1] for p in pairs], dtype=torch.long)
        sd = dict(ck["model"])
        emb = self.model.asset_emb.detach().clone()
        emb[ni.to(emb.device)] = sd["asset_emb"][oi].to(emb.device, emb.dtype)
        sd["asset_emb"] = emb
        self.model.load_state_dict(sd)
        opt_sd = ck["opt"]
        idx = next(i for i, p in enumerate(self.model.parameters()) if p is self.model.asset_emb)
        st = (opt_sd.get("state") or {}).get(idx)
        if st:
            for key in ("exp_avg", "exp_avg_sq"):
                if key in st and tuple(st[key].shape) == (len(saved_syms), emb.shape[1]):
                    t = torch.zeros(emb.shape, dtype=st[key].dtype)
                    t[ni] = st[key][oi]
                    st[key] = t
        self.opt.load_state_dict(opt_sd)
        self.migrated_new = [s for s in cur if s not in pos]
        log.warning("checkpoint migrated to the new universe: %d names kept, %d dropped, %d new (%s)",
                    len(pairs), len(saved_syms) - len(pairs), len(self.migrated_new), ", ".join(self.migrated_new[:12]))
