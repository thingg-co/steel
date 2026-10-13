"""Read-only exchange account balances for the Portfolio tab: the contract every venue follows.

The ONLY HTTP in this package goes through `Account._request`, which refuses any path the
reader did not declare in `endpoints`, and every declared endpoint is a balance, account or
position read. Nothing here creates, replaces or cancels an order, moves funds, or touches a
trading endpoint, on any venue; `tests/test_invariants.py` pins all of it (the gate, the
endpoint words, the single HTTP call site). Make the API keys read-only and nothing here can
do more. Account ids are masked before they leave this package; keys and secrets are never
logged.

A venue reader subclasses `Account`, declares its `fields` (what the Control panel asks for),
its `endpoints`, and implements `balances()` -> [{asset, qty, hold, id}] (spot) with
`kind: "perp"` rows for derivative positions ({asset, qty (signed), price (the mark),
entry, leverage}). `fetch_balances` marks every row at the live price when the name is
modeled, at the venue's mark otherwise.

A reader may also implement `fees()` -> {spot_taker_bps, spot_maker_bps, perp_taker_bps,
perp_maker_bps}: the account's actual fee tier from the venue's fee endpoint (a declared read
like the balance one), in bps per leg with a fee positive and a rebate negative, a class the
venue lacks left out, `None` when the reader is not configured. The venues return decimals
(0.001), percents ("0.26") or bps (35), and OKX a negative number for a fee: `fee_bps` and each
reader's comment say which.
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from ..config import canonical
from ..sources import STABLES, UA, base_of

CASH = {"USD", "USDT", "USDC", "USDE", "USDS", "DAI", "PYUSD", "FDUSD", "TUSD", "USD1", "UST", "USDP", "BUSD", "USDD", "EUR", "GBP"} | STABLES
FIAT_RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}       # rough marks for the little fiat a venue account holds


def mask(value) -> str:
    v = str(value or "")
    return "****" + v[-4:] if len(v) >= 4 else "****"


def fnum(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default           # NaN


def fee_bps(value, scale: float = 1e4) -> float | None:
    """A venue's fee rate to bps per leg, or None when it is missing or not a number (never 0
    for "unknown"): a decimal rate (0.001) scales by 1e4, a percent ("0.26") by 100, a figure
    already in bps (35) by 1. The sign is kept: a fee is positive, a rebate negative."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return round(v * scale, 6)


def fee_rates(spot_taker=None, spot_maker=None, perp_taker=None, perp_maker=None) -> dict:
    """The `fees()` dict from per-class bps values; a None (a class the venue does not have, or
    a rate it did not report) leaves its key out."""
    out = {}
    for k, v in (("spot_taker_bps", spot_taker), ("spot_maker_bps", spot_maker), ("perp_taker_bps", perp_taker), ("perp_maker_bps", perp_maker)):
        if v is not None:
            out[k] = float(v)
    return out


class Account:
    """One venue's read-only reader. Class attributes describe it for the Control panel
    (`KEY_SERVICES` is built from them); instances hold the credentials."""

    id = ""
    label = ""                                 # the venue, for the panel (not `name`: a key field may be called that, as Coinbase's is)
    URL = ""                                   # where to create a read-only key
    note = ""                                  # the panel's one-line explanation (say "read-only" and the venue's name for that permission)
    venue = "spot"                             # spot | perp | mixed: what the account holds
    fields: list[dict] = []                    # [{"id": "key", "label": "API key"}, {"id": "secret", "label": "API secret", "multiline": False}]
    required: tuple[str, ...] = ()             # field ids that must be present; default: every field
    endpoints: tuple[str, ...] = ()            # URL paths this reader may call; a trailing * is a prefix
    HOST = ""                                  # the API host, "https://..."; a subclass may read an env override

    def __init__(self, **creds):
        for f in self.fields:
            setattr(self, f["id"], str(creds.get(f["id"]) or "").strip())

    @classmethod
    def file(cls) -> str:
        return f"{cls.id}.json"

    @classmethod
    def describe(cls) -> dict:
        """The KEY_SERVICES entry: what the panel shows and where the key is saved."""
        return {"id": cls.id, "name": cls.label, "file": cls.file(), "kind": "json_multi", "url": cls.URL, "note": cls.note,
                "venue": cls.venue, "fields": [dict(f) for f in cls.fields]}

    def values(self) -> dict:
        return {f["id"]: getattr(self, f["id"], "") for f in self.fields}

    def configure(self, vals: dict) -> None:
        """Push new values in (empty ones keep what is there) so a key saved in the panel works
        without a restart."""
        for f in self.fields:
            v = str(vals.get(f["id"]) or "")
            if v.strip():
                setattr(self, f["id"], self._clean(f["id"], v))

    def _clean(self, field: str, v: str) -> str:
        return v.strip()

    @property
    def configured(self) -> bool:
        need = self.required or tuple(f["id"] for f in self.fields)
        return all(getattr(self, k, "") for k in need)

    def asset(self, code: str) -> str:
        """The venue's asset code to a base symbol (XXBT -> BTC on Kraken, UST -> USDT on Bitfinex)."""
        return str(code or "").upper()

    async def balances(self) -> list[dict]:
        raise NotImplementedError

    async def fees(self) -> dict | None:
        """The account's actual trading fee rates, {spot_taker_bps, spot_maker_bps, perp_taker_bps,
        perp_maker_bps} in bps per leg (a class the venue lacks is left out, never 0 for unknown),
        or None when the reader is not configured or the venue has no fee read. A reader
        overrides it with its fee endpoint, declared in `endpoints` like any other read."""
        return None

    # The gate --------------------------------------------------------------------------
    def allows(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path
        return any(path == e or (e.endswith("*") and path.startswith(e[:-1])) for e in self.endpoints)

    async def _request(self, method: str, url: str, *, params=None, data=None, json=None, headers=None, content=None,
                       timeout: float = 20.0) -> httpx.Response:
        """The one place this package talks to a venue. GET or POST to a declared read-only
        endpoint, or PermissionError before any connection is made."""
        method = str(method).upper()
        if method not in ("GET", "POST"):
            raise PermissionError(f"{self.label}: {method} is not a read")
        if not self.allows(url):
            raise PermissionError(f"{self.label}: {urllib.parse.urlsplit(url).path} is not a declared read-only endpoint")
        hdrs = {"User-Agent": UA, "Accept": "application/json"} | dict(headers or {})
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.request(method, url, params=params, data=data, json=json, headers=hdrs, content=content)

    async def _json(self, method: str, url: str, **kw):
        r = await self._request(method, url, **kw)
        if r.status_code >= 400:
            text = r.text[:120].replace("\n", " ")
            raise RuntimeError(f"HTTP {r.status_code}: {text}")
        return r.json()


def _price(prices: dict, sym: str) -> float:
    p = prices.get(sym)
    if isinstance(p, dict):
        return fnum(p.get("price"))
    return fnum(p)


async def fetch_balances(accounts, prices: dict) -> dict:
    """{t, accounts: [{id, name, venue, cash, liquidation, positions: [...]}]}, read-only. Each
    configured venue is one account; positions are marked at the live price of their BASE-USD
    pair when it is modeled, else at the venue's own mark when the reader gave one, else
    unpriced (value 0). Dollar stables and fiat are cash. Raises nothing: a venue that fails is
    reported in its own `error`."""
    out = []
    for acct in accounts or []:
        if acct is None or not acct.configured:
            continue
        try:
            rows = await acct.balances()
        except Exception as e:  # noqa: BLE001
            out.append({"id": acct.id, "name": acct.label, "venue": acct.venue, "error": f"{type(e).__name__}: {str(e)[:100]}",
                        "cash": 0.0, "liquidation": 0.0, "positions": []})
            continue
        positions, cash = [], 0.0
        for b in rows:
            asset = str(b.get("asset") or "").upper()
            qty = fnum(b.get("qty"))
            kind = b.get("kind") or "spot"
            if kind == "spot" and asset in CASH:
                cash += qty * FIAT_RATES.get(asset, 1.0)
                continue
            if not asset or qty == 0:
                continue
            sym = b.get("symbol") or canonical(asset)
            live = _price(prices, sym)
            mark = live or fnum(b.get("price"))
            value = qty * mark
            row = {"symbol": sym, "asset": asset, "kind": kind, "qty": round(qty, 8), "price": mark, "value": round(value, 2),
                   "marked": mark > 0, "hold": round(fnum(b.get("hold")), 8)}
            if kind == "perp":
                row["entry"] = fnum(b.get("entry"))
                row["leverage"] = fnum(b.get("leverage"))
                row["upnl"] = round(fnum(b.get("upnl")), 2)
            positions.append(row)
        positions.sort(key=lambda x: -abs(x["value"]))
        perps = sum(p["value"] for p in positions if p["kind"] == "perp")
        spot = sum(p["value"] for p in positions if p["kind"] != "perp")
        # a perp's notional is not equity: the margin behind it is in cash, so liquidation value is cash + spot + the venue's reported collateral
        out.append({"id": acct.id, "name": acct.label, "venue": acct.venue, "cash": round(cash, 2),
                    "liquidation": round(cash + spot, 2), "perp_notional": round(perps, 2), "positions": positions})
    out.sort(key=lambda x: -x["liquidation"])
    return {"t": time.time(), "accounts": out}


def held_symbols(portfolio: dict) -> set[str]:
    """Every modelable name an account holds (dust, shorts and stables excluded)."""
    out = set()
    for a in (portfolio or {}).get("accounts") or []:
        for p in a.get("positions") or []:
            if base_of(p["symbol"]) not in STABLES and (p.get("value") or 0) >= 10.0:
                out.add(p["symbol"])
    return out
