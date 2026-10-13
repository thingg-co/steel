"""The policy's cost and exit arithmetic, as pure functions the engine's `_suggest` and the
rebalance step call: what an hour of a position costs beyond the quoted spread (the funding a
perp short pays, the price impact of the order), when a name is too illiquid to touch, where a
margined short is liquidated, and when an open position has to go (a stop, a profit target, a
time limit). Nothing here touches the model.

Sources: market impact follows the square-root law, cost ≈ η σ √(Q / ADV) (Almgren-Chriss,
Gatheral); Amihud's illiquidity, |return| per dollar traded, is Kyle's lambda in practice, the
price move an order of a given size buys; perpetual funding is paid by longs to shorts when the
rate is positive and the other way when negative, every `interval` hours on the notional; an
isolated-margin short at leverage L is liquidated when the price rises by about 1/L, so its
worst case is the margin posted, a known number at entry.
"""
from __future__ import annotations

import math

HOURS_PER_DAY = 24.0
TRADING_DAYS = 365
BARS_PER_DAY = 288                                  # 5-minute bars in a crypto day


def funding_bps_per_hour(rate: float | None, side: int = -1, interval_hours: float = 8.0) -> float:
    """What a position pays in funding per hour, in bps of notional: positive when it costs
    the position, negative when it earns. `rate` is the venue's per-interval rate (0.0001 =
    0.01%); a short (side -1) pays when the rate is negative, a long (side +1) when positive."""
    if rate is None or interval_hours <= 0 or side == 0:
        return 0.0
    paid = float(side) * float(rate)                # the long pays a positive rate, the short a negative one
    return 1e4 * paid / float(interval_hours)


def implied_move_bps(dvol: float | None, hours: float = 1.0) -> float:
    """The one-sigma move an annualized implied volatility index (Deribit DVOL, in percent)
    prices in over `hours` of a 24/7 year, in bps."""
    if not dvol or dvol <= 0 or hours <= 0:
        return 0.0
    return 1e4 * (float(dvol) / 100.0) * math.sqrt(float(hours) / (TRADING_DAYS * HOURS_PER_DAY))


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_abs_move_bps(q) -> float:
    """E|y| from a five-quantile forecast, read as a normal with median q50 and sigma from the
    10-90 spread (the folded-normal mean)."""
    if q is None or len(q) < 5:
        return 0.0
    mu = float(q[2])
    sigma = max(1e-6, (float(q[4]) - float(q[0])) / 2.563)
    z = mu / sigma
    return sigma * math.sqrt(2.0 / math.pi) * math.exp(-0.5 * z * z) + mu * (1.0 - 2.0 * _phi(-z))


def dollar_volume(bars) -> float:
    """A daily dollar-volume estimate from the bars held: the mean bar's dollar volume times
    the bars in a day. Zero when nothing traded."""
    vals = [float(b.volume) * float(b.close) for b in bars if getattr(b, "volume", 0.0) and getattr(b, "close", 0.0)]
    if not vals:
        return 0.0
    return sum(vals) / len(vals) * BARS_PER_DAY


def sigma_daily(bars) -> float:
    """Daily volatility from the bars' close-to-close log returns, scaled by √(bars per day)."""
    closes = [float(b.close) for b in bars if getattr(b, "close", 0.0) and getattr(b, "volume", 0.0)]
    if len(closes) < 3:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0) * BARS_PER_DAY)


def impact_bps(dollars: float, adv_dollars: float, sigma_d: float, eta: float = 0.1) -> float:
    """Square-root price impact of an order of `dollars` against a name trading `adv_dollars`
    a day with daily volatility `sigma_d`, in bps: η σ √(Q / ADV)."""
    if dollars <= 0 or adv_dollars <= 0 or sigma_d <= 0 or eta <= 0:
        return 0.0
    return 1e4 * float(eta) * float(sigma_d) * math.sqrt(float(dollars) / float(adv_dollars))


def amihud(bars) -> float:
    """Amihud's illiquidity over the bars held: mean |bar return| per dollar traded, in bps per
    dollar. Zero when there is not enough to say."""
    closes = [(float(b.close), float(b.volume) * float(b.close)) for b in bars
              if getattr(b, "close", 0.0) and getattr(b, "volume", 0.0)]
    if len(closes) < 3:
        return 0.0
    vals = [abs(math.log(c1 / c0)) * 1e4 / dv1 for (c0, _), (c1, dv1) in zip(closes, closes[1:]) if c0 > 0 and dv1 > 0]
    return sum(vals) / len(vals) if vals else 0.0


def kyle_impact_bps(amihud_bps_per_dollar: float, dollars: float) -> float:
    """The price move, in bps, that an order of `dollars` buys at that illiquidity."""
    if amihud_bps_per_dollar <= 0 or dollars <= 0:
        return 0.0
    return float(amihud_bps_per_dollar) * float(dollars)


def liquidation_price(entry: float, leverage: float, side: int = -1, maintenance: float = 0.0) -> float:
    """Where an isolated-margin position is closed by the venue: a short at leverage L is gone
    when the price has risen by (1 - maintenance) / L, a long when it has fallen by that much.
    The loss at that point is the margin posted, so it is the position's worst case."""
    if entry <= 0 or leverage <= 0:
        return 0.0
    move = max(0.0, 1.0 - float(maintenance)) / float(leverage)
    return float(entry) * (1.0 + move) if side < 0 else float(entry) * max(0.0, 1.0 - move)


def exit_decisions(pos: dict, avg: dict, prices: dict, held_since: dict, bar_index: int, cfg,
                   shorts: dict | None = None, longs: dict | None = None) -> dict[str, str]:
    """Which open positions have to close now, and why: a stop (`stop_loss_bps`), a profit
    target (`take_profit_bps`) or a time limit (`max_hold_bars`); each 0 means off. Spot and
    perp longs are judged against their entry, perp shorts against theirs with the sign flipped."""
    stop = float(getattr(cfg, "stop_loss_bps", 0.0) or 0.0)
    take = float(getattr(cfg, "take_profit_bps", 0.0) or 0.0)
    limit = int(getattr(cfg, "max_hold_bars", 0) or 0)
    out: dict[str, str] = {}
    book = [(s, 1, avg.get(s)) for s, sh in pos.items() if sh > 0]
    book += [(s, 1, (sh or {}).get("entry")) for s, sh in (longs or {}).items() if s not in pos]
    book += [(s, -1, (sh or {}).get("entry")) for s, sh in (shorts or {}).items() if s not in pos]
    for s, side, a in book:
        px = prices.get(s)
        if not px or not a or px <= 0 or a <= 0:
            continue
        upnl = side * (float(px) / float(a) - 1.0) * 1e4
        held = bar_index - int(held_since.get(s, bar_index))
        if stop > 0 and upnl <= -stop:
            out[s] = f"stop: down {-upnl:.0f} bps from entry (limit {stop:.0f})"
        elif take > 0 and upnl >= take:
            out[s] = f"profit target: up {upnl:.0f} bps from entry (target {take:.0f})"
        elif limit > 0 and held >= limit:
            out[s] = f"time limit: held {held} bars (limit {limit})"
    return out


def cluster_caps(targets: dict, sector_of, beta_of, *, max_cluster: float = 0.0, max_beta: float = 0.0, exempt=None) -> dict:
    """Scale `targets` (symbol -> signed weight) in place so no sector carries more than
    `max_cluster` gross weight and the book's net BTC beta (the sum of weight x loading) stays
    within `max_beta`; a crowded cluster's names are cut together, pro rata. Exempt names (the
    index hedges) are left alone and left out of the sums. Returns a note per name touched."""
    exempt = exempt or set()
    notes: dict[str, str] = {}
    if max_cluster > 0:
        by: dict[str, list] = {}
        for s, w in targets.items():
            if w and s not in exempt:
                by.setdefault(sector_of(s), []).append(s)
        for sector, names in by.items():
            gross = sum(abs(targets[s]) for s in names)
            if gross > max_cluster + 1e-9:
                f = max_cluster / gross
                for s in names:
                    targets[s] *= f
                    notes[s] = f"cluster cap: {sector} was {gross:.0%} gross, cut to {max_cluster:.0%} ({f:.2f}x)"
    if max_beta > 0:
        contrib = {s: w * beta_of(s) for s, w in targets.items() if w and s not in exempt}
        net = sum(contrib.values())
        if abs(net) > max_beta + 1e-9:
            sign = 1.0 if net > 0 else -1.0
            same = [s for s, c in contrib.items() if c * sign > 0]
            S = sum(contrib[s] for s in same)
            O = net - S
            f = max(0.0, (sign * max_beta - O) / S) if S else 0.0
            for s in same:
                targets[s] *= f
                notes[s] = (notes.get(s, "").rstrip() + "; " if s in notes else "") + f"beta cap: net BTC beta was {net:+.2f}, cut to {sign * max_beta:+.2f} ({f:.2f}x)"
    return notes
