"""Execution: the one place Steel may send an order, and the gate every order passes.

This package is off unless `STEEL_EXECUTION=1`, and even then a venue trades only when it is
armed (`execution_venues`) with a trade-permission key of its own (`<venue>-trade.json`, never
the read-only key). Every order goes through `ExecutionGate.check`: the kill switch (a flag,
the dashboard button, or the file state/KILL), the per-order, per-name, per-venue and per-UTC-day
notional caps, the open-exposure and leverage caps, and a cooldown after a venue rejects an
order. A cap left at zero refuses everything: nothing executes until the operator sets it.
Every request and every answer is appended to state/orders.jsonl before and after it is sent.
Clients default to `dry_run`, where they build and sign the request and log it without sending.

A venue client subclasses `Executor`: it declares `id`, `venue` ("spot" or "perp"), `fields`
(its trade key's fields), `endpoints` (the order paths it calls, which only this package may
hold), and implements `_place`, `_cancel`, `_open_orders` and `_fills`. The policy never calls
this directly: tickets come from the operator (POST /api/execute) or, once the paper-to-live
rule exists, from the live book.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from ..config import read_creds
from ..sources import UA, base_of

KILL_FILE = "KILL"


@dataclass
class Order:
    venue: str
    symbol: str                 # canonical BASE-USD
    side: str                   # "buy" | "sell"
    qty: float                  # base units
    price: float | None = None  # None = market
    kind: str = "perp"          # "spot" | "perp"
    reduce_only: bool = False
    notional_usd: float = 0.0   # qty x reference price, for the caps
    client_id: str = ""
    note: str = ""


@dataclass
class OrderResult:
    ok: bool
    venue: str
    order_id: str = ""
    status: str = ""            # accepted | filled | rejected | dry-run | refused
    filled_qty: float = 0.0
    avg_price: float | None = None
    error: str = ""
    raw: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)   # what was (or would have been) sent, secrets out
    t: float = 0.0


class ExecutionGate:
    """The rules every order must clear. Stateful: it keeps the day's and each venue's spent
    notional, the open exposure the live book reports, and the reject cooldowns."""

    def __init__(self, cfg, state_dir: str | Path = "state"):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.killed = False
        self.kill_reason = ""
        self.spent_day: dict[str, float] = {}             # UTC day -> notional sent
        self.spent_venue: dict[tuple[str, str], float] = {}   # (day, venue) -> notional
        self.spent_name: dict[tuple[str, str], float] = {}    # (day, symbol) -> notional
        self.exposure_usd = 0.0                           # the live book's open notional, set by its owner
        self.equity_usd = 0.0
        self.rejects: dict[str, float] = {}               # venue -> time of the last reject
        self.orders_path = self.state_dir / "orders.jsonl"
        self.clock: dict = {"offset_s": None, "venues": {}, "checked": 0.0}   # the box against the venues' clocks (execution/clock.py)

    # State --------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "execution", False))

    @property
    def armed(self) -> set[str]:
        return {v.strip().lower() for v in str(getattr(self.cfg, "execution_venues", "") or "").split(",") if v.strip()}

    def kill_file(self) -> bool:
        return (self.state_dir / KILL_FILE).exists()

    def kill(self, reason: str = "operator") -> None:
        self.killed, self.kill_reason = True, reason
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / KILL_FILE).write_text(f"{time.time():.0f} {reason}\n")
        except OSError:
            pass

    def revive(self) -> None:
        """Lift the kill switch (the operator, deliberately); the file goes too."""
        self.killed, self.kill_reason = False, ""
        try:
            (self.state_dir / KILL_FILE).unlink()
        except OSError:
            pass

    def caps(self) -> dict:
        c = self.cfg
        return {"order_usd": float(getattr(c, "exec_max_order_usd", 0.0) or 0.0),
                "name_usd": float(getattr(c, "exec_max_name_usd", 0.0) or 0.0),
                "venue_usd": float(getattr(c, "exec_max_venue_usd", 0.0) or 0.0),
                "day_usd": float(getattr(c, "exec_max_day_usd", 0.0) or 0.0),
                "exposure_frac": float(getattr(c, "exec_max_exposure_frac", 0.0) or 0.0),
                "leverage": float(getattr(c, "exec_max_leverage", 0.0) or 0.0),
                "reject_cooldown_s": float(getattr(c, "exec_reject_cooldown_s", 300.0) or 0.0)}

    @staticmethod
    def day(ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))

    def check(self, order: Order, now: float | None = None, leverage: float = 1.0) -> str:
        """Why the order may not go, or "" when it may. Every rule is checked; the first
        failure is returned."""
        now = time.time() if now is None else now
        caps = self.caps()
        if not self.enabled:
            return "execution is off (STEEL_EXECUTION=1 turns it on)"
        if self.killed or self.kill_file():
            return f"kill switch is on ({self.kill_reason or 'state/KILL exists'})"
        if order.venue not in self.armed:
            return f"{order.venue} is not armed (execution_venues)"
        off, tol = self.clock.get("offset_s"), float(getattr(self.cfg, "clock_max_offset_s", 1.0) or 0.0)
        if off is not None and tol > 0 and abs(float(off)) > tol:
            return f"the box clock is {float(off):+.2f} s off the venues' (limit {tol:g} s); fix the clock (chrony/ntp)"
        if order.side not in ("buy", "sell") or order.qty <= 0:
            return "malformed order"
        if order.notional_usd <= 0:
            return "no reference price for the notional"
        for key, label in (("order_usd", "per-order"), ("name_usd", "per-name"), ("venue_usd", "per-venue"), ("day_usd", "per-day")):
            if caps[key] <= 0:
                return f"the {label} cap is unset (nothing executes until every cap is set)"
        if order.notional_usd > caps["order_usd"]:
            return f"order {order.notional_usd:,.0f} USD is over the per-order cap {caps['order_usd']:,.0f}"
        d = self.day(now)
        if self.spent_name.get((d, order.symbol), 0.0) + order.notional_usd > caps["name_usd"]:
            return f"{base_of(order.symbol)} would pass its daily cap {caps['name_usd']:,.0f} USD"
        if self.spent_venue.get((d, order.venue), 0.0) + order.notional_usd > caps["venue_usd"]:
            return f"{order.venue} would pass its daily cap {caps['venue_usd']:,.0f} USD"
        if self.spent_day.get(d, 0.0) + order.notional_usd > caps["day_usd"]:
            return f"the day's cap {caps['day_usd']:,.0f} USD would be passed"
        if caps["exposure_frac"] > 0 and self.equity_usd > 0 and not order.reduce_only:
            if self.exposure_usd + order.notional_usd > caps["exposure_frac"] * self.equity_usd:
                return f"open exposure would pass {caps['exposure_frac'] * 100:.0f}% of equity"
        if caps["leverage"] > 0 and leverage > caps["leverage"]:
            return f"leverage {leverage:g}x is over the cap {caps['leverage']:g}x"
        last = self.rejects.get(order.venue)
        if last is not None and caps["reject_cooldown_s"] > 0 and now - last < caps["reject_cooldown_s"]:
            return f"{order.venue} rejected an order {now - last:.0f} s ago; cooling down"
        return ""

    def spent(self, order: Order, now: float | None = None) -> None:
        d = self.day(now)
        self.spent_day[d] = self.spent_day.get(d, 0.0) + order.notional_usd
        self.spent_venue[(d, order.venue)] = self.spent_venue.get((d, order.venue), 0.0) + order.notional_usd
        self.spent_name[(d, order.symbol)] = self.spent_name.get((d, order.symbol), 0.0) + order.notional_usd

    def audit(self, event: str, order: Order | None = None, result: OrderResult | None = None, **extra) -> None:
        rec = {"t": time.time(), "event": event, **extra}
        if order is not None:
            rec["order"] = asdict(order)
        if result is not None:
            rec["result"] = {k: v for k, v in asdict(result).items() if k != "raw"} | {"raw": str(result.raw)[:400]}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.orders_path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def status(self) -> dict:
        d = self.day()
        return {"enabled": self.enabled, "armed": sorted(self.armed), "killed": bool(self.killed or self.kill_file()),
                "kill_reason": self.kill_reason or ("state/KILL exists" if self.kill_file() else ""), "caps": self.caps(),
                "spent_today": round(self.spent_day.get(d, 0.0), 2),
                "spent_by_venue": {v: round(x, 2) for (dd, v), x in self.spent_venue.items() if dd == d},
                "exposure_usd": round(self.exposure_usd, 2), "equity_usd": round(self.equity_usd, 2),
                "cooling": {v: round(t, 0) for v, t in self.rejects.items()},
                "clock": {**self.clock, "ok": self.clock.get("offset_s") is None or abs(float(self.clock["offset_s"])) <= float(getattr(self.cfg, "clock_max_offset_s", 1.0) or 1e9),
                          "limit_s": float(getattr(self.cfg, "clock_max_offset_s", 1.0) or 0.0)}}


class Executor:
    """One venue's order client. Secrets never leave the instance; `request` in a result
    carries the request with its headers stripped."""

    id = ""
    label = ""
    venue = "perp"                       # what it trades
    fields: list[dict] = []              # the trade key's fields, like an Account's
    endpoints: tuple[str, ...] = ()      # the order paths this client calls; only steel/execution may hold these
    HOST = ""
    TESTNET = ""                         # the venue's testnet host, when it has one
    SECRET_HEADERS = ("api-key", "api-secret", "x-mbx-apikey", "authorization", "ok-access-key", "ok-access-sign", "ok-access-passphrase",
                      "x-bapi-api-key", "x-bapi-sign", "access-key", "access-sign", "access-passphrase", "kc-api-key", "kc-api-sign",
                      "kc-api-passphrase", "key", "sign", "api-sign", "apikey", "authent", "bfx-apikey", "bfx-signature", "x-gemini-apikey",
                      "x-gemini-payload", "x-gemini-signature", "x-auth", "x-auth-signature", "access-nonce")

    def __init__(self, gate: ExecutionGate, *, dry_run: bool = True, testnet: bool = False, **creds):
        self.gate = gate
        self.dry_run = bool(dry_run)
        self.testnet = bool(testnet)
        for f in self.fields:
            setattr(self, f["id"], str(creds.get(f["id"]) or "").strip())

    @classmethod
    def file(cls) -> str:
        return f"{cls.id}-trade.json"

    @classmethod
    def load(cls, gate: ExecutionGate, *, dry_run: bool = True, testnet: bool = False) -> "Executor":
        return cls(gate, dry_run=dry_run, testnet=testnet, **read_creds(f"{cls.id}-trade", [f["id"] for f in cls.fields]))

    @property
    def configured(self) -> bool:
        return all(getattr(self, f["id"], "") for f in self.fields)

    @property
    def host(self) -> str:
        return self.TESTNET if self.testnet and self.TESTNET else self.HOST

    def allows(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path
        return any(path == e or (e.endswith("*") and path.startswith(e[:-1])) for e in self.endpoints)

    def _scrub(self, headers: dict | None) -> dict:
        return {k: ("***" if k.lower() in self.SECRET_HEADERS else v) for k, v in (headers or {}).items()}

    async def _send(self, method: str, url: str, *, params=None, data=None, json_body=None, headers=None, content=None,
                    timeout: float = 15.0) -> tuple[dict, dict]:
        """The only HTTP in the package: (the request as logged, the venue's answer). In dry-run
        the request is logged and nothing is sent."""
        if not self.allows(url):
            raise PermissionError(f"{self.label}: {urllib.parse.urlsplit(url).path} is not a declared order endpoint")
        req = {"method": method.upper(), "url": url, "params": params, "data": data, "json": json_body,
               "content": content if isinstance(content, str) else None, "headers": self._scrub(headers)}
        if self.dry_run:
            return req, {"dry_run": True}
        hdrs = {"User-Agent": UA, "Accept": "application/json"} | dict(headers or {})
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method.upper(), url, params=params, data=data, json=json_body, headers=hdrs, content=content)
        try:
            body = r.json()
        except ValueError:
            body = {"text": r.text[:400]}
        if not isinstance(body, dict):
            body = {"data": body}
        body["_status"] = r.status_code
        return req, body

    async def place(self, order: Order, *, leverage: float = 1.0) -> OrderResult:
        """The gate, the audit log, the venue. Never raises: a refusal or a failure is a result."""
        why = self.gate.check(order, leverage=leverage)
        if why:
            res = OrderResult(False, self.id, status="refused", error=why, t=time.time())
            self.gate.audit("refused", order, res)
            return res
        if not self.configured:
            res = OrderResult(False, self.id, status="refused", error=f"{self.label}: no trade key ({self.file()})", t=time.time())
            self.gate.audit("refused", order, res)
            return res
        self.gate.audit("sending", order, dry_run=self.dry_run, testnet=self.testnet)
        try:
            res = await self._place(order)
        except Exception as e:  # noqa: BLE001
            res = OrderResult(False, self.id, status="failed", error=f"{type(e).__name__}: {str(e)[:160]}", t=time.time())
        res.t = res.t or time.time()
        if res.ok or res.status == "dry-run":
            self.gate.spent(order)
        elif res.status in ("rejected", "failed"):
            self.gate.rejects[self.id] = time.time()
        self.gate.audit("result", order, res)
        return res

    async def cancel(self, order_id: str, symbol: str) -> OrderResult:
        self.gate.audit("cancel", extra={"order_id": order_id, "symbol": symbol, "venue": self.id}) if False else self.gate.audit("cancel", order_id=order_id, symbol=symbol, venue=self.id)
        try:
            res = await self._cancel(order_id, symbol)
        except Exception as e:  # noqa: BLE001
            res = OrderResult(False, self.id, order_id=order_id, status="failed", error=f"{type(e).__name__}: {str(e)[:160]}")
        self.gate.audit("cancel-result", result=res)
        return res

    # The venue's part -----------------------------------------------------------------
    async def _place(self, order: Order) -> OrderResult:
        raise NotImplementedError

    async def _cancel(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return []

    async def fills(self, since: float) -> list[dict]:
        return []

    @classmethod
    def describe(cls) -> dict:
        return {"id": cls.id, "name": cls.label, "file": cls.file(), "kind": "json_multi", "venue": cls.venue,
                "fields": [dict(f) for f in cls.fields], "testnet": bool(cls.TESTNET)}
