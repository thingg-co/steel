"""Money between venues in the simulation: the rail table, the paper book's two pools (margin
has to be on the perp venue, cash on the spot venue, and moving it costs a fee and takes time),
and the arbitrage executor naming the transfer a missing balance would need."""
import json
from types import SimpleNamespace

from steel.paper import PaperBook
from steel.sources.fees import Transfer, transfer_cost


def test_transfer_table_picks_the_cheapest_rail(monkeypatch, tmp_path):
    t = transfer_cost("USDC", 5000, 1.0)
    assert t.rail == "solana" and t.fee_usd == 0.5 and t.minutes == 5 and round(t.bps(5000), 2) == 1.0
    b = transfer_cost("btc", 0.1, 100_000.0)
    assert b.rail == "bitcoin" and b.fee_usd == 10.0 and b.minutes == 45 and b.seconds == 2700
    e = transfer_cost("ETH", 1, 4000.0)
    assert e.rail in ("arbitrum", "base") and e.fee_usd == 0.4
    assert transfer_cost("SOMETOKEN", 100, 2.0).rail == "ethereum"          # an unknown token is an ERC-20
    f = tmp_path / "fees.json"
    f.write_text(json.dumps({"transfers": {"BTC": [{"network": "lightning", "fee_usd": 0.01, "minutes": 1}]}}))
    monkeypatch.setenv("STEEL_FEES", str(f))
    assert transfer_cost("BTC", 0.1, 100_000.0) == Transfer("BTC", 0.1, "lightning", 0.01, 1)


def two_venue_book(long_mode="perp", **kw):
    return PaperBook(100_000.0, cost_bps=10.0, perp_fee_bps=5.0, long_mode=long_mode, short_max_frac=0.5, short_leverage=2.0,
                     venues=("coinbase", "hyperliquid"), perp_cash_frac=0.1, transfer=transfer_cost, min_transfer_usd=250.0, **kw)


def test_margin_waits_for_the_transfer_and_pays_the_fee():
    b = two_venue_book()
    assert b.split and b.cash == 90_000.0 and b.collateral == 10_000.0 and b.equity({}) == 100_000.0
    px = {"BTC-USD": 100.0}
    # a long perp at 1x (the 15% weight cap: 15k margin) needs its margin on the perp venue: 10k is there, the rest is sent and the open waits
    b.rebalance({"BTC-USD": 0.5}, px, ts=1000.0)
    assert "BTC-USD" not in b.perps and b.n_transfers == 1 and len(b.in_flight) == 1
    f = b.in_flight[0]
    assert f["to"] == "perp" and f["rail"] == "solana" and f["lands_at"] == 1000.0 + 300 and f["fee"] == 0.5
    assert round(b.cash + b.collateral + f["amount"] + b.transfer_fees, 6) == 100_000.0      # nothing leaks but the fee
    assert b.equity(px, 1000.0) == 100_000.0 - 0.5
    # the next bar, still in flight: no second transfer, still no position
    b.rebalance({"BTC-USD": 0.5}, px, ts=1200.0)
    assert "BTC-USD" not in b.perps and b.n_transfers == 1
    # after it lands the perp opens, margin from the collateral pool
    b.rebalance({"BTC-USD": 0.5}, px, ts=1400.0)
    assert "BTC-USD" in b.perps and not b.in_flight
    p = b.perps["BTC-USD"]
    assert p["side"] == 1 and abs(p["margin"] - 15_000.0) < 60                            # 15k less the fee and rounding
    assert b.collateral < 100.0 and b.transfer_fees == 0.5 and b.transfer_wait_s == 300.0
    # closing returns the margin to the perp venue; the spot venue's cash is untouched
    cash_before = b.cash
    b.rebalance({"BTC-USD": 0.0}, {"BTC-USD": 110.0}, ts=2000.0)
    assert "BTC-USD" not in b.perps and b.cash == cash_before and b.collateral > 16_000.0    # the margin and a 10% gain, back on the perp venue
    snap = b.snapshot({"BTC-USD": 110.0})
    assert snap["venues"] == {"spot": "coinbase", "perp": "hyperliquid", "split": True} and snap["n_transfers"] == 1
    assert snap["transfers"][0]["event"] == "landed" and snap["transfers"][1]["event"] == "sent"
    # state round-trips, pools and all
    c = two_venue_book()
    c.load_state(b.to_state())
    assert c.cash == b.cash and c.collateral == b.collateral and c.n_transfers == 1 and c.transfer_fees == 0.5


def test_spot_buy_pulls_cash_back_from_the_perp_venue():
    b = two_venue_book(long_mode="spot")
    b.collateral, b.cash = 99_000.0, 1_000.0                                              # most of the money sits on the perp venue
    px = {"ETH-USD": 10.0}
    b.rebalance({"ETH-USD": 0.1}, px, ts=10.0)                                            # a 10k spot buy: 1k there, 9k+ to come
    assert not b.pos and len(b.in_flight) == 1 and b.in_flight[0]["to"] == "spot" and b.in_flight[0]["amount"] > 9_000.0
    b.rebalance({"ETH-USD": 0.1}, px, ts=10.0 + 301)
    assert b.pos.get("ETH-USD", 0) > 0


def test_one_venue_is_one_pool():
    b = PaperBook(100_000.0, long_mode="perp", venues=("bybit", "bybit"), perp_cash_frac=0.5, transfer=transfer_cost)
    assert not b.split and b.collateral == 0.0 and b.cash == 100_000.0
    b.rebalance({"BTC-USD": 0.3}, {"BTC-USD": 100.0}, ts=1.0)
    assert "BTC-USD" in b.perps and b.n_transfers == 0 and not b.in_flight
    # an old one-pool checkpoint loaded into a two-venue book is split once
    c = two_venue_book()
    c.load_state({"cash": 80_000.0})
    assert c.cash == 40_000.0 and c.collateral == 40_000.0
    # and a two-pool checkpoint loaded into a one-venue book merges, in-flight money included
    d = PaperBook(100_000.0, venues=("bybit", "bybit"), transfer=transfer_cost)
    d.load_state({"cash": 10.0, "collateral": 20.0, "in_flight": [{"to": "perp", "amount": 5.0, "lands_at": 0, "from": "spot", "fee": 0.5}]})
    assert d.cash == 35.0 and d.collateral == 0.0 and not d.in_flight


def test_arb_names_the_transfer_a_missing_balance_needs():
    from steel.arb import ArbExecutor
    cfg = SimpleNamespace(arb=True, short_leverage=2.0, transfer_asset="USDC")
    ex = ArbExecutor(cfg, {}, SimpleNamespace(armed=set()), venue_kind={"coinbase": "spot", "kraken": "spot", "bybit": "perp"}, state_dir="/nonexistent")
    ex.portfolio = {"accounts": [{"id": "kraken", "cash": 50.0, "positions": [{"asset": "BTC", "qty": 0.01, "kind": "spot"}]},
                                 {"id": "coinbase", "cash": 100.0, "positions": []}, {"id": "bybit", "cash": 10.0, "positions": []}]}
    why = ex._funded("BTC-USD", "coinbase", "kraken", 0.1, 10_000.0)
    assert why.startswith("only 0.01 BTC on kraken") and "bitcoin" in why and "10.00 USD (10.0 bps" in why and "45 min" in why
    why = ex._funded("BTC-USD", "coinbase", "bybit", 0.1, 10_000.0)                       # the sell leg is a perp: collateral there
    assert "cash on coinbase" in why and "USDC" in why and "solana" in why and "5 min" in why
    ex.portfolio["accounts"][1]["cash"] = 20_000.0
    assert "collateral on bybit" in ex._funded("BTC-USD", "coinbase", "bybit", 0.1, 10_000.0)
