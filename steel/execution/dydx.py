"""dYdX v4: an order is a chain transaction. `MsgPlaceOrder` / `MsgCancelOrder` are protobuf
messages wrapped in a Cosmos SDK `TxBody`, signed over the `SignDoc` (SIGN_MODE_DIRECT) with the
account's secp256k1 key and broadcast as `POST /cosmos/tx/v1beta1/txs` on a public full node
(dydx-ops-rest.kingnodes.com; test-dydx-rest.kingnodes.com when `testnet`). The market
parameters, the block height, open orders and fills come from the public indexer
(indexer.dydx.trade); the account number and sequence from the node. The protobuf wire format
is hand-encoded here (varints and length-delimited fields; the ten messages involved have no
nested complexity), so no protobuf, grpc or dydx-v4-client package is needed."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import uuid
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from eth_keys import keys

from ..sources import base_of
from .base import Executor, Order, OrderResult

HOST = "https://dydx-ops-rest.kingnodes.com"
TESTNET = "https://test-dydx-rest.kingnodes.com"
INDEXER = "https://indexer.dydx.trade"
TESTNET_INDEXER = "https://indexer.v4testnet.dydx.exchange"
CHAIN, TESTNET_CHAIN = "dydx-mainnet-1", "dydx-testnet-4"
MARKETS, HEIGHT, ORDERS, FILLS = "/v4/perpetualMarkets", "/v4/height", "/v4/orders", "/v4/fills"
ACCOUNTS, BROADCAST = "/cosmos/auth/v1beta1/accounts/", "/cosmos/tx/v1beta1/txs"
NAMESPACE = uuid.UUID("0f9da948-a6fb-4c45-9edc-4685c3f3317d")   # the indexer's uuid5 namespace (postgres/src/helpers/uuid.ts)
SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SHORT_TERM, LONG_TERM = 0, 64                                  # order_flags
SIDE = {"buy": 1, "sell": 2}
IOC, GTT = 1, 0                                                # Order.TimeInForce
PLACE, CANCEL, PUBKEY = "/dydxprotocol.clob.MsgPlaceOrder", "/dydxprotocol.clob.MsgCancelOrder", "/cosmos.crypto.secp256k1.PubKey"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


# Protobuf wire format ----------------------------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _u(field: int, n: int) -> bytes:
    """A varint field (uint32/uint64/enum/bool), omitted at zero as proto3 does."""
    return _varint(field << 3) + _varint(int(n)) if n else b""


def _f32(field: int, n: int) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<I", int(n)) if n else b""


def _ld(field: int, v: bytes | str) -> bytes:
    """A length-delimited field (string, bytes, embedded message), omitted when empty."""
    v = v.encode() if isinstance(v, str) else v
    return _varint(field << 3 | 2) + _varint(len(v)) + v if v else b""


def _any(type_url: str, value: bytes) -> bytes:
    return _ld(1, type_url) + _ld(2, value)


def order_id(owner: str, number: int, client_id: int, flags: int, clob_pair_id: int) -> bytes:
    """OrderId{subaccount_id{owner, number}, client_id (fixed32), order_flags, clob_pair_id}."""
    return _ld(1, _ld(1, owner) + _u(2, number)) + _f32(2, client_id) + _u(3, flags) + _u(4, clob_pair_id)


def encode_order(oid: bytes, side: int, quantums: int, subticks: int, *, good_til_block: int = 0, good_til_block_time: int = 0,
                 time_in_force: int = GTT, reduce_only: bool = False, client_metadata: int = 0) -> bytes:
    return (_ld(1, oid) + _u(2, side) + _u(3, quantums) + _u(4, subticks) + _u(5, good_til_block) + _f32(6, good_til_block_time)
            + _u(7, time_in_force) + _u(8, int(reduce_only)) + _u(9, client_metadata))


def msg_place(order: bytes) -> bytes:
    return _any(PLACE, _ld(1, order))


def msg_cancel(oid: bytes, good_til_block: int = 0, good_til_block_time: int = 0) -> bytes:
    return _any(CANCEL, _ld(1, oid) + _u(2, good_til_block) + _f32(3, good_til_block_time))


def sign_tx(key: keys.PrivateKey, msgs: list[bytes], chain_id: str, account_number: int, sequence: int, *, memo: str = "Steel",
            gas_limit: int = 1_000_000) -> bytes:
    """TxRaw bytes: body (the Any-wrapped messages, memo), auth info (the compressed public key,
    SIGN_MODE_DIRECT, the sequence, a fee of `gas_limit` gas and no coins: CLOB messages pay no
    gas on dYdX, and this is the official client's default), and one 64-byte r||s signature over
    sha256(SignDoc{body, auth, chain_id, account_number}), deterministic (RFC 6979) and low-s
    as Cosmos requires."""
    body = b"".join(_ld(1, m) for m in msgs) + _ld(2, memo)
    signer = _ld(1, _any(PUBKEY, _ld(1, key.public_key.to_compressed_bytes()))) + _ld(2, _ld(1, _u(1, 1))) + _u(3, sequence)
    auth = _ld(1, signer) + _ld(2, _u(2, gas_limit))
    doc = _ld(1, body) + _ld(2, auth) + _ld(3, chain_id) + _u(4, account_number)
    sig = key.sign_msg_hash(hashlib.sha256(doc).digest())
    r, s = sig.r, sig.s if sig.s <= SECP_N // 2 else SECP_N - sig.s
    return _ld(1, body) + _ld(2, auth) + _ld(3, r.to_bytes(32, "big") + s.to_bytes(32, "big"))


# Keys and addresses --------------------------------------------------------------------------

def bech32(hrp: str, data: bytes) -> str:
    bits = acc = 0
    out: list[int] = []
    for b in data:
        acc, bits = (acc << 8) | b, bits + 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    vals = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + out + [0] * 6
    chk = 1
    for v in vals:
        top, chk = chk >> 25, ((chk & 0x1FFFFFF) << 5) ^ v
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= g if (top >> i) & 1 else 0
    chk ^= 1
    return hrp + "1" + "".join(CHARSET[d] for d in out + [(chk >> 5 * (5 - i)) & 31 for i in range(6)])


def address_of(key: keys.PrivateKey) -> str | None:
    """bech32("dydx", ripemd160(sha256(compressed pubkey))); None when OpenSSL lacks ripemd160."""
    try:
        h = hashlib.new("ripemd160", hashlib.sha256(key.public_key.to_compressed_bytes()).digest()).digest()
    except ValueError:
        return None
    return bech32("dydx", h)


def indexer_order_uuid(address: str, subaccount: int, client_id: int, clob_pair_id: int, flags: int) -> str:
    sub = uuid.uuid5(NAMESPACE, f"{address}-{subaccount}")
    return str(uuid.uuid5(NAMESPACE, f"{sub}-{client_id}-{clob_pair_id}-{flags}"))


# Sizing ------------------------------------------------------------------------------------------

def quantums(size: float, market: dict) -> int:
    """size x 10^-atomicResolution, floored to stepBaseQuantums (never upsized); 0 under a step."""
    step = int(market["stepBaseQuantums"])
    raw = Decimal(str(size)).scaleb(-int(market["atomicResolution"]))
    return int((raw / step).to_integral_value(ROUND_FLOOR)) * step


def subticks(price: float, market: dict, side: str = "buy") -> int:
    """price x 10^(atomicResolution - quantumConversionExponent + 6) (6: USDC's resolution), to a
    multiple of subticksPerTick: floored for a buy, ceiled for a sell, so the order never pays
    more than asked."""
    tick = int(market["subticksPerTick"])
    raw = Decimal(str(price)).scaleb(int(market["atomicResolution"]) - int(market["quantumConversionExponent"]) + 6)
    return int((raw / tick).to_integral_value(ROUND_FLOOR if side == "buy" else ROUND_CEILING)) * tick


def _iso(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


class DydxExecutor(Executor):
    """A dYdX address and its secp256k1 private key (64 hex, the operator derives it: with the
    account's 24-word mnemonic, `eth_account.Account.from_mnemonic(words,
    account_path="m/44'/118'/0'/0/0").key.hex()` is the Cosmos path dYdX wallets use). The key
    must derive the address (checked before anything is sent). Subaccount 0 trades; markets are
    already BASE-USD. A market order is what the venue has for one: a short-term IOC limit at
    the oracle price +/-`SLIPPAGE` (order_flags 0, good for `BLOCKS_AHEAD` blocks, free). A limit
    order is long-term (order_flags 64, good `GOOD_TIL_S` seconds, GTT, also free). Reduce-only
    is legal only on short-term IOC orders (`ErrReduceOnlyDisabled`), so a reduce-only limit
    goes as a short-term IOC at that price. Quantity floors to `stepBaseQuantums`; under one
    step the order is refused here. `client_id` is a fixed32: a decimal string is used as is, any
    other string is the first four bytes of its sha256, none is random. The result's `order_id`
    is the indexer's uuid for the order; `_cancel` takes that, from the orders this instance
    placed (`known`) or the indexer. In dry-run nothing is read either: seed `markets[sym]`
    (the indexer's perpetualMarkets row) and `chain` ({height, account_number, sequence}) to
    build a deterministic tx."""

    id = "dydx"
    label = "dYdX"
    venue = "perp"
    fields = [{"id": "address", "label": "dYdX chain address (dydx1...)"}, {"id": "private_key", "label": "secp256k1 private key (64 hex)"}]
    endpoints = (MARKETS, HEIGHT, ORDERS, ORDERS + "/*", FILLS, ACCOUNTS + "*", BROADCAST)
    HOST = HOST
    TESTNET = TESTNET
    SLIPPAGE = 0.05
    BLOCKS_AHEAD = 10          # under the chain's ShortBlockWindow (40)
    GOOD_TIL_S = 86_400        # under StatefulOrderTimeWindow (95 days)
    SUBACCOUNT = 0

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None
        self.markets: dict[str, dict] = {}
        self.chain: dict = {}
        self.known: dict[str, dict] = {}

    @property
    def configured(self) -> bool:
        return self.address.startswith("dydx1") and len(self.address) > 20 and len(self.private_key.lower().removeprefix("0x")) == 64

    @property
    def indexer(self) -> str:
        return TESTNET_INDEXER if self.testnet else INDEXER

    @property
    def chain_id(self) -> str:
        return TESTNET_CHAIN if self.testnet else CHAIN

    def key(self) -> keys.PrivateKey:
        return keys.PrivateKey(bytes.fromhex(self.private_key.lower().removeprefix("0x")))

    def stamp(self) -> float:
        return time.time() if self.now is None else self.now

    @staticmethod
    def inst(sym: str) -> str:
        return f"{base_of(sym)}-USD"

    @staticmethod
    def client_id(cid: str) -> int:
        if cid.isdigit() and int(cid) < 2**32:
            return int(cid)
        return int.from_bytes(hashlib.sha256(cid.encode()).digest()[:4] if cid else os.urandom(4), "big")

    # Reads (through the gate; a dry-run gets the seeded caches) -------------------------------
    async def _get(self, url: str, params: dict | None = None) -> dict:
        _, ans = await self._send("GET", url, params=params)
        return ans

    async def _market(self, sym: str) -> dict:
        inst = self.inst(sym)
        if not self.dry_run:
            ans = await self._get(f"{self.indexer}{MARKETS}", {"ticker": inst})
            m = (ans.get("markets") or {}).get(inst)
            if not m:
                raise ValueError(f"{inst} is not a dYdX market")
            self.markets[inst] = m
        if inst not in self.markets:
            raise ValueError(f"dry-run: no market parameters for {inst} (seed `markets`)")
        return self.markets[inst]

    async def _state(self) -> dict:
        if not self.dry_run:
            h = await self._get(f"{self.indexer}{HEIGHT}")
            a = (await self._get(f"{self.host}{ACCOUNTS}{self.address}")).get("account") or {}
            a = a.get("base_account", a)
            self.chain = {"height": int(h.get("height") or 0), "account_number": int(a.get("account_number") or 0), "sequence": int(a.get("sequence") or 0)}
        return {"height": 0, "account_number": 0, "sequence": 0} | self.chain

    async def _broadcast(self, msg: bytes, state: dict) -> tuple[dict, dict]:
        tx = sign_tx(self.key(), [msg], self.chain_id, state["account_number"], state["sequence"])
        body = {"tx_bytes": base64.b64encode(tx).decode(), "mode": "BROADCAST_MODE_SYNC"}
        return await self._send("POST", f"{self.host}{BROADCAST}", content=json.dumps(body), headers={"Content-Type": "application/json"})

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        tr = ans.get("tx_response") or {}
        if tr and int(tr.get("code") or 0) == 0:
            return OrderResult(True, self.id, order_id=order_id, status="accepted", raw=ans, request=req)
        msg = (f"{tr.get('raw_log') or ''} ({tr.get('codespace')} {tr.get('code')})" if tr
               else f"{ans.get('message') or ans.get('text') or ''} (HTTP {ans.get('_status')}, code {ans.get('code')})")
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=msg[:160], raw=ans, request=req)

    # The contract ------------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        derived = address_of(self.key())
        if derived is not None and derived != self.address:
            return OrderResult(False, self.id, status="rejected", error="the private key does not derive the configured address")
        m = await self._market(order.symbol)
        q = quantums(order.qty, m)
        if q <= 0:
            return OrderResult(False, self.id, status="rejected", error=f"{order.qty:g} is under one step ({m.get('stepSize')})")
        state = await self._state()
        clob, cid = int(m["clobPairId"]), self.client_id(order.client_id or "")
        short = order.price is None or order.reduce_only
        px = order.price if order.price is not None else float(m["oraclePrice"]) * (1 + self.SLIPPAGE if order.side == "buy" else 1 - self.SLIPPAGE)
        flags = SHORT_TERM if short else LONG_TERM
        oid = order_id(self.address, self.SUBACCOUNT, cid, flags, clob)
        enc = encode_order(oid, SIDE[order.side], q, subticks(px, m, order.side), time_in_force=IOC if short else GTT, reduce_only=order.reduce_only,
                           good_til_block=state["height"] + self.BLOCKS_AHEAD if short else 0,
                           good_til_block_time=0 if short else int(self.stamp()) + self.GOOD_TIL_S, client_metadata=int(order.price is None))
        uid = indexer_order_uuid(self.address, self.SUBACCOUNT, cid, clob, flags)
        self.known[uid] = {"clientId": cid, "clobPairId": clob, "orderFlags": flags, "goodTilBlockTime": 0 if short else int(self.stamp()) + self.GOOD_TIL_S}
        req, ans = await self._broadcast(msg_place(enc), state)
        req["order"] = {"clob_pair_id": clob, "client_id": cid, "order_flags": flags, "quantums": q, "subticks": subticks(px, m, order.side), "short_term": short}
        return self._result(req, ans, order_id=uid)

    async def _cancel(self, order_id_: str, symbol: str) -> OrderResult:
        info = self.known.get(order_id_)
        if info is None:
            o = await self._get(f"{self.indexer}{ORDERS}/{order_id_}")
            if not o.get("clobPairId"):
                return OrderResult(False, self.id, order_id=order_id_, status="rejected", error="unknown order (not placed here, not on the indexer)")
            info = {"clientId": int(o["clientId"]), "clobPairId": int(o["clobPairId"]), "orderFlags": int(o.get("orderFlags") or 0),
                    "goodTilBlockTime": int(_iso(o["goodTilBlockTime"])) if o.get("goodTilBlockTime") else 0}
        state = await self._state()
        oid = order_id(self.address, self.SUBACCOUNT, info["clientId"], info["orderFlags"], info["clobPairId"])
        short = info["orderFlags"] == SHORT_TERM
        msg = msg_cancel(oid, good_til_block=state["height"] + self.BLOCKS_AHEAD if short else 0,
                         good_til_block_time=0 if short else max(info["goodTilBlockTime"], int(self.stamp()) + 60))
        req, ans = await self._broadcast(msg, state)
        return self._result(req, ans, order_id=order_id_)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"address": self.address, "subaccountNumber": self.SUBACCOUNT, "status": ["OPEN", "BEST_EFFORT_OPENED"], "limit": 100}
        if symbol:
            params["ticker"] = self.inst(symbol)
        ans = await self._get(f"{self.indexer}{ORDERS}", params)
        return [{"order_id": str(o.get("id")), "symbol": str(o.get("ticker", "")), "side": str(o.get("side", "")).lower(), "qty": float(o.get("size") or 0),
                 "price": float(o.get("price") or 0), "filled": float(o.get("totalFilled") or 0), "status": str(o.get("status", "")).lower()}
                for o in ans.get("data") or []]

    async def fills(self, since: float) -> list[dict]:
        ans = await self._get(f"{self.indexer}{FILLS}", {"address": self.address, "subaccountNumber": self.SUBACCOUNT, "limit": 100})
        out = []
        for f in ans.get("fills") or []:
            t = _iso(f["createdAt"]) if f.get("createdAt") else 0.0
            if t >= since:
                out.append({"order_id": str(f.get("orderId") or ""), "symbol": str(f.get("market", "")), "side": str(f.get("side", "")).lower(),
                            "qty": float(f.get("size") or 0), "price": float(f.get("price") or 0), "fee": float(f.get("fee") or 0), "t": t})
        return out
