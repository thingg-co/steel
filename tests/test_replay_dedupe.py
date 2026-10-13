"""One replay row per bar, one label per bar, across restarts. Exists because every restart
re-walked the same ten days of startup history into the persisted replay: 526 windows held 180
distinct label vectors on 2026-10-10, so recent days were trained on three times over. The
store holds bars: a window's first add also stores its lead-in bars (window - 1 of them,
unlabeled), later windows add one bar each."""
import numpy as np

from steel.config import Config
from steel.learner import OnlineLearner


def make_learner(cap=16):
    cfg = Config()
    cfg.symbols = ["A", "B"]
    cfg.device = "cpu"
    cfg.torch_threads = 1
    cfg.d_model = 16
    cfg.dilations = (1, 2)
    cfg.n_experts = 2
    cfg.n_heads = 2
    cfg.window = 8
    cfg.replay_size = cap
    cfg.quantiles = (0.1, 0.25, 0.5, 0.75, 0.9)
    cfg.batch_size = 4
    cfg.compile = False
    return OnlineLearner(cfg, n_features=3)


def window(learner, seed):
    r = np.random.default_rng(seed)
    return r.random((2, learner.cfg.window, 3), dtype=np.float32), r.random((2,), dtype=np.float32)


def test_same_bar_is_stored_once():
    L = make_learner()
    x, y = window(L, 1)
    assert L.add(x, y, ts=1_700_000_300.0) is True
    assert L.add(x, y, ts=1_700_000_300.0) is False          # the restart re-walk
    assert L.add(x, y, ts=1_700_000_600.0) is True
    assert (L.size, L.labels, L.n_labeled) == (9, 2, 2)      # 8 bars for the first window (7 lead-in), one more for the next


def test_untimed_windows_still_add():
    L = make_learner()
    x, y = window(L, 2)
    assert L.add(x, y) and L.add(x, y)                       # legacy callers: no key, no dedupe
    assert L.size == 16 and L.n_labeled == 2                 # each untimed window is a run of its own


def test_dedupe_survives_save_and_load(tmp_path):
    L = make_learner()
    for i in range(5):
        L.add(*window(L, i), ts=1_700_000_000.0 + 300 * i)
    L.save(tmp_path)
    M = make_learner()
    M.load(tmp_path)
    assert M.size == 12 and M.n_labeled == 5
    assert M.add(*window(M, 1), ts=1_700_000_300.0) is False    # the bar a restart would re-add
    assert M.add(*window(M, 9), ts=1_700_009_000.0) is True


def test_legacy_replay_without_timestamps_loads(tmp_path):
    L = make_learner()
    for i in range(3):
        L.add(*window(L, i), ts=1_700_000_000.0 + 300 * i)
    L.save(tmp_path)
    import shutil
    rd = tmp_path / "replay"
    z = {k: np.load(rd / f"{k}.npy") for k in ("x", "y", "mask")}
    ptr = __import__("json").loads((rd / "meta.json").read_text())["ptr"]
    shutil.rmtree(rd)                                             # a pre-0.5.12 replay: one npz, no timestamps
    np.savez_compressed(tmp_path / "replay.npz", ptr=ptr, **z)
    M = make_learner()
    M.load(tmp_path)
    assert M.size == 10 and not M.store.slot                  # kept, unlinked: nothing to deduplicate or sample


def test_ring_overwrite_frees_the_old_bar():
    L = make_learner(cap=8)
    L.add(*window(L, 0), ts=1_700_000_000.0)                  # fills the ring: 7 lead-in bars and the end bar
    L.add(*window(L, 1), ts=1_700_000_300.0)                  # wraps onto the oldest lead-in bar
    assert L.size == 8 and 1_700_000_000 - 7 * 300 not in L.store.slot and 1_700_000_000 in L.store.slot
    assert L.add(*window(L, 0), ts=1_700_000_000.0) is False  # bar 0 is still labeled
    for i in range(2, 9):
        L.add(*window(L, i), ts=1_700_000_000.0 + 300 * i)    # ... until the ring comes round to its slot
    assert 1_700_000_000 not in L.store.slot
    assert L.add(*window(L, 0), ts=1_700_000_000.0) is True   # bar 0 left the buffer, so it may return


def test_windows_follow_the_time_links_and_a_gap_breaks_them():
    L = make_learner(cap=64)
    for i in range(10):
        L.add(*window(L, i), ts=1_700_000_000.0 + 300 * i)
    st = L.store
    end = st.slot[1_700_000_000 + 300 * 9]
    slots, ok = st.chains(np.array([end]))
    assert ok[0] and [int(st.t[s]) for s in slots[0]] == [1_700_000_000 + 300 * k for k in range(2, 10)]
    x = st.gather(np.array([end]))
    assert x.shape == (1, 2, 8, 3) and np.array_equal(x[0, :, -1, :], st.x[end])
    L.add(*window(L, 20), ts=1_700_000_000.0 + 300 * 30)     # a gap: this window's lead-in bars are stored, its chain is its own
    far = st.slot[1_700_000_000 + 300 * 30]
    assert st.eligible(np.array([far]))[0] and st.prev[st.slot[1_700_000_000 + 300 * 23]] == -1
    assert L.n_labeled == 11 and L.size == 25                # 17 bars, then a window of 8 new ones across the gap
