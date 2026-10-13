"""Exogenous signal providers: social attention, the crypto Fear & Greed index, perpetual
positioning (funding, open interest, basis, the long/short ratio), implied volatility
(Deribit's DVOL) and the whole-market scan.

These do not produce prices. Each provider returns slow-moving, bounded signals that
are fed to the model as extra per-asset features and combined into a "crowding /
euphoria" index that drives the contrarian overlay in the policy. Everything is
toggleable and degrades to neutral (zero) when a provider is off or failing.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

import httpx

from .market import MarketScanner

log = logging.getLogger(__name__)

UA = "steel/0.1 (research; https://github.com/thingg-co/steel)"


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def base_of(sym: str) -> str:
    return sym.split("-")[0].upper() if "-" in sym else sym.upper()


class SocialProvider:
    """Reddit crypto attention via ApeWisdom's crypto aggregate (r/CryptoCurrency and the
    rest): mention counts and their 24-hour momentum per name. No key. ApeWisdom carries no
    tone, so sentiment here is mention momentum, which is what retail euphoria looks like."""

    id = "social"
    name = "Reddit crypto chatter"

    def __init__(self):
        self.last: dict[str, dict] = {}
        self.status = ""

    async def gather(self, say=None) -> dict:
        say = say or (lambda *a, **k: None)
        tickers: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}, follow_redirects=True) as c:
            for page in (1, 2):
                r = await c.get(f"https://apewisdom.io/api/v1.0/filter/all-crypto/page/{page}")
                r.raise_for_status()
                d = r.json()
                for row in d.get("results", []):
                    t = str(row.get("ticker") or "").upper().removesuffix(".X")
                    if not t:
                        continue
                    m = int(row.get("mentions") or 0)
                    m0 = int(row.get("mentions_24h_ago") or 0) or m
                    tickers[t] = {"mentions": m, "mention_chg": (m - m0) / max(m0, 1),
                                  "upvotes": int(row.get("upvotes") or 0), "rank": row.get("rank"),
                                  "name": row.get("name", "")}
                if page >= int(d.get("pages") or 1):
                    break
                await asyncio.sleep(0.3)
        maxm = max((d["mentions"] for d in tickers.values()), default=1) or 1
        for d in tickers.values():
            d["attention"] = min(1.0, math.log1p(d["mentions"]) / math.log1p(maxm))
        self.last = tickers
        self.status = f"{len(tickers)} names"
        say(f"ApeWisdom: {len(tickers)} crypto names; top " +
            ", ".join(f"{t} {d['mentions']}" for t, d in sorted(tickers.items(), key=lambda kv: -kv[1]['mentions'])[:5]))
        return tickers

    def for_symbol(self, sym: str) -> dict:
        d = self.last.get(base_of(sym))
        if not d:
            return {"social_sent": 0.0, "social_attn": 0.0, "mentions": 0, "mention_chg": 0.0}
        return {"social_sent": clip(math.tanh(d["mention_chg"])), "social_attn": d["attention"],
                "mentions": d["mentions"], "mention_chg": d["mention_chg"]}


class FearGreedProvider:
    """Crypto Fear & Greed index (market-wide) from alternative.me. No key."""

    id = "feargreed"
    name = "Fear & Greed"

    def __init__(self):
        self.value = 50
        self.classification = ""
        self.status = ""

    async def gather(self, say=None) -> dict:
        say = say or (lambda *a, **k: None)
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            r = await c.get("https://api.alternative.me/fng/", params={"limit": 1})
            r.raise_for_status()
            d = r.json()["data"][0]
        self.value = int(d["value"])
        self.classification = d["value_classification"]
        self.status = f"{self.value} ({self.classification})"
        say(f"Fear & Greed: {self.value} ({self.classification})")
        return {"value": self.value, "class": self.classification, "norm": clip((self.value - 50) / 50.0)}


class DerivativesProvider:
    """Perp positioning per name from two venues, no key: Bybit's public v5 API (funding, open
    interest and its change, the mark/index basis, one call for every USDT contract, and the
    retail long/short account ratio, one call per name, paced) and Hyperliquid's asset contexts
    (funding, open interest, the mark/oracle premium, one call for every perp). A name on both
    averages the two funding rates and basis and sums the open interest; a name on one takes
    that one (Hyperliquid lists what Bybit does not, and the reverse). Hyperliquid's funding is
    hourly: it is scaled to Bybit's 8-hour interval so the paper book, which accrues the rate per
    `funding_interval_hours`, sees one unit. The funding rates also feed the paper book."""

    id = "derivatives"
    name = "Perp positioning (Bybit + Hyperliquid)"
    REST = "https://api.bybit.com"
    HL = "https://api.hyperliquid.xyz/info"
    HL_HOURS = 1.0                          # Hyperliquid pays funding every hour; Bybit every 8

    def __init__(self, ratio_names: int = 80):
        self.prev_oi: dict[str, float] = {}
        self.last: dict[str, dict] = {}
        self.ratio: dict[str, float] = {}
        self.ratio_names = int(ratio_names)
        self.status = ""

    @staticmethod
    def pair(sym: str) -> str:
        base = base_of(sym)
        return {"SHIB": "1000SHIBUSDT", "PEPE": "1000PEPEUSDT", "BONK": "1000BONKUSDT", "FLOKI": "1000FLOKIUSDT"}.get(base, base + "USDT")

    async def gather(self, symbols, say=None) -> dict:
        say = say or (lambda *a, **k: None)
        pairs = {self.pair(s): s for s in symbols}
        out: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{self.REST}/v5/market/tickers", params={"category": "linear"})
            r.raise_for_status()
            rows = (r.json().get("result") or {}).get("list") or []
            for row in rows:
                sym = pairs.get(row.get("symbol"))
                if not sym:
                    continue
                try:
                    funding = float(row.get("fundingRate") or 0.0)
                    mark, index = float(row.get("markPrice") or 0), float(row.get("indexPrice") or 0)
                    oiv = float(row.get("openInterestValue") or 0.0)
                except (TypeError, ValueError):
                    continue
                basis = (mark / index - 1.0) if mark > 0 and index > 0 else 0.0
                prev = self.prev_oi.get(sym, oiv)
                oi_chg = (oiv - prev) / prev if prev else 0.0
                self.prev_oi[sym] = oiv
                out[sym] = {"funding": funding, "funding_sig": clip(math.tanh(funding * 2000)),
                            "oi": oiv, "oi_chg": clip(oi_chg, -1, 1), "oi_chg_sig": clip(math.tanh(oi_chg * 5)),
                            "basis": basis, "basis_sig": clip(math.tanh(basis * 1000)),
                            "next_funding": row.get("nextFundingTime"),
                            "longshort": self.ratio.get(sym, 1.0), "longshort_sig": 0.0}
            # the account ratio is one request per name: the first `ratio_names` of the universe each pass
            for sym in [s for s in symbols if s in out][:self.ratio_names]:
                try:
                    lr = await c.get(f"{self.REST}/v5/market/account-ratio",
                                     params={"category": "linear", "symbol": self.pair(sym), "period": "5min", "limit": 1})
                    lst = ((lr.json().get("result") or {}).get("list") or []) if lr.status_code == 200 else []
                    if lst:
                        buy, sell = float(lst[0].get("buyRatio") or 0.5), float(lst[0].get("sellRatio") or 0.5)
                        self.ratio[sym] = buy / sell if sell > 0 else 1.0
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.08)
            n_bybit = len(out)
            try:
                n_hl = await self._hyperliquid(c, symbols, out)
            except Exception as e:  # noqa: BLE001
                n_hl = 0
                self.status = f"Hyperliquid: {type(e).__name__}"
        for sym, d in out.items():
            lsr = self.ratio.get(sym, 1.0)
            d["longshort"] = lsr
            d["longshort_sig"] = clip(math.tanh(math.log(max(lsr, 1e-3))))
        self.last = out
        self.status = f"{len(out)} perps ({n_bybit} Bybit, {n_hl} Hyperliquid)" if out else "no data"
        if out:
            say("perps: " + ", ".join(
                f"{base_of(s)} fund {d['funding'] * 100:+.3f}% OI {d['oi_chg'] * 100:+.1f}% L/S {d['longshort']:.2f}"
                for s, d in list(out.items())[:8]))
        return out

    @staticmethod
    def hl_coin(sym: str) -> str:
        base = base_of(sym)
        return {"SHIB": "kSHIB", "PEPE": "kPEPE", "BONK": "kBONK", "FLOKI": "kFLOKI", "LUNC": "kLUNC", "DOGS": "kDOGS", "NEIRO": "kNEIRO"}.get(base, base)

    async def _hyperliquid(self, c, symbols, out: dict) -> int:
        """Merge Hyperliquid's perp contexts into `out` (see the class docstring). Returns the
        number of names it reported."""
        r = await c.post(self.HL, json={"type": "metaAndAssetCtxs"})
        if r.status_code != 200:
            return 0
        d = r.json()
        universe = (d[0] or {}).get("universe") or []
        ctxs = d[1] or []
        coins = {self.hl_coin(s): s for s in symbols}
        n = 0
        for meta, ctx in zip(universe, ctxs):
            sym = coins.get(str(meta.get("name") or ""))
            if not sym or meta.get("isDelisted"):
                continue
            try:
                funding = float(ctx.get("funding") or 0.0) * (8.0 / self.HL_HOURS)
                mark, oracle = float(ctx.get("markPx") or 0), float(ctx.get("oraclePx") or 0)
                oiv = float(ctx.get("openInterest") or 0.0) * mark
            except (TypeError, ValueError):
                continue
            basis = (mark / oracle - 1.0) if mark > 0 and oracle > 0 else 0.0
            n += 1
            if sym in out:                                   # both venues: average the rates, sum the interest
                cur = out[sym]
                cur["funding"] = (cur["funding"] + funding) / 2.0
                cur["basis"] = (cur["basis"] + basis) / 2.0
                cur["oi"] = cur.get("oi", 0.0) + oiv
                cur["funding_sig"] = clip(math.tanh(cur["funding"] * 2000))
                cur["basis_sig"] = clip(math.tanh(cur["basis"] * 1000))
                cur["venues"] = 2
                continue
            key = f"hl:{sym}"
            prev = self.prev_oi.get(key, oiv)
            oi_chg = (oiv - prev) / prev if prev else 0.0
            self.prev_oi[key] = oiv
            out[sym] = {"funding": funding, "funding_sig": clip(math.tanh(funding * 2000)),
                        "oi": oiv, "oi_chg": clip(oi_chg, -1, 1), "oi_chg_sig": clip(math.tanh(oi_chg * 5)),
                        "basis": basis, "basis_sig": clip(math.tanh(basis * 1000)), "next_funding": None,
                        "longshort": 1.0, "longshort_sig": 0.0, "venues": 1}
        return n

    def rates(self) -> dict[str, float]:
        """The current per-interval funding rate per symbol, for the paper book."""
        return {s: d["funding"] for s, d in self.last.items()}


class VolProvider:
    """Implied volatility from Deribit's DVOL indices (BTC and ETH, annualized percent): the
    crypto VIX. Names other than ETH read BTC's. No key."""

    id = "vol"
    name = "Implied vol (Deribit DVOL)"
    REST = "https://www.deribit.com/api/v2/public"

    def __init__(self):
        self.dvol: dict[str, float] = {}
        self.status = ""

    async def gather(self, say=None) -> dict:
        say = say or (lambda *a, **k: None)
        now = int(time.time() * 1000)
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            for cur in ("BTC", "ETH"):
                r = await c.get(f"{self.REST}/get_volatility_index_data",
                                params={"currency": cur, "resolution": 3600, "start_timestamp": now - 4 * 3600 * 1000, "end_timestamp": now})
                if r.status_code != 200:
                    continue
                data = (r.json().get("result") or {}).get("data") or []
                if data:
                    self.dvol[cur] = float(data[-1][4])          # [ts, open, high, low, close]
                await asyncio.sleep(0.2)
        self.status = ", ".join(f"{k} {v:.0f}" for k, v in self.dvol.items()) or "no data"
        if self.dvol:
            say("DVOL: " + self.status)
        return dict(self.dvol)

    def for_symbol(self, sym: str) -> float | None:
        base = base_of(sym)
        return self.dvol.get("ETH" if base in ("ETH", "STETH", "CBETH", "LSETH") else "BTC")

    @staticmethod
    def sig(dvol: float | None) -> float:
        """DVOL sits around 35 (calm) to 110 (panic); 55 is the middle of the road."""
        return clip((float(dvol) - 55.0) / 35.0) if dvol else 0.0


class SignalHub:
    """Runs the toggleable signal providers, assembling per-asset features plus a crowding index."""

    def __init__(self, cfg, symbols):
        self.cfg = cfg
        self.symbols = symbols
        self.providers = {p.id: p for p in (SocialProvider(), FearGreedProvider(), DerivativesProvider(), VolProvider())}
        off = {x.strip() for x in cfg.signals_off.split(",") if x.strip()}
        self.enabled = {pid: pid not in off for pid in self.providers}
        self.market = MarketScanner(getattr(cfg, "coingecko_key", ""), getattr(cfg, "radar_top", 250), getattr(cfg, "state_dir", ""))
        self.enabled["market"] = "market" not in off
        self.state = {"per_asset": {}, "market": {}, "crowding": {}, "funding": {}, "fear_greed": {}, "dvol": {},
                      "providers": self.status(), "t": None}

    def toggle(self, pid: str, on: bool) -> dict:
        if pid not in self.enabled:
            return {"error": "unknown signal provider"}
        self.enabled[pid] = on
        return {"ok": True}

    def status(self) -> list[dict]:
        out = [{"id": p.id, "name": p.name, "enabled": self.enabled.get(p.id, False),
                "status": getattr(p, "status", "")} for p in self.providers.values()]
        out.append({"id": "market", "name": "Whole-market scan", "enabled": self.enabled.get("market", False),
                    "status": self.market.status})
        return out

    def funding_rates(self) -> dict[str, float]:
        return self.providers["derivatives"].rates() if self.enabled.get("derivatives") else {}

    async def gather(self, say=None) -> dict:
        say = say or (lambda *a, **k: None)
        social, fng, deriv, vol = (self.providers[k] for k in ("social", "feargreed", "derivatives", "vol"))
        market = {"fng": 0.0, "fng_value": None, "fng_class": None}
        if self.enabled["social"]:
            try:
                await social.gather(say)
            except Exception as e:  # noqa: BLE001
                say(f"social failed: {type(e).__name__}", "warn")
        if self.enabled["feargreed"]:
            try:
                fg = await fng.gather(say)
                market = {"fng": fg["norm"], "fng_value": fg["value"], "fng_class": fg["class"]}
            except Exception as e:  # noqa: BLE001
                say(f"Fear & Greed failed: {type(e).__name__}", "warn")
        if self.enabled["derivatives"]:
            try:
                await deriv.gather(self.symbols, say)
            except Exception as e:  # noqa: BLE001
                say(f"derivatives failed: {type(e).__name__}", "warn")
        if self.enabled["vol"]:
            try:
                await vol.gather(say)
            except Exception as e:  # noqa: BLE001
                say(f"DVOL failed: {type(e).__name__}", "warn")
        market_feat = {"breadth": 0.0, "btc_dom": 0.0, "stable_flow": 0.0}
        if self.enabled.get("market"):
            try:
                market_feat = (await self.market.scan(say)).get("feat", market_feat)
            except Exception as e:  # noqa: BLE001
                say(f"market scan failed: {type(e).__name__}", "warn")

        # enrich the movers radar with our own signals
        radar = (self.market.state or {}).get("radar", [])
        social_last = social.last if self.enabled.get("social") else {}
        deriv_last = deriv.last if self.enabled.get("derivatives") else {}
        for row in radar:
            row["social"] = social_last.get(row.get("base") or "", {}).get("mentions", 0) if social_last else 0
            d = deriv_last.get(row["symbol"])
            row["funding"] = d["funding"] if d else None

        per_asset, crowding = {}, {}
        for sym in self.symbols:
            w = social.for_symbol(sym) if self.enabled["social"] else {"social_sent": 0.0, "social_attn": 0.0, "mentions": 0, "mention_chg": 0.0}
            d = deriv_last.get(sym, {})
            funding_sig, oi_sig, ls_sig, basis_sig = (d.get("funding_sig", 0.0), d.get("oi_chg_sig", 0.0),
                                                      d.get("longshort_sig", 0.0), d.get("basis_sig", 0.0))
            dv = vol.for_symbol(sym) if self.enabled["vol"] else None
            crowd = (0.9 * market["fng"] + 1.1 * w["social_attn"] * (0.3 + 0.7 * clip(w["social_sent"] + 0.15)) +
                     1.0 * funding_sig + 0.7 * ls_sig + 0.4 * oi_sig + 0.5 * basis_sig)
            crowding[sym] = round(clip(crowd / 3.6), 3)
            feats = {"social_sent": w["social_sent"], "social_attn": w["social_attn"], "fng": market["fng"],
                     "funding": funding_sig, "oi_chg": oi_sig, "longshort": ls_sig, "basis": basis_sig,
                     "dvol": vol.sig(dv), "btc_dom": market_feat["btc_dom"], "breadth": market_feat["breadth"],
                     "stable_flow": market_feat["stable_flow"]}
            per_asset[sym] = {**feats, "mentions": w["mentions"], "mention_chg": w.get("mention_chg", 0.0),
                              "funding_rate": d.get("funding"), "oi": d.get("oi"), "oi_chg_raw": d.get("oi_chg"),
                              "longshort_raw": d.get("longshort"), "basis_raw": d.get("basis"), "dvol_raw": dv,
                              "feat": feats}
        self.state = {"per_asset": per_asset, "market": self.market.state,
                      "fear_greed": {"value": market.get("fng_value"), "class": market.get("fng_class"), "norm": market["fng"]},
                      "dvol": dict(vol.dvol) if self.enabled["vol"] else {},
                      "crowding": crowding, "funding": self.funding_rates(),
                      "providers": self.status(), "t": time.time()}
        return self.state
