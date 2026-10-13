"""The replay budget in bytes (per bar: the feature row plus the label, mask and flags per name,
capped at replay_days), and a smaller buffer keeping the newest bars on load."""
import numpy as np

from steel.config import Config
from tests.test_universe import fill_windows, make


def test_replay_gb_follows_the_universe():
    cfg = Config()
    cfg.symbols = ["A"] * 232
    cfg.window = 128
    cfg.replay_gb = 36.0
    cfg.replay_days = 0.0
    assert cfg.replay_bytes_per_bar(25) == 232 * (25 * 4 + 10) + 40
    assert cfg.replay_bars(25) == int(36e9 // cfg.replay_bytes_per_bar(25))                   # 1.4M bars: the window no longer multiplies the cost
    cfg.symbols = ["A"] * 400
    assert cfg.replay_bars(25) == int(36e9 // (400 * 110 + 40))
    cfg.replay_days = 400.0
    assert cfg.replay_bars(25) == 400 * 288                                                   # the time cap bites first
    cfg.replay_gb = 0.0
    cfg.replay_size = 4096
    assert cfg.replay_bars(25) == 4096 and cfg.replay_windows(25) == 4096


def test_smaller_buffer_keeps_the_newest_windows(tmp_path):
    A = make(["AAA", "BBB"], cap=64)
    fill_windows(A, 30)
    A.save(tmp_path, force_replay=True)
    assert A.size == 37                                              # 30 windows: 7 lead-in bars + 30
    B = make(["AAA", "BBB"], cap=16)
    B.load(tmp_path)
    assert B.size == 16 and B.ptr == 0
    assert np.array_equal(B.rt[:16], A.rt[21:37])                    # the last 16 bars, in order
    assert np.array_equal(B.ry[:16], A.ry[21:37])
    assert B.add(np.zeros((2, 8, 3), dtype=np.float32), np.zeros(2, dtype=np.float32), ts=float(A.rt[36])) is False   # still deduplicated
