"""Options: Deribit's BTC and ETH chains into dealer gamma exposure by strike, the IV smile
per expiry, the term structure and max pain. Public endpoints, no key. Gamma is Black-Scholes
from each option's mark IV; GEX per option is gamma x open interest (in base units on Deribit)
x spot² x 1% with calls positive and puts negative, the usual dealer-positioning convention,
summed by strike. The zero-gamma level is where the cumulative sum crosses zero. Refreshed every
`options_minutes`."""
from __future__ import annotations

import math
import time

import httpx

REST = "https://www.deribit.com/api/v2/public"
UA = "steel/1.0"


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    return _npdf(d1) / (spot * iv * math.sqrt(t_years))


def parse_name(name: str) -> tuple[str, float, float, str] | None:
    """'BTC-27DEC26-100000-C' -> (BTC, expiry ts, strike, C|P); None when the name is not an option."""
    try:
        base, exp, strike, kind = name.split("-")
        if kind not in ("C", "P"):
            return None
        d, mon, yy = int(exp[:-5]), exp[-5:-2], int(exp[-2:])
        m = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"].index(mon) + 1
        import datetime as _dt
        ts = _dt.datetime(2000 + yy, m, d, 8, 0, tzinfo=_dt.timezone.utc).timestamp()      # Deribit expires 08:00 UTC
        return base, ts, float(strike.replace("d", ".")), kind
    except (ValueError, IndexError):
        return None


def analyze(rows: list[dict], now: float | None = None, *, max_days: float = 120.0) -> dict:
    """rows: Deribit book summaries (instrument_name, open_interest, mark_iv, underlying_price,
    mark_price, volume). Returns GEX by strike (USD per 1%), per expiry bucket, the IV smile per
    expiry, the term structure, max pain and the zero-gamma level."""
    now = time.time() if now is None else now
    spot = None
    opts = []
    for r in rows:
        p = parse_name(str(r.get("instrument_name") or ""))
        if not p:
            continue
        base, exp, strike, kind = p
        t = (exp - now) / (365.0 * 86400.0)
        if t <= 0 or t > max_days / 365.0:
            continue
        iv = float(r.get("mark_iv") or 0.0) / 100.0
        oi = float(r.get("open_interest") or 0.0)
        s = float(r.get("underlying_price") or 0.0)
        if s > 0:
            spot = s if spot is None else spot
        opts.append({"exp": exp, "t": t, "strike": strike, "kind": kind, "iv": iv, "oi": oi, "spot": s, "mark": float(r.get("mark_price") or 0.0),
                     "volume": float(r.get("volume") or 0.0)})
    if not opts or not spot:
        return {"spot": spot, "n": len(opts), "by_strike": [], "by_expiry": [], "smile": {}, "term": [], "zero_gamma": None, "max_pain": None, "total": 0.0}
    by_strike: dict[float, float] = {}
    by_exp: dict[float, dict] = {}
    smile: dict[float, dict[float, list]] = {}
    for o in opts:
        s = o["spot"] or spot
        g = bs_gamma(s, o["strike"], o["t"], o["iv"])
        gex = g * o["oi"] * s * s * 0.01 * (1 if o["kind"] == "C" else -1)
        o["gex"] = gex
        by_strike[o["strike"]] = by_strike.get(o["strike"], 0.0) + gex
        e = by_exp.setdefault(o["exp"], {"exp": o["exp"], "days": round(o["t"] * 365, 2), "gex": 0.0, "oi": 0.0, "calls": 0.0, "puts": 0.0, "atm_iv": None, "atm_gap": 1e18})
        e["gex"] += gex; e["oi"] += o["oi"]; e["calls" if o["kind"] == "C" else "puts"] += o["oi"]
        gap = abs(o["strike"] - s)
        if gap < e["atm_gap"] and o["iv"] > 0:
            e["atm_gap"], e["atm_iv"] = gap, o["iv"]
        if o["iv"] > 0:
            smile.setdefault(o["exp"], {}).setdefault(o["strike"], []).append(o["iv"])
    strikes = sorted(by_strike)
    cum, zero = 0.0, None
    prev_cum = None
    for k in strikes:
        cum += by_strike[k]
        if prev_cum is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum):
            zero = k
        prev_cum = cum
    # max pain: the expiry-agnostic strike where option holders' total intrinsic value is least
    pains = []
    for k in strikes:
        pain = 0.0
        for o in opts:
            if o["kind"] == "C":
                pain += max(0.0, k - o["strike"]) * o["oi"]
            else:
                pain += max(0.0, o["strike"] - k) * o["oi"]
        pains.append((pain, k))
    max_pain = min(pains)[1] if pains else None
    total = sum(by_strike.values())
    near = [k for k in strikes if abs(k / spot - 1) <= 0.25]
    return {"spot": spot, "n": len(opts), "total": round(total, 2),
            "by_strike": [{"strike": k, "gex": round(by_strike[k], 2)} for k in near],
            "by_expiry": [{k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in e.items() if k2 != "atm_gap"} for e in sorted(by_exp.values(), key=lambda x: x["exp"])],
            "smile": {str(int(exp)): [{"strike": k, "iv": round(sum(v) / len(v), 4)} for k, v in sorted(sm.items()) if abs(k / spot - 1) <= 0.4] for exp, sm in smile.items()},
            "term": [{"exp": e["exp"], "days": e["days"], "atm_iv": e["atm_iv"]} for e in sorted(by_exp.values(), key=lambda x: x["exp"]) if e["atm_iv"]],
            "zero_gamma": zero, "max_pain": max_pain,
            "walls": sorted(((abs(by_strike[k]), k, by_strike[k]) for k in near), reverse=True)[:6]}


async def fetch_chain(asset: str, timeout: float = 20.0) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}) as c:
        r = await c.get(f"{REST}/get_book_summary_by_currency", params={"currency": asset.upper(), "kind": "option"})
        r.raise_for_status()
        return list(r.json().get("result") or [])


class OptionsHub:
    """The latest analysis per asset, refreshed on a cadence by the engine."""

    def __init__(self, assets=("BTC", "ETH")):
        self.assets = list(assets)
        self.latest: dict[str, dict] = {}
        self.at: dict[str, float] = {}
        self.error: dict[str, str] = {}

    async def refresh(self, asset: str) -> dict | None:
        try:
            rows = await fetch_chain(asset)
            out = analyze(rows)
            out["asset"] = asset
            out["t"] = time.time()
            self.latest[asset] = out
            self.at[asset] = out["t"]
            self.error.pop(asset, None)
            return out
        except Exception as e:  # noqa: BLE001
            self.error[asset] = f"{type(e).__name__}: {str(e)[:80]}"
            return None

    def features(self, asset: str) -> dict | None:
        """What a bar carries for a BTC or ETH pair: total GEX as a share of the chain's notional and the nearest ATM IV."""
        a = self.latest.get(asset)
        if not a or not a.get("spot"):
            return None
        oi_notional = sum(e["oi"] for e in a.get("by_expiry") or []) * a["spot"] or 1.0
        term = a.get("term") or []
        return {"gex_norm": max(-1.0, min(1.0, a["total"] / oi_notional * 100.0)), "iv_atm": term[0]["atm_iv"] if term else 0.0}

    def status(self) -> dict:
        return {"assets": self.assets, "at": self.at, "errors": self.error, "n": {a: v.get("n") for a, v in self.latest.items()}}
