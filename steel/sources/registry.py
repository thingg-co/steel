"""Every venue Steel can read, in priority order (lower wins a name when both are fresh).

Spot venues first (the asset itself), perpetual-futures venues after (a derivative that funding
keeps near spot), the aggregate index last. The simulator is not here: the manager appends it as
the fallback of last resort. Coinbase and Kraken are on out of the box; everything else is
present but off until toggled in the Sources panel (or named in STEEL_SOURCES_ON with a plus).
"""
from __future__ import annotations

from .apex import ApeXSource
from .aster import AsterSource
from .binance import BinanceSource
from .bitfinex import BitfinexSource
from .bitget import BitgetSource
from .bitstamp import BitstampSource
from .blofin import BloFinSource
from .bybit import BybitSource
from .coinbase import CoinbaseSource
from .coingecko import CoinGeckoSource
from .cryptocom import CryptoComSource
from .deribit import DeribitSource
from .dydx import DydxSource
from .gateio import GateIOSource
from .gemini import GeminiSource
from .htx import HTXSource
from .hyperliquid import HyperliquidSource
from .kraken import KrakenSource
from .krakenfutures import KrakenFuturesSource
from .kucoin import KuCoinSource
from .mexc import MEXCSource
from .okx import OKXSource

VENUES = [
    CoinbaseSource, KrakenSource, BinanceSource, BybitSource, OKXSource,
    BitgetSource, KuCoinSource, GateIOSource, MEXCSource, HTXSource, CryptoComSource, BitfinexSource, GeminiSource, BitstampSource,
    HyperliquidSource, BloFinSource, KrakenFuturesSource, DydxSource, ApeXSource, DeribitSource, AsterSource,
    CoinGeckoSource,
]
# steel/sources/bitmex.py is kept but not listed: BitMEX closed its exchange on 2026-09-22 (every perp
# settled), so the source would never serve a bar. Its balance reader stays registered (accounts/registry.py).

REGISTRY = sorted(VENUES, key=lambda c: c.priority)
assert len({c.id for c in REGISTRY}) == len(REGISTRY), "duplicate source id"
