"""Gate.io: spot accounts, the USDT futures account and its positions, and the account's fee
rates (`GET /api/v4/wallet/fee`). Four GETs, nothing else."""
from __future__ import annotations

import hashlib
import hmac
import math
import time

from ..sources import unscale
from .base import Account, fee_bps, fee_rates, fnum

HOST = "https://api.gateio.ws"
SPOT = "/api/v4/spot/accounts"
FUT_ACCOUNT = "/api/v4/futures/usdt/accounts"
FUT_POSITIONS = "/api/v4/futures/usdt/positions"
FEE = "/api/v4/wallet/fee"                            # {taker_fee, maker_fee, futures_taker_fee, futures_maker_fee, ...}


class GateIOAccount(Account):
    """Gate.io API key and secret: SIGN is the hex HMAC-SHA512 of "METHOD\\npath\\nquery\\n
    sha512hex(body)\\ntimestamp" with a second-resolution Timestamp (the docs' worked example
    reproduces). The spot accounts, then the USDT futures account (balance plus unrealised PnL as
    USDT collateral) and its positions: `size` is in contracts, so the base amount is the
    position's `value` (settle currency) over the mark; a key without futures still shows spot."""

    id = "gateio"
    label = "Gate.io"
    URL = "https://www.gate.com/myaccount/api_key_manage"
    note = 'Optional. A read-only ("Read only" permission) API key shows your Gate.io spot and futures balances on the Portfolio tab.'
    venue = "mixed"
    fields = [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret"}]
    endpoints = (SPOT, FUT_ACCOUNT, FUT_POSITIONS, FEE)
    HOST = HOST

    def sign(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> str:
        ts = int(time.time()) if ts is None else int(ts)
        msg = "\n".join([method.upper(), path, query, hashlib.sha512(body.encode()).hexdigest(), str(ts)])
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha512).hexdigest()

    def headers(self, method: str, path: str, query: str = "", body: str = "", ts: int | None = None) -> dict:
        ts = int(time.time()) if ts is None else int(ts)
        return {"KEY": self.key, "Timestamp": str(ts), "SIGN": self.sign(method, path, query, body, ts), "Content-Type": "application/json"}

    async def _get(self, path: str, query: str = ""):
        url = f"{HOST}{path}" + (f"?{query}" if query else "")
        return await self._json("GET", url, headers=self.headers("GET", path, query))

    async def balances(self) -> list[dict]:
        out: list[dict] = []
        for a in await self._get(SPOT) or []:               # [{currency, available, locked}]
            avail, locked = fnum(a.get("available")), fnum(a.get("locked"))
            if avail + locked > 0:
                out.append({"asset": self.asset(a.get("currency")), "qty": avail + locked, "hold": locked, "id": "gateio"})
        try:
            f = await self._get(FUT_ACCOUNT) or {}          # {total, unrealised_pnl, available, currency, ...}
            equity = fnum(f.get("total")) + fnum(f.get("unrealised_pnl"))
            if equity > 0:
                out.append({"asset": self.asset(f.get("currency") or "USDT"), "qty": equity,
                            "hold": max(0.0, fnum(f.get("total")) - fnum(f.get("available"))), "id": "gateio-futures"})
            for p in await self._get(FUT_POSITIONS) or []:  # [{contract, size, value, entry_price, mark_price, unrealised_pnl, leverage, ...}]
                size, mark = fnum(p.get("size")), fnum(p.get("mark_price"))
                if size == 0 or mark <= 0:
                    continue
                base, factor = unscale(str(p.get("contract") or "").split("_")[0])
                qty = math.copysign(fnum(p.get("value")) / mark, size) * factor
                out.append({"asset": self.asset(base), "kind": "perp", "qty": qty, "price": mark / factor,
                            "entry": fnum(p.get("entry_price")) / factor, "leverage": fnum(p.get("leverage")), "upnl": fnum(p.get("unrealised_pnl"))})
        except RuntimeError:
            pass                                            # no futures permission: spot still shows
        return out

    async def fees(self) -> dict | None:
        """One read gives the whole structure: `taker_fee` / `maker_fee` (spot) and
        `futures_taker_fee` / `futures_maker_fee` (the USDT perps, `settle=usdt`), decimal strings
        ("0.002" is 20 bps) at the account's VIP tier."""
        if not self.configured:
            return None
        d = await self._get(FEE, "currency_pair=BTC_USDT&settle=usdt") or {}
        return fee_rates(spot_taker=fee_bps(d.get("taker_fee")), spot_maker=fee_bps(d.get("maker_fee")),
                         perp_taker=fee_bps(d.get("futures_taker_fee")), perp_maker=fee_bps(d.get("futures_maker_fee")))
