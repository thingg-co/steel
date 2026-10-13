"""Hyperliquid: `POST /exchange` (order, cancel, cancelByCloid) and `POST /info` (meta,
openOrders, userFills) on api.hyperliquid.xyz (api.hyperliquid-testnet.xyz when `testnet`), the
USDC-margined perps signed by an approved API (agent) wallet, byte for byte as the official
python SDK signs."""
from __future__ import annotations

import math
import re
import time
from decimal import ROUND_DOWN, Decimal

import msgpack
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from ..sources import base_of, unscale
from .base import Executor, Order, OrderResult

EXCHANGE = "/exchange"
INFO = "/info"
SCALED = {"PEPE", "SHIB", "BONK", "LUNC", "FLOKI", "DOGS", "NEIRO"}     # listed as kPEPE...: a price per thousand tokens
SLIPPAGE = 0.005                                                        # the SDK's market_open default
# Asset index and szDecimals from the live meta (POST /info {"type":"meta"}, 2026-10-11), the dry
# run's table: the index is the coin's position in `universe` and differs per network. A live
# client reloads the meta from its host before its first order and hourly after.
MAINNET_META = {"BTC": (0, 5), "ETH": (1, 4), "SOL": (5, 2), "DOGE": (12, 0), "kPEPE": (15, 0)}
TESTNET_META = {"SOL": (0, 2), "BTC": (3, 5), "ETH": (4, 4), "kPEPE": (12, 0), "DOGE": (173, 0)}
DOMAIN = {"chainId": 1337, "name": "Exchange", "verifyingContract": "0x0000000000000000000000000000000000000000", "version": "1"}
TYPES = {"Agent": [{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}],
         "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"},
                          {"name": "verifyingContract", "type": "address"}]}
CLOID = re.compile(r"^0x[0-9a-fA-F]{32}$")


def wire(x: float) -> str:
    """The SDK's `float_to_wire`: 8 decimals, trailing zeros stripped, a rounding error refused."""
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError(f"float_to_wire causes rounding: {x}")
    if rounded == "-0":
        rounded = "0"
    return f"{Decimal(rounded).normalize():f}"


def action_hash(action: dict, nonce: int, vault: str = "") -> bytes:
    """keccak(msgpack(action) + nonce as 8 big-endian bytes + 0x00, or 0x01 + the vault's 20 bytes)."""
    data = msgpack.packb(action) + int(nonce).to_bytes(8, "big")
    data += b"\x01" + bytes.fromhex(vault[2:] if vault.startswith("0x") else vault) if vault else b"\x00"
    return keccak(data)


class HyperliquidExecutor(Executor):
    """An account address plus the private key of an API wallet approved for it (app -> More ->
    API): an agent signs orders and cancels and nothing else; it cannot withdraw, transfer or
    approve another agent, which is the point of trading with one. Each action is msgpacked, hashed
    with the nonce (the ms timestamp) and the vault flag, and that hash signed as EIP-712
    `Agent{source: "a" | "b", connectionId}` under domain Exchange/1/chainId 1337: the SDK's
    `sign_l1_action`, and the test pins its published vectors. `vault` set trades that vault with
    the agent's account as the leader (`vaultAddress` in the envelope, folded into the hash). A
    market order is the SDK's: an IOC limit at the reference price pushed 0.5% toward the taker
    side; a limit is GTC, its price rounded to the passive side. Prices carry 5 significant
    figures and at most 6 - szDecimals decimals; sizes are cut (never raised) to szDecimals.
    `kPEPE` and friends quote a thousand tokens (`unscale`). A client id becomes the venue's
    16-byte `cloid`: used as-is when it already is one, else the first 16 bytes of its keccak.
    The answer per order is `resting` (an oid), `filled` (oid, totalSz, avgPx) or `error`; an
    unknown or unapproved agent answers `status: err` with a string. Orders for a vault, and
    its open orders and fills, live under the vault's address."""

    id = "hyperliquid"
    label = "Hyperliquid"
    venue = "perp"
    fields = [{"id": "address", "label": "Account address (0x...)"}, {"id": "agent_key", "label": "API wallet private key (hex)"},
              {"id": "vault", "label": "Vault address (optional)"}]
    endpoints = (EXCHANGE, INFO)
    HOST = "https://api.hyperliquid.xyz"
    TESTNET = "https://api.hyperliquid-testnet.xyz"
    SCALED = SCALED

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests
        self.meta: dict[str, tuple[int, int]] = {}   # coin -> (asset index, szDecimals), from the live meta
        self.meta_at = 0.0
        self._last_nonce = 0

    @property
    def configured(self) -> bool:
        return self.address.lower().startswith("0x") and len(self.address) == 42 and bool(self.agent_key)

    @property
    def account(self) -> str:
        return self.vault or self.address

    def stamp(self) -> int:
        """The nonce: ms since the epoch, strictly increasing within the process."""
        self._last_nonce = max(self._last_nonce + 1, int((time.time() if self.now is None else self.now) * 1000))
        return self._last_nonce

    # Symbols and meta ---------------------------------------------------------------------
    @classmethod
    def coin(cls, sym: str) -> tuple[str, float]:
        """BTC-USD -> ("BTC", 1.0); PEPE-USD -> ("kPEPE", 1000.0)."""
        base = base_of(sym)
        return (f"k{base}", 1000.0) if base in cls.SCALED else (base, 1.0)

    @staticmethod
    def canonical(coin: str) -> tuple[str, float]:
        base, factor = unscale(str(coin or ""))
        return f"{base}-USD", factor

    def seed(self) -> dict:
        return TESTNET_META if self.testnet else MAINNET_META

    async def load_meta(self, max_age: float = 3600.0) -> dict:
        if self.meta and time.time() - self.meta_at < max_age:
            return self.meta
        _, ans = await self._send("POST", f"{self.host}{INFO}", json_body={"type": "meta"}, headers={"Content-Type": "application/json"})
        meta = {}
        for i, u in enumerate(ans.get("universe") or []):
            if u.get("name"):
                meta[u["name"]] = (i, int(u.get("szDecimals") or 0))
        if meta:
            self.meta, self.meta_at = meta, time.time()
        return self.meta

    def asset(self, sym: str) -> tuple[int, int, float]:
        """(asset index, szDecimals, factor) for a canonical symbol, or a ValueError."""
        coin, factor = self.coin(sym)
        spec = (self.meta or self.seed()).get(coin)
        if spec is None:
            raise ValueError(f"{coin} is not in the {'live' if self.meta else 'seed'} meta")
        return spec[0], spec[1], factor

    # Rounding --------------------------------------------------------------------------------
    @staticmethod
    def size(qty: float, sz_decimals: int) -> str:
        """Cut to szDecimals, never raised."""
        q = Decimal(str(qty)).quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)
        if q <= 0:
            raise ValueError(f"size {qty} rounds to nothing at {sz_decimals} decimals")
        return wire(float(q))

    @staticmethod
    def price(px: float, sz_decimals: int, side: str | None = None) -> str:
        """5 significant figures and at most 6 - szDecimals decimals: the SDK's `_slippage_price`
        rounding (half-up) for a market price, or to the passive side for a limit (`side`:
        a buy rounds down, a sell up)."""
        decimals = 6 - sz_decimals
        if side is None:
            return wire(round(float(f"{px:.5g}"), decimals))
        exp = math.floor(math.log10(px)) - 4 if px > 0 else 0
        step = Decimal(10) ** max(exp, -decimals)
        d = Decimal(str(px)) / step
        q = (d.to_integral_value(rounding="ROUND_FLOOR") if side == "buy" else d.to_integral_value(rounding="ROUND_CEILING")) * step
        return wire(float(q))

    @staticmethod
    def cloid(client_id: str) -> str:
        cid = str(client_id or "").strip()
        return cid.lower() if CLOID.match(cid) else "0x" + keccak(cid.encode())[:16].hex()

    # Signing ---------------------------------------------------------------------------------
    def sign(self, action: dict, nonce: int) -> dict:
        h = action_hash(action, nonce, self.vault)
        typed = {"domain": DOMAIN, "types": TYPES, "primaryType": "Agent", "message": {"source": "b" if self.testnet else "a", "connectionId": h}}
        signed = Account.sign_message(encode_typed_data(full_message=typed), private_key=self.agent_key)
        return {"r": hex(signed["r"]), "s": hex(signed["s"]), "v": signed["v"]}

    def envelope(self, action: dict, nonce: int) -> dict:
        body = {"action": action, "nonce": nonce, "signature": self.sign(action, nonce)}
        if self.vault:
            body["vaultAddress"] = self.vault
        return body

    def order_action(self, order: Order) -> dict:
        asset, sz_decimals, factor = self.asset(order.symbol)
        market = order.price is None
        if market:
            ref = order.notional_usd / order.qty if order.qty else 0.0
            if ref <= 0:
                raise ValueError("a market order needs a reference price (order.notional_usd)")
            px = self.price(ref * factor * (1 + SLIPPAGE if order.side == "buy" else 1 - SLIPPAGE), sz_decimals)
        else:
            px = self.price(order.price * factor, sz_decimals, order.side)
        o = {"a": asset, "b": order.side == "buy", "p": px, "s": self.size(order.qty / factor, sz_decimals), "r": bool(order.reduce_only),
             "t": {"limit": {"tif": "Ioc" if market else "Gtc"}}}
        if order.client_id:
            o["c"] = self.cloid(order.client_id)
        return {"type": "order", "orders": [o], "grouping": "na"}

    # Transport --------------------------------------------------------------------------------
    async def _exchange(self, action: dict) -> tuple[dict, dict]:
        body = self.envelope(action, self.stamp())
        req, ans = await self._send("POST", f"{self.host}{EXCHANGE}", json_body=body, headers={"Content-Type": "application/json"})
        req["json"] = {**body, "signature": "***"}
        return req, ans

    async def _info(self, body: dict) -> list:
        _, ans = await self._send("POST", f"{self.host}{INFO}", json_body=body, headers={"Content-Type": "application/json"})
        return ans.get("data") if isinstance(ans.get("data"), list) else []

    def _result(self, req: dict, ans: dict, order_id: str = "", cancel: bool = False) -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        if ans.get("status") == "ok":
            statuses = ((ans.get("response") or {}).get("data") or {}).get("statuses") or [{}]
            st = statuses[0]
            if cancel and st == "success":
                return OrderResult(True, self.id, order_id=order_id, status="cancelled", raw=ans, request=req)
            if isinstance(st, dict) and "resting" in st:
                return OrderResult(True, self.id, order_id=str(st["resting"].get("oid", "")), status="accepted", raw=ans, request=req)
            if isinstance(st, dict) and "filled" in st:
                f = st["filled"]
                return OrderResult(True, self.id, order_id=str(f.get("oid", "")), status="filled", filled_qty=float(f.get("totalSz") or 0),
                                   avg_price=float(f.get("avgPx") or 0) or None, raw=ans, request=req)
            msg = str(st.get("error") if isinstance(st, dict) else st)
        elif "status" in ans:
            msg = str(ans.get("response"))
        else:
            msg = f"HTTP {ans.get('_status')}: {ans.get('text', '')}"
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=msg[:160], raw=ans, request=req)

    # The contract -----------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        if not self.dry_run:
            await self.load_meta()
        try:
            action = self.order_action(order)
        except ValueError as e:
            return OrderResult(False, self.id, status="refused", error=f"{self.label}: {e}")
        req, ans = await self._exchange(action)
        res = self._result(req, ans)
        if res.status == "filled":
            res.filled_qty *= self.coin(order.symbol)[1]
            if res.avg_price:
                res.avg_price /= self.coin(order.symbol)[1]
        return res

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        if not self.dry_run:
            await self.load_meta()
        asset = self.asset(symbol)[0]
        oid = str(order_id)
        action = ({"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": oid.lower()}]} if CLOID.match(oid)
                  else {"type": "cancel", "cancels": [{"a": asset, "o": int(oid)}]})
        req, ans = await self._exchange(action)
        return self._result(req, ans, order_id=oid, cancel=True)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        out = []
        for o in await self._info({"type": "openOrders", "user": self.account}):
            coin = str(o.get("coin", ""))
            if coin.startswith("@") or "/" in coin:                    # spot orders share the list
                continue
            sym, factor = self.canonical(coin)
            if symbol and sym != symbol:
                continue
            orig, left = float(o.get("origSz") or o.get("sz") or 0), float(o.get("sz") or 0)
            out.append({"order_id": str(o.get("oid")), "symbol": sym, "side": "buy" if o.get("side") == "B" else "sell", "qty": orig * factor,
                        "price": float(o.get("limitPx") or 0) / factor, "filled": max(0.0, orig - left) * factor, "status": "open",
                        "client_id": str(o.get("cloid") or "")})
        return out

    async def fills(self, since: float) -> list[dict]:
        out = []
        for f in await self._info({"type": "userFills", "user": self.account}):
            coin = str(f.get("coin", ""))
            t = float(f.get("time") or 0) / 1000.0
            if t < since or coin.startswith("@") or "/" in coin:
                continue
            sym, factor = self.canonical(coin)
            out.append({"order_id": str(f.get("oid")), "symbol": sym, "side": "buy" if f.get("side") == "B" else "sell", "qty": float(f.get("sz") or 0) * factor,
                        "price": float(f.get("px") or 0) / factor, "fee": float(f.get("fee") or 0), "t": t})
        return out
