"""Several horizons from one net: the model's horizon axis and its loss, labels for the longer
horizons from the stored closes, the learner's samples and forecasts, and the policy still
reading the first horizon only."""
import numpy as np
import torch

from steel.config import Config
from steel.learner import OnlineLearner
from steel.model import HORIZON_WEIGHT, SteelNet, steel_loss
from tests.test_replay_dedupe import make_learner


def test_model_forward_and_loss_carry_a_horizon_axis():
    net = SteelNet(3, 2, 5, d_model=16, dilations=(1, 2), n_experts=2, n_heads=2, n_horizons=3)
    x = torch.randn(4, 2, 8, 3)
    q, up, dn, gate, attn = net(x)
    assert q.shape == (4, 2, 3, 5) and up.shape == (4, 2, 3) and dn.shape == (4, 2, 3) and gate.shape == (4, 2) and attn.shape == (4, 2, 2)
    assert (q[..., 1:] >= q[..., :-1]).all()                                        # monotone on every horizon
    y = torch.randn(4, 2, 3) * 30
    mask = torch.ones(4, 2, 3)
    mask[:, :, 2] = 0.0                                                             # the 24 h horizon unlabeled everywhere
    loss, parts = steel_loss(q, up, dn, gate, y, (0.1, 0.25, 0.5, 0.75, 0.9), mask=mask)
    l0, p0 = steel_loss(q[:, :, 0], up[:, :, 0], dn[:, :, 0], gate, y[:, :, 0], (0.1, 0.25, 0.5, 0.75, 0.9), mask=mask[:, :, 0])
    l1, _ = steel_loss(q[:, :, 1], up[:, :, 1], dn[:, :, 1], gate, y[:, :, 1], (0.1, 0.25, 0.5, 0.75, 0.9), balance=0.0, mask=mask[:, :, 1])
    l2, _ = steel_loss(q[:, :, 2], up[:, :, 2], dn[:, :, 2], gate, y[:, :, 2], (0.1, 0.25, 0.5, 0.75, 0.9), balance=0.0, mask=mask[:, :, 2])
    assert torch.isclose(loss, l0 + HORIZON_WEIGHT * (l1 + l2)) and parts == p0      # the parts reported are the policy horizon's
    one = SteelNet(3, 2, 5, d_model=16, dilations=(1, 2), n_experts=2, n_heads=2)
    q1, up1, dn1, _, _ = one(x)
    assert q1.shape == (4, 2, 5) and up1.shape == (4, 2)                              # one horizon: the old shapes


def test_store_labels_longer_horizons_from_closes():
    L = make_learner(cap=256)
    rng = np.random.default_rng(1)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, (40, 2)), axis=0)).astype(np.float32)
    for i in range(40):
        L.add(rng.random((2, 8, 3), dtype=np.float32), np.zeros(2, dtype=np.float32), ts=1_700_000_000.0 + 300 * i,
              closes=closes[i], real=np.array([True, i % 7 != 3]))
    st = L.store
    s0 = st.slot[1_700_000_000]
    assert st.ahead(np.array([s0]), 10)[0] == st.slot[1_700_000_000 + 3000] and st.ahead(np.array([s0]), 45)[0] == -1
    y, mk = st.labels_at(np.array([s0, st.slot[1_700_000_000 + 300 * 35]]), 10)
    assert y.shape == (2, 2) and np.isclose(y[0, 0], np.log(closes[10, 0] / closes[0, 0]) * 1e4)
    assert mk[0, 0] == 1.0 and mk[1, 0] == 0.0                                         # the second has no bar 10 ahead
    y3, mk3 = st.labels_at(np.array([st.slot[1_700_000_000 + 300 * 3]]), 1)              # name B did not trade on bar 3: no label
    assert mk3[0, 1] == 0.0 and mk3[0, 0] == 1.0
    lead = st.slot[1_700_000_000 - 300 * 7]                                              # a lead-in bar has no closes
    assert st.labels_at(np.array([lead]), 5)[1].sum() == 0.0


def test_learner_trains_predicts_and_scores_across_horizons():
    cfg = Config()
    cfg.symbols = ["A", "B"]; cfg.device = "cpu"; cfg.torch_threads = 1; cfg.d_model = 16; cfg.dilations = (1, 2); cfg.n_experts = 2
    cfg.n_heads = 2; cfg.window = 8; cfg.replay_size = 512; cfg.quantiles = (0.1, 0.25, 0.5, 0.75, 0.9); cfg.batch_size = 4; cfg.compile = False
    cfg.horizon = 2; cfg.extra_horizons = "6,20"
    assert cfg.horizons == (2, 6, 20)
    L = OnlineLearner(cfg, n_features=3)
    assert L.horizons == (2, 6, 20) and L.model.n_horizons == 3
    rng = np.random.default_rng(2)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, (240, 2)), axis=0)).astype(np.float32)
    for i in range(240):
        L.add(rng.random((2, 8, 3), dtype=np.float32), rng.normal(0, 20, 2).astype(np.float32), ts=1_700_000_000.0 + 300 * i, closes=closes[i])
    x, y, mk = L._sample(4)
    assert x.shape == (4, 2, 8, 3) and y.shape == (4, 2, 3) and mk.shape == (4, 2, 3)
    assert mk[:, :, 0].sum() > 0
    res = L.train_steps(3)
    assert res and np.isfinite(res["loss"]) and L.steps == 3
    v = L.validate()
    assert v and v["n"] >= 1 and np.isfinite(v["pinball"])
    pred = L.predict(rng.random((2, 8, 3), dtype=np.float32))
    assert pred.q.shape == (2, 5) and pred.p_up.shape == (2,) and set(pred.horizons) == {6, 20}
    assert pred.horizons[20]["q"].shape == (2, 5) and 0.0 <= float(pred.horizons[6]["p_up"][0]) <= 1.0
    card = L.holdout_scorecard()
    assert card is None or card["n"] >= 1
    cfg.extra_horizons = ""
    single = OnlineLearner(cfg, n_features=3)
    assert single.horizons == (2,) and single.model.n_horizons == 1
    p1 = single.predict(np.zeros((2, 8, 3), dtype=np.float32))
    assert p1.horizons == {} and p1.q.shape == (2, 5)
