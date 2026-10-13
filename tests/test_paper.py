"""Tests for PaperBook in steel/paper.py: spot longs and isolated-margin perp shorts."""
import pytest

from steel.paper import PaperBook


ts = 1_800_000_000.0


def book(**kw):
    args = dict(start=100_000.0, cost_bps=8.0, perp_fee_bps=5.0, short_max_frac=0.05, short_leverage=2.0)
    args.update(kw)
    return PaperBook(**args)


class TestPaperBook:
    def test_short_target_opens_a_margined_short_never_negative_spot(self):
        b = book()
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts)
        assert b.pos.get("BTC-USD", 0.0) == 0.0                     # no negative spot
        sh = b.shorts["BTC-USD"]
        assert sh["qty"] > 0 and sh["entry"] == 200.0 and sh["leverage"] == 2.0
        assert sh["liq"] == pytest.approx(300.0)                     # a 2x short is liquidated 50% up
        assert sh["margin"] == pytest.approx(5_000.0)                # 10% of equity notional at 2x = 5% margin
        assert sh["qty"] * 200.0 == pytest.approx(10_000.0)
        assert b.cash == pytest.approx(100_000.0 - 5_000.0 - 10_000.0 * 5.0 / 1e4)   # margin posted plus the taker fee
        assert b.trades[0]["side"] == "open short" and "liq 300.00" in b.trades[0]["note"]
        for pos in b.pos.values():
            assert pos >= 0

    def test_short_margin_capped_by_short_max_frac(self):
        b = book(short_max_frac=0.02)
        b.rebalance({"BTC-USD": -0.15}, {"BTC-USD": 200.0}, ts)
        sh = b.shorts["BTC-USD"]
        assert sh["margin"] <= 100_000.0 * 0.02 + 1e-6               # the most this view can lose
        assert sh["qty"] * 200.0 == pytest.approx(sh["margin"] * 2.0)

    def test_short_closes_long_spot_first(self):
        b = book()
        prices = {"BTC-USD": 200.0}
        b.rebalance({"BTC-USD": 0.1}, prices, ts)
        assert b.pos.get("BTC-USD", 0.0) > 0
        b.rebalance({"BTC-USD": -0.1}, prices, ts)
        assert "BTC-USD" not in b.pos
        assert "BTC-USD" in b.shorts

    def test_short_marks_gains_losses_and_closes(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts)
        eq0 = b.equity({"BTC-USD": 200.0}, ts)
        assert eq0 == pytest.approx(100_000.0)
        assert b.equity({"BTC-USD": 180.0}, ts) == pytest.approx(101_000.0)   # 10% down on a $10k short: +$1,000
        assert b.equity({"BTC-USD": 220.0}, ts) == pytest.approx(99_000.0)
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 180.0}, ts + 1)
        assert "BTC-USD" not in b.shorts
        assert b.realized == pytest.approx(1_000.0)
        assert b.cash == pytest.approx(101_000.0)
        assert b.trades[0]["side"] == "close short"

    def test_liquidation_loses_exactly_the_margin(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts)
        margin = b.shorts["BTC-USD"]["margin"]
        assert b.equity({"BTC-USD": 1_000_000.0}, ts) == pytest.approx(100_000.0 - margin)   # never worse than the margin
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 301.0}, ts + 1)      # past the liquidation price: the venue closes it
        assert "BTC-USD" not in b.shorts and b.n_liquidations == 1
        assert b.realized == pytest.approx(-margin)
        assert b.cash == pytest.approx(100_000.0 - margin)
        assert b.trades[0]["side"] == "liquidated"

    def test_funding_accrues_with_the_rate_sign(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0, funding_interval_s=8 * 3600.0)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts + 8 * 3600.0, funding={"BTC-USD": 0.001})   # +0.1% per 8 h pays the short
        assert b.shorts["BTC-USD"]["funding"] == pytest.approx(10.0)      # 0.1% of $10k
        assert b.funding_net == pytest.approx(10.0)
        assert b.equity({"BTC-USD": 200.0}, ts) == pytest.approx(100_010.0)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts + 12 * 3600.0, funding={"BTC-USD": -0.002})  # half an interval at -0.2%
        assert b.shorts["BTC-USD"]["funding"] == pytest.approx(10.0 - 10.0)
        assert b.funding_net == pytest.approx(0.0)

    def test_short_min_hold(self):
        b = book(short_min_hold_s=900.0)
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts)
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 200.0}, ts + 300.0)
        assert "BTC-USD" in b.shorts                                  # too young to close
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 200.0}, ts + 900.0)
        assert "BTC-USD" not in b.shorts

    def test_fill_fee_and_spread(self):
        b = book()
        b.rebalance({"BTC-USD": 0.1}, {"BTC-USD": 200.0}, ts, quotes={"BTC-USD": {"bid": 199.9, "ask": 200.1}})
        # The buy must fill at the ask
        assert b.trades[0]["price"] == 200.1
        assert b.trades[0]["side"] == "buy"
        trade_notional = b.trades[0]["notional"]
        assert b.fees == pytest.approx(trade_notional * 8.0 / 1e4)
        assert b.spread_cost == pytest.approx(b.trades[0]["shares"] * 0.1, rel=1e-3)

    def test_short_fills_at_the_bid_and_closes_at_the_ask(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0)
        q = {"BTC-USD": {"bid": 199.0, "ask": 201.0}}
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts, quotes=q)
        assert b.shorts["BTC-USD"]["entry"] == 199.0
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 200.0}, ts + 1, quotes=q)
        assert b.trades[0]["price"] == 201.0 and b.realized == pytest.approx(-(201.0 - 199.0) * b.trades[0]["shares"])

    def test_weights_clipped_and_no_leverage(self):
        b = PaperBook(start=100_000.0, cost_bps=0.0, max_weight=0.15)
        prices = {f"S{i}": 100.0 for i in range(8)}
        b.rebalance({f"S{i}": 0.5 for i in range(8)}, prices, ts)
        for sym in prices:
            assert b.pos.get(sym, 0.0) * prices[sym] <= 0.15 * 100_000 + 1e-6
        assert sum(b.pos.get(sym, 0.0) * prices[sym] for sym in prices) <= 100_000 + 1e-6

    def test_small_rebalance_skipped(self):
        b = book()
        b.rebalance({"BTC-USD": 0.001}, {"BTC-USD": 200.0}, ts)     # 100 notional, below the 200 floor
        assert "BTC-USD" not in b.pos
        assert len(b.trades) == 0

    def test_round_trip_costs_only_fees(self):
        b = book()
        prices = {"BTC-USD": 200.0}
        b.rebalance({"BTC-USD": 0.1}, prices, ts)
        assert b.fees > 0
        b.rebalance({"BTC-USD": 0.0}, prices, ts)
        assert "BTC-USD" not in b.pos
        assert b.equity(prices, ts) == pytest.approx(100_000.0 - b.fees, rel=1e-9)

    def test_state_round_trip(self):
        b = book()
        prices = {"BTC-USD": 200.0, "ETH-USD": 20.0}
        b.rebalance({"BTC-USD": 0.1, "ETH-USD": -0.1}, prices, ts)
        d = b.to_state()
        b2 = PaperBook()
        b2.load_state(d)
        assert b2.cash == pytest.approx(b.cash)
        assert b2.pos == b.pos
        assert b2.shorts["ETH-USD"]["qty"] == pytest.approx(b.shorts["ETH-USD"]["qty"])
        assert b2.equity(prices, ts) == pytest.approx(b.equity(prices, ts))
        b3 = PaperBook()
        b3.load_state(d, symbols={"BTC-USD"})                        # a name no longer modeled is dropped
        assert "ETH-USD" not in b3.shorts and "BTC-USD" in b3.pos

    def test_snapshot_lists_both_kinds(self):
        b = book()
        prices = {"BTC-USD": 200.0, "ETH-USD": 20.0}
        b.rebalance({"BTC-USD": 0.1, "ETH-USD": -0.1}, prices, ts)
        snap = b.snapshot(prices)
        kinds = {p["sym"]: p["kind"] for p in snap["positions"]}
        assert kinds == {"BTC-USD": "spot", "ETH-USD": "short"}
        short = next(p for p in snap["positions"] if p["kind"] == "short")
        assert short["risk"] == pytest.approx(5_000.0) and short["liq"] == pytest.approx(30.0) and short["leverage"] == 2.0
        assert snap["n_shorts"] == 1 and snap["gross"] == pytest.approx(20_000.0, rel=1e-3)
        assert snap["net_exposure"] == pytest.approx(0.0, abs=20.0)


class TestPerpLongs:
    def test_long_mode_perp_opens_a_1x_long_with_no_liquidation(self):
        b = book(long_mode="perp", long_leverage=1.0)
        b.rebalance({"BTC-USD": 0.1}, {"BTC-USD": 200.0}, ts)
        assert "BTC-USD" not in b.pos and "BTC-USD" in b.longs and "BTC-USD" not in b.shorts
        lp = b.perps["BTC-USD"]
        assert lp["side"] == 1 and lp["margin"] == pytest.approx(10_000.0) and lp["liq"] == 0.0 and lp["leverage"] == 1.0
        assert b.cash == pytest.approx(100_000.0 - 10_000.0 - 10_000.0 * 5.0 / 1e4)   # the stake plus the perp taker fee, no spot fee
        assert b.fees == 0.0 and b.perp_fees == pytest.approx(5.0)
        assert b.trades[0]["side"] == "open long" and "no liquidation" in b.trades[0]["note"]
        assert b.equity({"BTC-USD": 220.0}, ts) == pytest.approx(100_000.0 - 5.0 + 1_000.0)
        assert b.equity({"BTC-USD": 1.0}, ts) == pytest.approx(100_000.0 - 5.0 - 10_000.0 + 50.0)   # at worst the stake (the margin floors at 0)
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 220.0}, ts + 1)
        assert "BTC-USD" not in b.perps and b.trades[0]["side"] == "close long" and b.realized == pytest.approx(1_000.0)

    def test_leveraged_long_is_liquidated_at_its_price_losing_the_margin(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0, long_mode="perp", long_leverage=2.0)
        b.rebalance({"BTC-USD": 0.1}, {"BTC-USD": 200.0}, ts)
        lp = b.perps["BTC-USD"]
        assert lp["liq"] == pytest.approx(100.0) and lp["margin"] == pytest.approx(5_000.0) and lp["qty"] * 200.0 == pytest.approx(10_000.0)
        assert b.equity({"BTC-USD": 1.0}, ts) == pytest.approx(100_000.0 - 5_000.0)
        b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 99.0}, ts + 1)      # through the liquidation price: the venue closes it
        assert "BTC-USD" not in b.perps and b.n_liquidations == 1 and b.realized == pytest.approx(-5_000.0)

    def test_long_pays_positive_funding_and_flips_to_a_short(self):
        b = book(cost_bps=0.0, perp_fee_bps=0.0, long_mode="perp", funding_interval_s=3600.0)
        b.rebalance({"BTC-USD": 0.1}, {"BTC-USD": 200.0}, ts)
        b.rebalance({"BTC-USD": 0.1}, {"BTC-USD": 200.0}, ts + 3600.0, funding={"BTC-USD": 0.001})
        assert b.perps["BTC-USD"]["funding"] == pytest.approx(-10.0) and b.funding_net == pytest.approx(-10.0)   # a long pays the positive rate
        b.rebalance({"BTC-USD": -0.1}, {"BTC-USD": 200.0}, ts + 7200.0)
        assert "BTC-USD" in b.shorts and "BTC-USD" not in b.longs and b.trades[1]["side"] == "close long" and b.trades[0]["side"] == "open short"

    def test_state_round_trip_keeps_both_sides_and_reads_an_old_shorts_book(self):
        b = book(long_mode="perp")
        prices = {"BTC-USD": 200.0, "ETH-USD": 20.0}
        b.rebalance({"BTC-USD": 0.1, "ETH-USD": -0.1}, prices, ts)
        d = b.to_state()
        assert "perps" in d and "shorts" not in d
        b2 = PaperBook(long_mode="perp")
        b2.load_state(d)
        assert b2.longs["BTC-USD"]["side"] == 1 and b2.shorts["ETH-USD"]["side"] == -1
        old = {"cash": 90_000.0, "shorts": {"ETH-USD": {"qty": 500.0, "entry": 20.0, "margin": 5_000.0, "leverage": 2.0, "liq": 30.0, "opened": ts, "funding": 0.0, "funded_at": ts}}}
        b3 = PaperBook()
        b3.load_state(old)
        assert b3.shorts["ETH-USD"]["side"] == -1 and b3.equity(prices, ts) == pytest.approx(95_000.0)
        snap = b.snapshot(prices)
        kinds = {p["sym"]: p["kind"] for p in snap["positions"]}
        assert kinds == {"BTC-USD": "long", "ETH-USD": "short"} and snap["n_longs"] == 1 and snap["n_shorts"] == 1 and snap["long_mode"] == "perp"
        assert snap["net_exposure"] == pytest.approx(0.0, abs=50.0) and snap["gross"] == pytest.approx(20_000.0, rel=1e-2)
