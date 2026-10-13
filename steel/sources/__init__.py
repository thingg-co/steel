"""Pluggable crypto market-data sources and the manager that routes them.

Each canonical symbol (BASE-USD) is served by the highest-priority source that is enabled
and currently producing data. Sources stream ticks for every symbol they support; the
manager's router forwards only the active provider's ticks for each symbol and drops the
rest, so enabling a second venue gives instant failover without double-counting volume.
Sources can be toggled at runtime from the control panel.

Sources fall into two shapes:
  * push  - a websocket the source keeps open (most venues)
  * poll  - a REST endpoint sampled on an interval (CoinGecko, and venues whose stream needs
            a key). Polled sources emit one tick per fresh observation; a quote-only poll keeps
            a price alive and never builds a candle; a candle poll emits each closed 5-minute
            bar once, so bars lag one interval behind a websocket's.

Candles come from real trades only. Every venue serves 5-minute history; the ones that page it
by date range (`backfill_range`) also serve the deep backfill.

One module per venue (steel/sources/<venue>.py) subclassing `Source` (base.py); the list is
`VENUES` in registry.py and `SourceManager` (manager.py) owns them all.
"""
from __future__ import annotations

from .apex import ApeXSource
from .aster import AsterSource
from .base import (STABLES, UA, Source, SourceStatus, Tick, asset_class, base_of, client, gather_symbols, quote_tick,
                   trade_tick, unscale)
from .binance import BinanceSource
from .bitfinex import BitfinexSource
from .bitget import BitgetSource
from .bitstamp import BitstampSource
from .bitmex import BitMEXSource
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
from .manager import SourceManager, enabled_by_default
from .mexc import MEXCSource
from .okx import OKXSource
from .registry import REGISTRY, VENUES
from .sim import SimSource

__all__ = ["STABLES", "UA", "Source", "SourceStatus", "Tick", "asset_class", "base_of", "client", "gather_symbols",
           "quote_tick", "trade_tick", "unscale", "ApeXSource", "AsterSource", "BinanceSource", "BitfinexSource", "BitgetSource",
           "BitMEXSource", "BitstampSource", "BloFinSource", "BybitSource", "CoinbaseSource", "CoinGeckoSource", "CryptoComSource",
           "DeribitSource", "DydxSource", "GateIOSource", "GeminiSource", "HTXSource", "HyperliquidSource", "KrakenFuturesSource",
           "KrakenSource", "KuCoinSource", "MEXCSource", "OKXSource", "SimSource", "SourceManager", "enabled_by_default", "REGISTRY",
           "VENUES"]
