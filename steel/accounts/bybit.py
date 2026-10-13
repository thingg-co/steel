"""Bybit: `GET /v5/account/wallet-balance` (the unified account), `GET /v5/position/list`
(the USDT- and USDC-settled linear perps) and the account's fee rates from
`GET /v5/account/fee-rate`, nothing else."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

from ..sources import unscale
from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.bybit.com"
WALLET = "/v5/account/wallet-balance"
POSITIONS = "/v5/position/list"
FEE_RATE = "/v5/account/fee-rate"                     # {result: {list: [{symbol, takerFeeRate, makerFeeRate}]}}
FEE_SYMBOL = "BTCUSDT"
SUFFIXES = ("USDT", "USDC", "PERP")


class BybitAccount(Account):
    """Bybit API key + secret: X-BAPI-SIGN = hex(HMAC-SHA256(secret, timestamp + key +
    recv_window + queryString)) beside X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-RECV-WINDOW and
    X-BAPI-SIGN-TYPE 2. The unified account holds the spot coins and the derivatives collateral
    in one wallet; positions are the linear perps settled in USDT (BTCUSDT) and USDC (BTCPERP),
    1000PEPEUSDT scaling through `unscale`. The separate funding wallet's balance endpoint sits
    on a path this reader may not declare, so it is not read."""

    id = "bybit"
    label = "Bybit"
    URL = "https://www.bybit.com/app/user/api-management"
    note = 'Optional. A read-only ("Read-Only" permission, no trading) Bybit API key shows your unified-account balances and open linear perpetual positions on the Portfolio tab.'
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (WALLET, POSITIONS, FEE_RATE)
    HOST = HOST
    RECV_WINDOW = 5000

    def sign(self, ts, query: str) -> str:
        data = f"{ts}{self.key}{self.RECV_WINDOW}{query}"
        return hmac.new(self.secret.encode(), data.encode(), hashlib.sha256).hexdigest()

    def headers(self, query: str, ts=None) -> dict:
        ts = str(int(time.time() * 1000) if ts is None else ts)
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(self.RECV_WINDOW),
                "X-BAPI-SIGN-TYPE": "2", "X-BAPI-SIGN": self.sign(ts, query)}

    @staticmethod
    def contract(symbol: str) -> tuple[str, float]:
        """BTCUSDT and BTCPERP -> ("BTC", 1.0); 1000PEPEUSDT -> ("PEPE", 1000.0)."""
        s = str(symbol or "").upper()
        for q in SUFFIXES:
            if s.endswith(q):
                s = s[:-len(q)]
                break
        return unscale(s)

    async def _get(self, path: str, params: dict) -> dict:
        q = urllib.parse.urlencode(params)
        try:
            d = await self._json("GET", f"{HOST}{path}?{q}", headers=self.headers(q))
        except RuntimeError as e:
            if str(e).strip() == "HTTP 401:":                   # an unknown key gets a bodiless 401 from the edge
                raise RuntimeError("HTTP 401: key not accepted (unknown key, or not a Read-Only v5 key)") from None
            raise
        if d.get("retCode") != 0:
            raise RuntimeError(f"{d.get('retMsg')} ({d.get('retCode')})"[:120])
        return d.get("result") or {}

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for acct in (await self._get(WALLET, {"accountType": "UNIFIED"})).get("list") or []:
            for c in acct.get("coin") or []:
                q = fnum(c.get("walletBalance"))
                if q > 0:
                    out.append({"asset": self.asset(c.get("coin")), "qty": q, "hold": fnum(c.get("locked")), "id": "unified"})
        for settle in ("USDT", "USDC"):
            cursor = ""
            for _ in range(5):
                params = {"category": "linear", "settleCoin": settle, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                res = await self._get(POSITIONS, params)
                for p in res.get("list") or []:
                    size = fnum(p.get("size"))
                    if not size:
                        continue
                    base, factor = self.contract(p.get("symbol"))
                    sign = -1.0 if p.get("side") == "Sell" else 1.0
                    out.append({"asset": base, "kind": "perp", "qty": sign * size * factor, "price": fnum(p.get("markPrice")) / factor,
                                "entry": fnum(p.get("avgPrice")) / factor, "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unrealisedPnl"))})
                cursor = res.get("nextPageCursor") or ""
                if not cursor:
                    break
        return out

    async def fees(self) -> dict | None:
        """The account's rates on BTCUSDT, spot and linear: `takerFeeRate` / `makerFeeRate` are
        decimal strings ("0.001" is 10 bps)."""
        if not self.configured:
            return None
        rates = {}
        for category in ("spot", "linear"):
            rows = (await self._get(FEE_RATE, {"category": category, "symbol": FEE_SYMBOL})).get("list") or []
            row = rows[0] if rows else {}
            rates[category] = (fee_bps(row.get("takerFeeRate")), fee_bps(row.get("makerFeeRate")))
        return fee_rates(spot_taker=rates["spot"][0], spot_maker=rates["spot"][1], perp_taker=rates["linear"][0], perp_maker=rates["linear"][1])
