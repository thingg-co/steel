"""The policy's cost and exit arithmetic (steel/policy.py) and its gates in Engine._suggest:
funding carry and price impact in the cost edge, the Amihud illiquidity gate, the liquidation
price of a margined short, and the stop / target / time-limit exits on spot and shorts."""
import collections
import math

from steel import policy
from steel.bars import Bar
from steel.engine import Engine
from test_gates import make_fake, make_pred


def _bar(close, volume=1000.0, ts=1_800_000_000.0):
    return Bar(ts=ts, open=close, high=close * 1.001, low=close * 0.999, close=close, volume=volume,
               buy_volume=volume * 0.6, sell_volume=volume * 0.4, trades=15, spread_bps=5.0)


def _bars(n=80, volume=1000.0, step=0.0057, start=200.0):
    """Closes alternating up and down by `step` log units: a known daily volatility."""
    out = collections.deque()
    px = start
    for k in range(n):
        px = px * math.exp(step if k % 2 == 0 else -step)
        out.append(_bar(px, volume, ts=1_800_000_000.0 + 300 * k))
    return out


def test_funding_implied_move_and_expected_move():
    assert abs(policy.funding_bps_per_hour(-0.0004, -1, 8.0) - 0.5) < 1e-9     # a short pays a negative rate: 4 bps per 8 h
    assert abs(policy.funding_bps_per_hour(0.0004, -1, 8.0) + 0.5) < 1e-9      # ... and earns a positive one
    assert abs(policy.funding_bps_per_hour(0.0004, 1, 8.0) - 0.5) < 1e-9       # the long pays the positive rate
    assert policy.funding_bps_per_hour(None, -1) == 0.0 and policy.funding_bps_per_hour(0.001, 0) == 0.0
    assert abs(policy.implied_move_bps(60.0, 1.0) - 64.1) < 0.5                # DVOL 60: one sigma over one hour of a 24/7 year
    assert policy.implied_move_bps(0.0, 1.0) == 0.0 and policy.implied_move_bps(None, 1.0) == 0.0
    sym = policy.expected_abs_move_bps([-150, -60, 0, 60, 150])
    assert abs(sym - 117.05 * math.sqrt(2 / math.pi)) < 0.5                    # folded normal, zero median
    assert abs(policy.expected_abs_move_bps([99, 99.5, 100, 100.5, 101]) - 100.0) < 0.1   # a sure move is its size
    assert policy.expected_abs_move_bps(None) == 0.0


def test_liquidation_price():
    assert policy.liquidation_price(200.0, 2.0, -1) == 300.0                   # a 2x short dies 50% up
    assert policy.liquidation_price(200.0, 4.0, -1) == 250.0
    assert policy.liquidation_price(200.0, 2.0, 1) == 100.0                    # a 2x long dies 50% down
    assert abs(policy.liquidation_price(200.0, 2.0, -1, maintenance=0.1) - 290.0) < 1e-9
    assert policy.liquidation_price(0.0, 2.0) == 0.0 and policy.liquidation_price(200.0, 0.0) == 0.0


def test_volume_volatility_impact_and_illiquidity_from_bars():
    bars = _bars()
    adv = policy.dollar_volume(bars)
    assert abs(adv / (1000.0 * 200.0 * 288) - 1.0) < 0.02               # mean bar dollar volume times a 24/7 day
    assert abs(policy.sigma_daily(bars) - 0.0057 * math.sqrt(288)) < 0.003
    assert policy.dollar_volume([]) == 0.0 and policy.sigma_daily(list(bars)[:2]) == 0.0
    assert abs(policy.impact_bps(10_000.0, 5_000_000.0, 0.04, 0.1) - 1.79) < 0.02   # eta sigma sqrt(Q/ADV)
    assert policy.impact_bps(10_000.0, 0.0, 0.04) == 0.0
    ami = policy.amihud(bars)
    assert abs(ami - 57.0 / (1000.0 * 200.0)) < 0.02 * ami              # ~57 bps per bar over ~$200k per bar
    assert abs(policy.kyle_impact_bps(ami, 20_000.0) - 5.7) < 0.3        # a $20k order moves it ~5.7 bps
    assert policy.amihud(list(bars)[:2]) == 0.0


def test_exit_decisions_stop_target_and_time_limit_on_spot_and_shorts():
    cfg = type("C", (), {"stop_loss_bps": 200.0, "take_profit_bps": 300.0, "max_hold_bars": 24})()
    pos = {"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0, "P": 0.0}
    avg = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    prices = {"A": 97.5, "B": 103.5, "C": 100.5, "D": 100.5, "S": 102.5, "T": 96.5, "U": 100.2}
    held = {"A": 90, "B": 90, "C": 70, "D": 95, "S": 95, "T": 95, "U": 60}
    shorts = {"S": {"entry": 100.0}, "T": {"entry": 100.0}, "U": {"entry": 100.0}}
    out = policy.exit_decisions(pos, avg, prices, held, 100, cfg, shorts=shorts)
    assert out["A"].startswith("stop: down 250 bps") and out["B"].startswith("profit target: up 350 bps")
    assert out["C"].startswith("time limit: held 30 bars") and "D" not in out and "P" not in out
    assert out["S"].startswith("stop: down 250 bps")                     # a short loses when the price rises
    assert out["T"].startswith("profit target: up 350 bps")
    assert out["U"].startswith("time limit: held 40 bars")
    off = type("C", (), {"stop_loss_bps": 0.0, "take_profit_bps": 0.0, "max_hold_bars": 0})()
    assert policy.exit_decisions(pos, avg, prices, held, 100, off, shorts=shorts) == {}


def test_impact_is_charged_against_the_edge():
    # a thin edge (12 bps over a 10 bps spread) survives on a liquid name and dies on an illiquid one
    liquid = make_fake(bars={"BTC-USD": _bars(volume=1_000_000.0)}, cfg={"confirm_bars": 1})
    r = Engine._suggest(liquid, 0, "BTC-USD", make_pred([-10, 0, 12, 24, 40], 0.7, 0.2))
    assert r["action"] == "BUY", r["why"]
    thin = make_fake(bars={"BTC-USD": _bars(volume=100.0)}, cfg={"confirm_bars": 1})
    r = Engine._suggest(thin, 0, "BTC-USD", make_pred([-10, 0, 12, 24, 40], 0.7, 0.2))
    assert r["action"] == "HOLD" and "bps of impact" in r["why"], r["why"]


def test_illiquid_names_do_not_trade():
    fake = make_fake(bars={"BTC-USD": _bars(volume=2.0)}, cfg={"confirm_bars": 1})
    r = Engine._suggest(fake, 0, "BTC-USD", make_pred([-20, 10, 40, 70, 100], 0.7, 0.2))
    assert r["action"] == "HOLD" and "too illiquid" in r["why"], r["why"]


def test_negative_funding_is_charged_against_a_bearish_edge_unless_switched_off():
    q = [-40, -24, -12, 0, 10]
    on = make_fake(bars={"BTC-USD": _bars(volume=1_000_000.0)}, funding={"BTC-USD": -0.002}, cfg={"confirm_bars": 1})
    r = Engine._suggest(on, 0, "BTC-USD", make_pred(q, 0.2, 0.7))
    assert r["action"] == "HOLD" and "bps/h of funding" in r["why"], r["why"]
    assert r["funding"] == -0.002
    off = make_fake(bars={"BTC-USD": _bars(volume=1_000_000.0)}, funding={"BTC-USD": -0.002}, cfg={"confirm_bars": 1, "funding_in_edge": False})
    r = Engine._suggest(off, 0, "BTC-USD", make_pred(q, 0.2, 0.7))
    assert r["action"] == "SELL", r["why"]
    paid = make_fake(bars={"BTC-USD": _bars(volume=1_000_000.0)}, funding={"BTC-USD": 0.002}, cfg={"confirm_bars": 1})
    r = Engine._suggest(paid, 0, "BTC-USD", make_pred(q, 0.2, 0.7))
    assert r["action"] == "SELL", r["why"]                              # a positive rate pays the short: no charge (and no credit)
    long_ = make_fake(bars={"BTC-USD": _bars(volume=1_000_000.0)}, funding={"BTC-USD": -0.002}, cfg={"confirm_bars": 1})
    r = Engine._suggest(long_, 0, "BTC-USD", make_pred([-10, 0, 12, 24, 40], 0.7, 0.2))
    assert r["action"] == "BUY", r["why"]                               # spot pays no funding


def test_cluster_caps_cut_sectors_and_beta_together():
    from steel.policy import cluster_caps
    sector = {"UNI-USD": "DeFi", "AAVE-USD": "DeFi", "SOL-USD": "L1", "ETH-USD": "L1", "BTC-USD": "Store of value"}
    beta = {"UNI-USD": 1.5, "AAVE-USD": 1.5, "SOL-USD": 1.2, "ETH-USD": 1.0, "BTC-USD": 1.0}
    t = {"UNI-USD": 0.3, "AAVE-USD": 0.3, "SOL-USD": 0.1, "ETH-USD": -0.05, "BTC-USD": -0.2}
    notes = cluster_caps(t, sector.get, beta.get, max_cluster=0.4, max_beta=0.0, exempt={"BTC-USD"})
    assert abs(t["UNI-USD"] - 0.2) < 1e-9 and abs(t["AAVE-USD"] - 0.2) < 1e-9 and t["SOL-USD"] == 0.1 and t["BTC-USD"] == -0.2
    assert set(notes) == {"UNI-USD", "AAVE-USD"} and "DeFi was 60% gross, cut to 40%" in notes["UNI-USD"]
    # net beta: 0.2*1.5 + 0.2*1.5 + 0.1*1.2 - 0.05*1.0 = 0.67; cap at 0.5 cuts the long-beta names only
    notes = cluster_caps(t, sector.get, beta.get, max_cluster=0.0, max_beta=0.5, exempt={"BTC-USD"})
    net = sum(t[s] * beta[s] for s in t if s != "BTC-USD")
    assert abs(net - 0.5) < 1e-9 and t["ETH-USD"] == -0.05 and "beta cap" in notes["SOL-USD"]
    # under both caps nothing moves
    assert cluster_caps({"UNI-USD": 0.1, "SOL-USD": 0.1}, sector.get, beta.get, max_cluster=0.4, max_beta=1.0) == {}
