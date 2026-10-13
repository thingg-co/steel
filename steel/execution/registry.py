"""Every venue Steel can send an order to. Each is an `Executor` (steel/execution/<venue>.py),
arming is per venue (`execution_venues`), and the whole package is off unless `execution` is on."""
from __future__ import annotations

from .binance import BinanceExecutor
from .bitget import BitgetExecutor
from .blofin import BloFinExecutor
from .bybit import BybitExecutor
from .coinbase import CoinbaseExecutor
from .deribit import DeribitExecutor
from .dydx import DydxExecutor
from .gateio import GateIOExecutor
from .aster import AsterExecutor
from .gateio_perp import GateIOPerpExecutor
from .hyperliquid import HyperliquidExecutor
from .kraken import KrakenExecutor
from .krakenfutures import KrakenFuturesExecutor
from .kucoin import KuCoinExecutor
from .okx import OKXExecutor

EXECUTORS: list = [CoinbaseExecutor, KrakenExecutor, BinanceExecutor, BybitExecutor, OKXExecutor, BitgetExecutor, KuCoinExecutor, GateIOExecutor,
                   KrakenFuturesExecutor, BloFinExecutor, DeribitExecutor, GateIOPerpExecutor, HyperliquidExecutor, AsterExecutor, DydxExecutor]

BY_ID = {c.id: c for c in EXECUTORS}
