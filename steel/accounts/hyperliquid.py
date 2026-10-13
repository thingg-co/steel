"""Hyperliquid: `POST /info` with `clearinghouseState` (the perp account),
`spotClearinghouseState` (spot balances) and `userFees` (the address's fee rates) for a wallet
address. Address only: the info endpoint is public and needs no key, so there is nothing here
that could ever sign."""
from __future__ import annotations

from ..sources import unscale
from .base import Account, fee_bps, fee_rates, fnum, mask

HOST = "https://api.hyperliquid.xyz"
INFO = "/info"
SPOT_ALIAS = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL", "UFART": "FARTCOIN", "UPUMP": "PUMP"}   # Unit-bridged spot tokens


class HyperliquidAccount(Account):
    """A wallet address and nothing else. `clearinghouseState` reports the margin summary and
    `assetPositions` (`szi` signed in coins, `kPEPE` and friends in thousands, `positionValue` the
    notional at the mark, `leverage.value`); `spotClearinghouseState` reports `total` and `hold`
    per spot coin. The perp collateral (account value less the open PnL, which the positions
    carry) is reported as USDC. A malformed address answers 422."""

    id = "hyperliquid"
    label = "Hyperliquid"
    URL = "https://app.hyperliquid.xyz/portfolio"
    note = ("Optional. Your public wallet address alone (no API key: Hyperliquid's info endpoint is read-only and needs no "
            "permission) shows your Hyperliquid perp and spot balances on the Portfolio tab.")
    venue = "mixed"
    fields = [{"id": "address", "label": "Wallet address (0x...)"}]
    endpoints = (INFO,)
    HOST = HOST

    @property
    def configured(self) -> bool:
        return self.address.lower().startswith("0x") and len(self.address) == 42

    @staticmethod
    def asset(code: str) -> str:
        """`kPEPE` -> PEPE (the size is scaled by the caller), `UBTC` -> BTC."""
        base = unscale(str(code or ""))[0].upper()
        return SPOT_ALIAS.get(base, base)

    def parse(self, perp: dict, spot: dict) -> list[dict]:
        acct = mask(self.address)
        out: list[dict] = []
        open_pnl = 0.0
        for ap in perp.get("assetPositions") or []:
            p = ap.get("position") or {}
            coin = str(p.get("coin") or "")
            factor = unscale(coin)[1]
            qty = fnum(p.get("szi")) * factor
            if not qty:
                continue
            upnl = fnum(p.get("unrealizedPnl"))
            open_pnl += upnl
            out.append({"asset": self.asset(coin), "kind": "perp", "qty": qty, "price": fnum(p.get("positionValue")) / abs(qty),
                        "entry": fnum(p.get("entryPx")) / factor, "leverage": fnum((p.get("leverage") or {}).get("value")), "upnl": upnl, "id": acct})
        summary = perp.get("marginSummary") or {}
        collateral = fnum(summary.get("accountValue")) - open_pnl
        if collateral > 0:
            out.append({"asset": "USDC", "qty": collateral, "hold": fnum(summary.get("totalMarginUsed")), "id": acct})
        for b in spot.get("balances") or []:
            coin = str(b.get("coin") or "")
            factor = unscale(coin)[1]
            total = fnum(b.get("total")) * factor
            if total > 0:
                out.append({"asset": self.asset(coin), "qty": total, "hold": fnum(b.get("hold")) * factor, "id": acct})
        return out

    async def balances(self) -> list[dict]:
        perp = await self._json("POST", f"{HOST}{INFO}", json={"type": "clearinghouseState", "user": self.address})
        spot = await self._json("POST", f"{HOST}{INFO}", json={"type": "spotClearinghouseState", "user": self.address})
        return self.parse(perp or {}, spot or {})

    async def fees(self) -> dict | None:
        """`userFees`: the address's effective rates after its volume tier and discounts,
        `userCrossRate` (perp taker) / `userAddRate` (perp maker) and the spot pair
        `userSpotCrossRate` / `userSpotAddRate`, decimal strings ("0.00045" is 4.5 bps)."""
        if not self.configured:
            return None
        d = await self._json("POST", f"{HOST}{INFO}", json={"type": "userFees", "user": self.address}) or {}
        return fee_rates(spot_taker=fee_bps(d.get("userSpotCrossRate")), spot_maker=fee_bps(d.get("userSpotAddRate")),
                         perp_taker=fee_bps(d.get("userCrossRate")), perp_maker=fee_bps(d.get("userAddRate")))
