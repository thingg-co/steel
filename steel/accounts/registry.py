"""Every venue whose balances Steel can read, in the order the Control panel lists them. Each is
a read-only reader (steel/accounts/<venue>.py); `KEY_SERVICES` in engine.py is built from
`describe()` of each, plus the CoinGecko key."""
from __future__ import annotations

from .apex import ApeXAccount
from .aster import AsterAccount
from .binance import BinanceAccount
from .bitfinex import BitfinexAccount
from .bitget import BitgetAccount
from .bitmex import BitMEXAccount
from .bitstamp import BitstampAccount
from .bybit import BybitAccount
from .blofin import BloFinAccount
from .coinbase import CoinbaseAccount
from .cryptocom import CryptoComAccount
from .deribit import DeribitAccount
from .dydx import DydxAccount
from .gateio import GateIOAccount
from .gemini import GeminiAccount
from .htx import HTXAccount
from .hyperliquid import HyperliquidAccount
from .kraken import KrakenAccount
from .krakenfutures import KrakenFuturesAccount
from .kucoin import KuCoinAccount
from .mexc import MEXCAccount
from .okx import OKXAccount

ACCOUNTS = [
    CoinbaseAccount, KrakenAccount, BinanceAccount, BybitAccount, OKXAccount,
    BitgetAccount, KuCoinAccount, GateIOAccount, MEXCAccount, HTXAccount, CryptoComAccount, BitfinexAccount, GeminiAccount, BitstampAccount,
    HyperliquidAccount, BloFinAccount, KrakenFuturesAccount, DydxAccount, ApeXAccount, DeribitAccount, AsterAccount, BitMEXAccount,
]

BY_ID = {c.id: c for c in ACCOUNTS}
assert len(BY_ID) == len(ACCOUNTS), "duplicate account id"
