"""What trading costs on each venue: the fee profile the policy charges against an edge and
the paper book charges on a fill.

Retail base-tier taker and maker fees in basis points per leg, the funding interval of the
venue's perpetuals in hours, and the maintenance margin its liquidation engine uses, from the
public fee schedules (2026). They are a starting table, not a feed: edit it, or put a json
object keyed by venue id in the file STEEL_FEES names ({"coinbase": {"spot_taker_bps": 25}})
to override any field for a venue. The venues a position would trade on are config
(`trade_venue_spot`, `trade_venue_perp`); their profiles are what the policy and the book use.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class FeeProfile:
    venue: str
    spot_taker_bps: float = 0.0      # 0 = the venue has no spot market
    spot_maker_bps: float = 0.0
    perp_taker_bps: float = 0.0      # 0 = no perps
    perp_maker_bps: float = 0.0
    funding_hours: float = 8.0       # how often the perps settle funding
    perp_maintenance: float = 0.005  # maintenance margin fraction at the lowest tier
    note: str = ""


FEES: dict[str, FeeProfile] = {p.venue: p for p in [
    FeeProfile("coinbase", 40.0, 25.0, 0.0, 0.0, note="Advanced Trade, $10k-50k 30-day tier (the first tier is 120/60)"),
    FeeProfile("kraken", 40.0, 25.0, 0.0, 0.0, note="Kraken Pro base tier"),
    FeeProfile("binance", 10.0, 10.0, 5.0, 2.0, 8.0, 0.004, note="VIP 0; USD-M futures"),
    FeeProfile("bybit", 10.0, 10.0, 5.5, 2.0, 8.0, 0.005, note="VIP 0; USDT perps"),
    FeeProfile("okx", 10.0, 8.0, 5.0, 2.0, 8.0, 0.005, note="Lv1"),
    FeeProfile("bitget", 10.0, 10.0, 6.0, 2.0, 8.0, 0.004),
    FeeProfile("kucoin", 10.0, 10.0, 6.0, 2.0, 8.0, 0.005),
    FeeProfile("gateio", 20.0, 20.0, 5.0, 1.5, 8.0, 0.005),
    FeeProfile("mexc", 5.0, 0.0, 2.0, 0.0, 8.0, 0.004, note="zero maker on spot"),
    FeeProfile("htx", 20.0, 20.0, 5.0, 2.0, 8.0, 0.005),
    FeeProfile("cryptocom", 7.5, 7.5, 7.5, 7.5, 8.0, 0.005),
    FeeProfile("bitfinex", 20.0, 10.0, 6.5, 2.0, 8.0, 0.005),
    FeeProfile("gemini", 40.0, 20.0, 0.0, 0.0, note="ActiveTrader base tier"),
    FeeProfile("bitstamp", 40.0, 30.0, 0.0, 0.0),
    FeeProfile("hyperliquid", 4.5, 1.5, 4.5, 1.5, 1.0, 0.0125, note="hourly funding; maintenance half the initial at max leverage"),
    FeeProfile("blofin", 0.0, 0.0, 6.0, 2.0, 8.0, 0.005),
    FeeProfile("krakenfutures", 0.0, 0.0, 5.0, 2.0, 8.0, 0.005, note="funding is continuous on flex perps; 8 h is the quoted rate's unit"),
    FeeProfile("dydx", 0.0, 0.0, 5.0, 1.0, 1.0, 0.03, note="v4 tier 1; hourly funding"),
    FeeProfile("apex", 0.0, 0.0, 5.0, 2.0, 8.0, 0.005),
    FeeProfile("deribit", 0.0, 0.0, 5.0, 0.0, 8.0, 0.005, note="zero maker on BTC and ETH perps"),
    FeeProfile("aster", 0.0, 0.0, 4.0, 1.0, 8.0, 0.005),
    FeeProfile("bitmex", 0.0, 0.0, 7.5, 2.0, 8.0, 0.005, note="closed 2026-09-22"),
    FeeProfile("sim", 10.0, 10.0, 5.0, 2.0, 8.0, 0.005, note="the simulator: a middling venue"),
]}
DEFAULT = FeeProfile("default", 20.0, 10.0, 5.5, 2.0, 8.0, 0.005, note="a venue without a profile: a middling CEX")


def _overrides() -> dict:
    path = os.environ.get("STEEL_FEES", "")
    if not path:
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def fee_profile(venue: str) -> FeeProfile:
    """The venue's profile (the default one for a venue the table lacks), with any STEEL_FEES
    override applied. Unknown override fields are ignored."""
    vid = str(venue or "").lower().strip()
    base = FEES.get(vid, replace(DEFAULT, venue=vid or "default"))
    over = _overrides().get(vid)
    if isinstance(over, dict):
        fields = {k: float(v) for k, v in over.items() if k in FeeProfile.__dataclass_fields__ and k not in ("venue", "note")}
        base = replace(base, **fields)
    return base


def round_trip_bps(profile: FeeProfile, perp: bool) -> float:
    """Two taker legs: what a position costs to open and close at the venue's taker rate."""
    return 2.0 * (profile.perp_taker_bps if perp else profile.spot_taker_bps)


# Moving money between venues ----------------------------------------------------------
#
# A position that needs capital on another venue (margin on the perp venue while the cash sits
# on the spot venue, inventory for an arbitrage leg) needs an on-chain transfer first: a
# withdrawal fee (most venues charge a flat amount per network that covers the network fee) and
# a wait (the venue's withdrawal processing plus the deposit's confirmations). The table holds
# typical 2026 values per asset and rail: a stablecoin moves in minutes for under a dollar on
# Solana or Base, BTC on-chain takes most of an hour. The simulation charges the fee and holds
# the capital for the wait; the cheapest rail by fee is assumed. `transfers` in the STEEL_FEES
# file replaces an asset's rails: {"transfers": {"BTC": [{"network": "bitcoin", "fee_units": 0.0002, "minutes": 60}]}}.

@dataclass(frozen=True)
class Rail:
    network: str
    fee_usd: float = 0.0        # a flat withdrawal fee in dollars (stablecoins, ERC-20s)
    fee_units: float = 0.0      # or in units of the asset (native coins)
    minutes: float = 10.0       # withdrawal processing plus deposit confirmations, typical


@dataclass(frozen=True)
class Transfer:
    asset: str
    qty: float
    rail: str
    fee_usd: float
    minutes: float

    @property
    def seconds(self) -> float:
        return self.minutes * 60.0

    def bps(self, notional_usd: float) -> float:
        return self.fee_usd / notional_usd * 1e4 if notional_usd > 0 else float("inf")


ERC20 = Rail("ethereum", 3.0, 0.0, 15.0)
RAILS: dict[str, list[Rail]] = {
    "USDC": [Rail("solana", 0.5, 0, 5), Rail("base", 0.5, 0, 8), Rail("arbitrum", 1.0, 0, 10), Rail("ethereum", 3.0, 0, 15)],
    "USDT": [Rail("tron", 1.0, 0, 5), Rail("solana", 1.0, 0, 5), Rail("arbitrum", 1.0, 0, 10), Rail("ethereum", 4.0, 0, 15)],
    "USD": [Rail("internal", 0.0, 0, 0)],          # dollars never leave a venue on-chain: a USD balance converts to a stablecoin first (transfer_asset)
    "BTC": [Rail("bitcoin", 0, 0.0001, 45)],       # Lightning is cheaper and instant but only a few venues speak it: add it in STEEL_FEES
    "ETH": [Rail("arbitrum", 0, 0.0001, 10), Rail("base", 0, 0.0001, 10), Rail("ethereum", 0, 0.0008, 10)],
    "SOL": [Rail("solana", 0, 0.01, 5)], "XRP": [Rail("xrpl", 0, 0.2, 5)], "XLM": [Rail("stellar", 0, 0.01, 5)],
    "HBAR": [Rail("hedera", 0, 0.1, 5)], "DOGE": [Rail("dogecoin", 0, 5.0, 30)], "LTC": [Rail("litecoin", 0, 0.001, 30)],
    "BCH": [Rail("bitcoin-cash", 0, 0.001, 30)], "ADA": [Rail("cardano", 0, 1.0, 15)], "DOT": [Rail("polkadot", 0, 0.1, 10)],
    "AVAX": [Rail("avalanche", 0, 0.01, 5)], "NEAR": [Rail("near", 0, 0.01, 5)], "SUI": [Rail("sui", 0, 0.05, 5)],
    "APT": [Rail("aptos", 0, 0.01, 5)], "TAO": [Rail("bittensor", 0, 0.0005, 10)], "ARB": [Rail("arbitrum", 0, 0.3, 10)],
    "OP": [Rail("optimism", 0, 0.1, 10)], "HYPE": [Rail("hyperliquid", 0, 0.05, 10)], "BNB": [Rail("bsc", 0, 0.0005, 5)],
    "TRX": [Rail("tron", 0, 1.0, 5)], "ATOM": [Rail("cosmos", 0, 0.01, 5)], "ALGO": [Rail("algorand", 0, 0.01, 5)],
    "TON": [Rail("ton", 0, 0.05, 5)], "FIL": [Rail("filecoin", 0, 0.001, 30)], "ICP": [Rail("icp", 0, 0.001, 5)],
    "XMR": [Rail("monero", 0, 0.0001, 30)], "ETC": [Rail("ethereum-classic", 0, 0.01, 30)], "KAS": [Rail("kaspa", 0, 1.0, 5)],
    "SEI": [Rail("sei", 0, 0.1, 5)], "INJ": [Rail("injective", 0, 0.01, 5)], "TIA": [Rail("celestia", 0, 0.01, 5)],
    "POL": [Rail("polygon", 0, 0.1, 10)], "WIF": [Rail("solana", 0, 0.1, 5)], "BONK": [Rail("solana", 0, 1000.0, 5)],
    "JUP": [Rail("solana", 0, 0.1, 5)], "PENGU": [Rail("solana", 0, 1.0, 5)], "TRUMP": [Rail("solana", 0, 0.05, 5)],
}


def _rails(asset: str) -> list[Rail]:
    a = str(asset or "").upper()
    over = _overrides().get("transfers")
    if isinstance(over, dict) and isinstance(over.get(a), list):
        try:
            return [Rail(str(r["network"]), float(r.get("fee_usd", 0.0)), float(r.get("fee_units", 0.0)), float(r.get("minutes", 10.0)))
                    for r in over[a]]
        except (KeyError, TypeError, ValueError):
            pass
    return RAILS.get(a, [ERC20])                   # an unknown token is an ERC-20 until the table says otherwise


def transfer_cost(asset: str, qty: float, price_usd: float) -> Transfer:
    """Moving `qty` of `asset` (worth price_usd each) to another venue on its cheapest rail:
    the fee in dollars and the wait in minutes."""
    a = str(asset or "").upper()
    best = None
    for r in _rails(a):
        fee = r.fee_usd + r.fee_units * float(price_usd or 0.0)
        cand = Transfer(a, float(qty), r.network, round(fee, 4), r.minutes)
        if best is None or (cand.fee_usd, cand.minutes) < (best.fee_usd, best.minutes):
            best = cand
    return best
