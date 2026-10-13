"""Aster V3: `POST /fapi/v3/order`, `DELETE /fapi/v3/order`, `GET /fapi/v3/openOrders` and
`GET /fapi/v3/userTrades` on fapi.asterdex.com (fapi.asterdex-testnet.com when `testnet`), the
Binance-futures request shape signed by an EVM API wallet (the V3 "Pro API" recipe) instead of
the V1 HMAC key the account reader uses."""
from __future__ import annotations

import re
import time
import urllib.parse
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data

from ..sources import base_of, unscale
from .base import Executor, Order, OrderResult

ORDER = "/fapi/v3/order"
OPEN_ORDERS = "/fapi/v3/openOrders"
TRADES = "/fapi/v3/userTrades"
QUOTE = "USDT"
# The names Aster lists as 1000x contracts (exchangeInfo, 2026-10-11); 1000NEX and the rest of the
# prefixed list have no plain-name twin, so `unscale` inverts them all.
SCALED = {"PEPE", "SHIB", "FLOKI", "BONK", "CHEEMS", "LUNC", "NEX", "XEC", "RATS", "CAT"}
# LOT_SIZE stepSize and PRICE_FILTER tickSize from exchangeInfo (2026-10-11) for the names a dry
# run can round; any other name is sent unrounded and an off-step quantity is the venue's -1111.
STEPS = {"BTCUSDT": ("0.001", "0.1"), "ETHUSDT": ("0.001", "0.01"), "SOLUSDT": ("0.01", "0.01"), "XRPUSDT": ("0.1", "0.0001"),
         "DOGEUSDT": ("1", "0.00001"), "1000PEPEUSDT": ("1", "0.0000001")}
STATUS = {"NEW": "accepted", "PARTIALLY_FILLED": "accepted", "FILLED": "filled", "CANCELED": "cancelled", "EXPIRED": "rejected",
          "REJECTED": "rejected", "EXPIRED_IN_MATCH": "rejected"}
TYPES = {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"},
                          {"name": "verifyingContract", "type": "address"}],
         "Message": [{"name": "msg", "type": "string"}]}
WEEK_US = 7 * 86_400_000_000


def _num(x: float) -> str:
    return f"{float(x):.10f}".rstrip("0").rstrip(".") or "0"


def _scrub_sig(s: str | None) -> str | None:
    return re.sub(r"signature=(?:0x)?[0-9a-fA-F]+", "signature=***", s) if s else s


class AsterExecutor(Executor):
    """The account's wallet address (`user`), the API wallet's address (`signer`, made on
    asterdex.com/en/api-wallet with perp trading on and withdrawals off) and that wallet's private
    key. The docs' recipe (futures V3 "Example of POST /fapi/v3/order" and the noop demo): the
    business parameters plus `nonce` (microseconds), `user` and `signer` are urlencoded in that
    order, that string is the `msg` of EIP-712 `Message{msg: string}` under domain
    AsterSignTransaction/1/chainId 1666 (714 on the testnet)/verifyingContract 0x0, signed by the
    signer, and `&signature=0x<65 bytes>` is appended; the signed string is the bytes sent (a POST
    or DELETE body as application/x-www-form-urlencoded, a GET query). The nonce must be within
    60 s of server time and unique per signer; the client keeps it increasing. One-way position
    mode is assumed: `reduceOnly` is sent and `positionSide` is not (hedge mode refuses
    reduceOnly, so a hedged account rejects rather than mis-sizes). A market order is MARKET
    with `quantity` only (Aster rejects `timeInForce` on it); a limit is LIMIT GTC. Quantities
    are cut to the step and prices to the tick on the passive side for the names in STEPS and
    passed through for the rest. `newClientOrderId` is `order.client_id` cut to
    `^[.A-Z:/a-z0-9_-]{1,36}$`. `fills` needs a symbol per request: without one it asks for every
    name this client has ordered in this process, over at most the last 7 days."""

    id = "aster"
    label = "Aster"
    venue = "perp"
    fields = [{"id": "user", "label": "Account wallet address (0x...)"}, {"id": "signer", "label": "API wallet address (0x...)"},
              {"id": "signer_key", "label": "API wallet private key (hex)"}]
    endpoints = (ORDER, OPEN_ORDERS, TRADES)
    HOST = "https://fapi.asterdex.com"
    TESTNET = "https://fapi.asterdex-testnet.com"
    SCALED = SCALED

    def __init__(self, gate, **kw):
        super().__init__(gate, **kw)
        self.now: float | None = None            # an injectable clock (seconds) for tests
        self.recent: set[str] = set()            # venue symbols this client has sent orders for
        self._last_nonce = 0

    @property
    def configured(self) -> bool:
        return all(len(getattr(self, f, "")) == 42 and getattr(self, f).lower().startswith("0x") for f in ("user", "signer")) and bool(self.signer_key)

    @property
    def chain_id(self) -> int:
        return 714 if self.testnet else 1666

    def nonce(self) -> int:
        """Microseconds since the epoch, strictly increasing within the process."""
        self._last_nonce = max(self._last_nonce + 1, int((time.time() if self.now is None else self.now) * 1_000_000))
        return self._last_nonce

    # Symbols ---------------------------------------------------------------------------------
    @classmethod
    def contract(cls, sym: str) -> tuple[str, float]:
        """BTC-USD -> ("BTCUSDT", 1.0); PEPE-USD -> ("1000PEPEUSDT", 1000.0)."""
        base = base_of(sym)
        return (f"1000{base}{QUOTE}", 1000.0) if base in cls.SCALED else (f"{base}{QUOTE}", 1.0)

    @staticmethod
    def canonical(symbol: str) -> tuple[str, float]:
        base, factor = unscale(str(symbol or "").upper().removesuffix(QUOTE))
        return f"{base}-USD", factor

    @staticmethod
    def quantity(venue_sym: str, qty: float) -> str:
        step = STEPS.get(venue_sym)
        if not step:
            return _num(qty)
        q = (Decimal(str(qty)) / Decimal(step[0])).to_integral_value(rounding=ROUND_DOWN) * Decimal(step[0])
        if q <= 0:
            raise ValueError(f"quantity {qty} is under the {venue_sym} step {step[0]}")
        return _num(float(q))

    @staticmethod
    def tick(venue_sym: str, px: float, side: str) -> str:
        step = STEPS.get(venue_sym)
        if not step:
            return _num(px)
        d = Decimal(str(px)) / Decimal(step[1])
        return _num(float(d.to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING) * Decimal(step[1])))

    # Signing ---------------------------------------------------------------------------------
    def payload(self, params: dict, nonce: int | None = None) -> str:
        """The exact string signed and sent: the parameters, then nonce, user and signer."""
        p = dict(params)
        p["nonce"] = str(self.nonce() if nonce is None else int(nonce))
        p["user"] = self.user
        p["signer"] = self.signer
        return urllib.parse.urlencode(p)

    def sign(self, payload: str) -> str:
        typed = {"types": TYPES, "primaryType": "Message", "message": {"msg": payload},
                 "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": self.chain_id, "verifyingContract": "0x0000000000000000000000000000000000000000"}}
        signed = Account.sign_message(encode_typed_data(full_message=typed), private_key=self.signer_key)
        return "0x" + bytes(signed.signature).hex()

    def signed(self, params: dict, nonce: int | None = None) -> str:
        payload = self.payload(params, nonce)
        return f"{payload}&signature={self.sign(payload)}"

    def order_params(self, order: Order) -> dict:
        symbol, factor = self.contract(order.symbol)
        market = order.price is None
        p = {"symbol": symbol, "side": "BUY" if order.side == "buy" else "SELL", "type": "MARKET" if market else "LIMIT"}
        if not market:
            p["timeInForce"] = "GTC"
        p["quantity"] = self.quantity(symbol, order.qty / factor)
        if not market:
            p["price"] = self.tick(symbol, order.price * factor, order.side)
        if order.reduce_only:
            p["reduceOnly"] = "true"
        cid = re.sub(r"[^.A-Z:/a-z0-9_-]", "", order.client_id or "")[:36]
        if cid:
            p["newClientOrderId"] = cid
        return p

    # Transport --------------------------------------------------------------------------------
    async def _call(self, method: str, path: str, params: dict) -> tuple[dict, dict]:
        body = self.signed(params)
        if method == "GET":
            req, ans = await self._send("GET", f"{self.host}{path}?{body}")
            req["url"] = _scrub_sig(req["url"])
        else:
            req, ans = await self._send(method, f"{self.host}{path}", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            req["content"] = _scrub_sig(req["content"])
        return req, ans

    def _result(self, req: dict, ans: dict, order_id: str = "") -> OrderResult:
        if ans.get("dry_run"):
            return OrderResult(True, self.id, order_id=order_id, status="dry-run", request=req)
        if "orderId" in ans:
            status = STATUS.get(str(ans.get("status")), "accepted")
            ok = status in ("accepted", "filled", "cancelled")
            return OrderResult(ok, self.id, order_id=str(ans["orderId"]), status=status, filled_qty=float(ans.get("executedQty") or 0),
                               avg_price=float(ans.get("avgPrice") or 0) or None, error="" if ok else str(ans.get("status")), raw=ans, request=req)
        msg = f"{ans.get('msg')} ({ans.get('code')})" if "code" in ans else f"HTTP {ans.get('_status')}: {ans.get('text', '')}"
        return OrderResult(False, self.id, order_id=order_id, status="rejected", error=msg[:160], raw=ans, request=req)

    # The contract -----------------------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        try:
            params = self.order_params(order)
        except ValueError as e:
            return OrderResult(False, self.id, status="refused", error=f"{self.label}: {e}")
        self.recent.add(params["symbol"])
        req, ans = await self._call("POST", ORDER, params)
        res = self._result(req, ans)
        factor = self.contract(order.symbol)[1]
        if res.filled_qty:
            res.filled_qty *= factor
        if res.avg_price:
            res.avg_price /= factor
        return res

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        req, ans = await self._call("DELETE", ORDER, {"symbol": self.contract(symbol)[0], "orderId": str(order_id)})
        return self._result(req, ans, order_id=order_id)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        _, ans = await self._call("GET", OPEN_ORDERS, {"symbol": self.contract(symbol)[0]} if symbol else {})
        out = []
        for o in ans.get("data") if isinstance(ans.get("data"), list) else []:
            sym, factor = self.canonical(o.get("symbol", ""))
            out.append({"order_id": str(o.get("orderId")), "symbol": sym, "side": str(o.get("side", "")).lower(), "qty": float(o.get("origQty") or 0) * factor,
                        "price": float(o.get("price") or 0) / factor, "filled": float(o.get("executedQty") or 0) * factor,
                        "status": str(o.get("status", "")).lower(), "client_id": str(o.get("clientOrderId") or "")})
        return out

    async def fills(self, since: float, symbol: str | None = None) -> list[dict]:
        start = max(int(since * 1000), (self.nonce() - WEEK_US) // 1000)
        out = []
        for venue_sym in ([self.contract(symbol)[0]] if symbol else sorted(self.recent)):
            _, ans = await self._call("GET", TRADES, {"symbol": venue_sym, "startTime": start, "limit": 1000})
            for f in ans.get("data") if isinstance(ans.get("data"), list) else []:
                sym, factor = self.canonical(f.get("symbol", ""))
                out.append({"order_id": str(f.get("orderId")), "symbol": sym, "side": str(f.get("side", "")).lower(), "qty": float(f.get("qty") or 0) * factor,
                            "price": float(f.get("price") or 0) / factor, "fee": float(f.get("commission") or 0), "t": float(f.get("time") or 0) / 1000.0})
        return out
