"""Binance: `GET /api/v3/account` (spot balances) and, on binance.com only, the USD-M futures
wallet `GET /fapi/v2/balance` and `GET /fapi/v2/positionRisk`; the account's fee rates from
`GET /sapi/v1/asset/tradeFee` (spot) and `GET /fapi/v1/commissionRate` (USD-M), nothing else."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse

from ..sources import unscale
from .base import Account, fee_bps, fee_rates, fnum

ACCOUNT = "/api/v3/account"
BALANCE = "/fapi/v2/balance"
POSITIONS = "/fapi/v2/positionRisk"
TRADE_FEE = "/sapi/v1/asset/tradeFee"                  # spot: [{symbol, makerCommission, takerCommission}]
COMMISSION = "/fapi/v1/commissionRate"                 # USD-M: {symbol, makerCommissionRate, takerCommissionRate}
FEE_SYMBOL = "BTCUSDT"
FUTURES = "https://fapi.binance.com"
QUOTES = ("USDT", "USDC", "FDUSD", "BUSD")


class BinanceAccount(Account):
    """Binance API key + secret: HMAC-SHA256 hex over the query string (`timestamp` and
    `recvWindow` included, `signature` last), the key in X-MBX-APIKEY. STEEL_BINANCE_HOST=
    api.binance.us reads a Binance.US account the same way, spot only: the USD-M futures wallet
    and positions are read on binance.com alone, and a key without the futures permission (or an
    account without a futures wallet) keeps its spot rows. 1000-prefixed contracts
    (1000PEPEUSDT) scale through `unscale`."""

    id = "binance"
    label = "Binance"
    URL = "https://www.binance.com/en/my/settings/api-management"
    note = ('Optional. A read-only ("Enable Reading" only, no trading or withdrawals) Binance API key shows your spot balances, and on '
            'binance.com the USD-M futures collateral and positions, on the Portfolio tab; STEEL_BINANCE_HOST=api.binance.us for Binance.US.')
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "Secret key"}]
    endpoints = (ACCOUNT, BALANCE, POSITIONS, TRADE_FEE, COMMISSION)
    RECV_WINDOW = 5000

    def __init__(self, **creds):
        super().__init__(**creds)
        self.host = os.environ.get("STEEL_BINANCE_HOST", "api.binance.com")
        self.HOST = f"https://{self.host}"
        self.futures_note = ""

    @property
    def futures(self) -> bool:
        return self.host.endswith("binance.com")

    def sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def query(self, params: dict | None = None, ts=None) -> str:
        """The signed query string: the parameters, then timestamp and recvWindow, then the signature."""
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) if ts is None else int(ts)
        p["recvWindow"] = self.RECV_WINDOW
        q = urllib.parse.urlencode(p)
        return f"{q}&signature={self.sign(q)}"

    @staticmethod
    def contract(symbol: str) -> tuple[str, float]:
        """BTCUSDT -> ("BTC", 1.0); 1000PEPEUSDT -> ("PEPE", 1000.0)."""
        s = str(symbol or "").upper()
        for q in QUOTES:
            if s.endswith(q):
                s = s[:-len(q)]
                break
        return unscale(s)

    async def _get(self, host: str, path: str, params: dict | None = None):
        return await self._json("GET", f"{host}{path}?{self.query(params)}", headers={"X-MBX-APIKEY": self.key})

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        d = await self._get(self.HOST, ACCOUNT, {"omitZeroBalances": "true"} if self.futures else None)
        for b in d.get("balances") or []:
            free, locked = fnum(b.get("free")), fnum(b.get("locked"))
            if free + locked > 0:
                out.append({"asset": self.asset(b.get("asset")), "qty": free + locked, "hold": locked, "id": "spot"})
        if not self.futures:
            return out
        try:
            wallet = (await self._get(FUTURES, BALANCE)) or []
            positions = (await self._get(FUTURES, POSITIONS)) or []
        except RuntimeError as e:                       # no futures permission or wallet: the spot rows stand
            self.futures_note = str(e)[:100]
            return out
        for b in wallet:
            q = fnum(b.get("balance"))
            if q > 0:
                out.append({"asset": self.asset(b.get("asset")), "qty": q, "hold": max(0.0, q - fnum(b.get("availableBalance", q))), "id": "futures"})
        for p in positions:
            amt = fnum(p.get("positionAmt"))
            if not amt:
                continue
            base, factor = self.contract(p.get("symbol"))
            out.append({"asset": base, "kind": "perp", "qty": amt * factor, "price": fnum(p.get("markPrice")) / factor,
                        "entry": fnum(p.get("entryPrice")) / factor, "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unRealizedProfit"))})
        return out

    async def fees(self) -> dict | None:
        """The account's rates on BTCUSDT: spot `makerCommission` / `takerCommission` and USD-M
        `makerCommissionRate` / `takerCommissionRate`, all decimal strings ("0.001" is 10 bps).
        The spot read is binance.com's `/sapi` path, which Binance.US does not serve, so on
        api.binance.us a failed spot read leaves the spot keys out; the futures read, like the
        futures balances, is binance.com only and a key without the permission keeps spot."""
        if not self.configured:
            return None
        spot_t = spot_m = perp_t = perp_m = None
        try:
            rows = await self._get(self.HOST, TRADE_FEE, {"symbol": FEE_SYMBOL})
            row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
            spot_t, spot_m = fee_bps(row.get("takerCommission")), fee_bps(row.get("makerCommission"))
        except RuntimeError as e:
            if self.futures:
                raise
            self.futures_note = str(e)[:100]
        if self.futures:
            try:
                d = await self._get(FUTURES, COMMISSION, {"symbol": FEE_SYMBOL}) or {}
                perp_t, perp_m = fee_bps(d.get("takerCommissionRate")), fee_bps(d.get("makerCommissionRate"))
            except RuntimeError as e:                   # no futures permission: the spot rates stand
                self.futures_note = str(e)[:100]
        return fee_rates(spot_taker=spot_t, spot_maker=spot_m, perp_taker=perp_t, perp_maker=perp_m)
