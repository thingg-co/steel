"""Whole-market scanner: breadth, category rotation, dominance, stablecoin flows and the
market-wide movers radar.

The net forecasts a focused tradeable universe, but the market it trades in is much bigger.
This module surveys that broader market on a slow cadence and produces (a) a human-readable
overview and (b) three market-wide features (breadth, BTC dominance, stablecoin supply flow)
shared across every tracked asset to condition the regime gate. CoinGecko's public tier
serves the top 250 names in one call (about 30 calls a minute across the app); DefiLlama
serves stablecoin supply. No key is needed; a CoinGecko demo key raises the limit.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import httpx

UA = "steel/0.1 (research; https://github.com/thingg-co/steel)"
CG = "https://api.coingecko.com/api/v3"


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# A category per base for the names we expect to see, so the treemap groups them without an API
# call each; anything else is looked up (CoinGecko's categories folded by SECTOR_OF) and persisted.
SECTOR_MAP = {
    "BTC": "Store of value", "LTC": "Store of value", "BCH": "Store of value", "XMR": "Privacy", "ZEC": "Privacy", "DASH": "Privacy",
    "ETH": "Smart contract L1", "SOL": "Smart contract L1", "ADA": "Smart contract L1", "AVAX": "Smart contract L1", "DOT": "Smart contract L1",
    "ATOM": "Smart contract L1", "NEAR": "Smart contract L1", "SUI": "Smart contract L1", "APT": "Smart contract L1", "TON": "Smart contract L1",
    "ICP": "Smart contract L1", "HBAR": "Smart contract L1", "XLM": "Payments", "XRP": "Payments", "ALGO": "Smart contract L1", "VET": "Smart contract L1",
    "TRX": "Smart contract L1", "SEI": "Smart contract L1", "INJ": "Smart contract L1", "TIA": "Modular / infra", "ETC": "Smart contract L1",
    "EGLD": "Smart contract L1", "FLOW": "Smart contract L1", "KAVA": "Smart contract L1", "XTZ": "Smart contract L1", "MINA": "Smart contract L1",
    "ARB": "Layer 2", "OP": "Layer 2", "POL": "Layer 2", "STRK": "Layer 2", "ZK": "Layer 2", "IMX": "Layer 2", "MANTLE": "Layer 2", "STX": "Layer 2",
    "UNI": "DeFi", "AAVE": "DeFi", "LDO": "DeFi", "CRV": "DeFi", "MKR": "DeFi", "SKY": "DeFi", "COMP": "DeFi", "SNX": "DeFi", "PENDLE": "DeFi",
    "ENA": "DeFi", "MORPHO": "DeFi", "JTO": "DeFi", "RAY": "DeFi", "JUP": "DeFi", "CAKE": "DeFi", "SUSHI": "DeFi", "1INCH": "DeFi", "EIGEN": "DeFi",
    "ETHFI": "DeFi", "ONDO": "RWA", "HYPE": "Perp DEX", "GMX": "Perp DEX", "DYDX": "Perp DEX", "ASTER": "Perp DEX",
    "LINK": "Oracle / infra", "PYTH": "Oracle / infra", "GRT": "Oracle / infra", "FIL": "Storage / infra", "AR": "Storage / infra", "RENDER": "AI / compute",
    "FET": "AI / compute", "TAO": "AI / compute", "WLD": "AI / compute", "VIRTUAL": "AI / compute", "AKT": "AI / compute",
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme", "WIF": "Meme", "FLOKI": "Meme", "TRUMP": "Meme", "PENGU": "Meme", "FARTCOIN": "Meme", "PUMP": "Meme",
    "BNB": "Exchange token", "CRO": "Exchange token", "OKB": "Exchange token", "KCS": "Exchange token",
    "SAND": "Gaming / metaverse", "MANA": "Gaming / metaverse", "AXS": "Gaming / metaverse", "GALA": "Gaming / metaverse", "APE": "Gaming / metaverse",
    "ENS": "Identity / social", "QNT": "Enterprise", "USDT": "Stablecoin", "USDC": "Stablecoin", "DAI": "Stablecoin", "USDS": "Stablecoin", "USDE": "Stablecoin",
    "PAXG": "Tokenized gold", "XAUT": "Tokenized gold", "WBTC": "Store of value", "STETH": "DeFi", "LEO": "Exchange token", "BGB": "Exchange token",
}
# CoinGecko category names folded into the treemap's groups
SECTOR_OF = [
    ("meme", "Meme"), ("stablecoin", "Stablecoin"), ("layer 2", "Layer 2"), ("rollup", "Layer 2"), ("exchange-based", "Exchange token"),
    ("centralized exchange", "Exchange token"), ("decentralized exchange", "DeFi"), ("perpetual", "Perp DEX"), ("liquid staking", "DeFi"),
    ("lending", "DeFi"), ("yield", "DeFi"), ("defi", "DeFi"), ("real world assets", "RWA"), ("rwa", "RWA"), ("artificial intelligence", "AI / compute"),
    ("ai agents", "AI / compute"), ("depin", "AI / compute"), ("oracle", "Oracle / infra"), ("storage", "Storage / infra"), ("gaming", "Gaming / metaverse"),
    ("metaverse", "Gaming / metaverse"), ("nft", "Gaming / metaverse"), ("privacy", "Privacy"), ("payment", "Payments"), ("smart contract platform", "Smart contract L1"),
    ("layer 1", "Smart contract L1"), ("proof of work", "Store of value"), ("tokenized gold", "Tokenized gold"), ("identity", "Identity / social"),
    ("social", "Identity / social"), ("interoperability", "Modular / infra"), ("modular", "Modular / infra"), ("infrastructure", "Modular / infra"),
]


def sector_from_categories(cats: list[str]) -> str:
    low = [str(c).lower() for c in cats or []]
    for needle, sector in SECTOR_OF:
        if any(needle in c for c in low):
            return sector
    return "Other"


class MarketScanner:
    def __init__(self, coingecko_key: str = "", radar_top: int = 250, state_dir: str = ""):
        self.key = coingecko_key
        self.radar_top = int(radar_top)
        self.state: dict = {}
        self.status = ""
        self.sector_file = os.path.join(state_dir, "sectors.json") if state_dir else ""
        self.sectors: dict = {}                       # base -> sector (persisted; a listing's category rarely changes)
        self.ids: dict[str, str] = {}                 # base -> coingecko id, from the last markets page
        try:
            self.sectors = json.loads(open(self.sector_file).read()) if self.sector_file else {}
        except (OSError, ValueError):
            self.sectors = {}

    def _headers(self) -> dict:
        h = {"User-Agent": UA}
        if self.key:
            h["x-cg-demo-api-key"] = self.key
        return h

    def sector_of(self, base: str) -> str | None:
        return SECTOR_MAP.get(base) or self.sectors.get(base)

    async def _fill_sectors(self, c, bases: list[str], budget: int = 8) -> None:
        """Look up the categories of radar names we have not placed, a few per scan (the free tier
        is tight), and persist. Names CoinGecko cannot describe get "Other" so they are not retried."""
        todo = [b for b in bases if b not in SECTOR_MAP and b not in self.sectors and b in self.ids][:budget]
        for b in todo:
            try:
                r = await c.get(f"{CG}/coins/{self.ids[b]}", headers=self._headers(),
                                params={"localization": "false", "tickers": "false", "market_data": "false",
                                        "community_data": "false", "developer_data": "false", "sparkline": "false"})
                if r.status_code == 429:
                    break
                self.sectors[b] = sector_from_categories(r.json().get("categories") or []) if r.status_code == 200 else "Other"
            except Exception:  # noqa: BLE001
                self.sectors[b] = "Other"
            await asyncio.sleep(2.0)
        if todo and self.sector_file:
            try:
                tmp = self.sector_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.sectors, f)
                os.replace(tmp, self.sector_file)
            except OSError:
                pass

    async def _markets(self, c) -> list[dict]:
        r = await c.get(f"{CG}/coins/markets", headers=self._headers(),
                        params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": min(250, max(50, self.radar_top)),
                                "page": 1, "price_change_percentage": "1h,24h,7d"})
        if r.status_code != 200:
            self.status = f"CoinGecko {r.status_code}"
            return []
        rows = []
        for x in r.json():
            base = str(x.get("symbol") or "").upper()
            px = float(x.get("current_price") or 0)
            if not base or px <= 0:
                continue
            self.ids[base] = x.get("id")
            dv = float(x.get("total_volume") or 0)           # USD traded in 24h, every venue
            rows.append({"symbol": f"{base}-USD", "base": base, "name": str(x.get("name") or "")[:24], "id": x.get("id"),
                         "chg": round(float(x.get("price_change_percentage_24h") or 0), 2),
                         "chg1h": round(float(x.get("price_change_percentage_1h_in_currency") or 0), 2),
                         "chg7d": round(float(x.get("price_change_percentage_7d_in_currency") or 0), 2),
                         "price": px, "vol": dv / px, "dollar_volume": dv,
                         "mcap": float(x.get("market_cap") or 0), "rank": x.get("market_cap_rank")})
        return rows

    async def _global(self, c):
        try:
            r = await c.get(f"{CG}/global", headers=self._headers())
            if r.status_code == 200:
                g = r.json()["data"]
                return {"total_mcap": g["total_market_cap"]["usd"], "btc_dom": g["market_cap_percentage"]["btc"],
                        "eth_dom": g["market_cap_percentage"].get("eth"), "chg24": g["market_cap_change_percentage_24h_usd"],
                        "volume24": (g.get("total_volume") or {}).get("usd")}
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _stables(self, c):
        """Total dollar-stablecoin supply and its day and week change: new supply is dry powder
        coming in, a shrinking supply is money leaving."""
        try:
            r = await c.get("https://stablecoins.llama.fi/stablecoins", params={"includePrices": "false"}, headers={"User-Agent": UA})
            if r.status_code != 200:
                return None
            tot = prev_d = prev_w = 0.0
            for a in r.json().get("peggedAssets") or []:
                if a.get("pegType") != "peggedUSD":
                    continue
                tot += float((a.get("circulating") or {}).get("peggedUSD") or 0)
                prev_d += float((a.get("circulatingPrevDay") or {}).get("peggedUSD") or 0)
                prev_w += float((a.get("circulatingPrevWeek") or {}).get("peggedUSD") or 0)
            if tot <= 0:
                return None
            return {"supply": tot, "flow_1d": (tot - prev_d) / prev_d if prev_d else 0.0,
                    "flow_7d": (tot - prev_w) / prev_w if prev_w else 0.0}
        except Exception:  # noqa: BLE001
            return None

    async def scan(self, say=None, light=False) -> dict:
        say = say or (lambda *a, **k: None)
        prev = self.state or {}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            rows = await self._markets(c)
            glob = prev.get("global") if light else (await self._global(c) or prev.get("global"))
            stables = prev.get("stables") if light else (await self._stables(c) or prev.get("stables"))
            if not light and rows:
                await self._fill_sectors(c, [r["base"] for r in rows])
        radar = rows or prev.get("radar", [])
        for r in radar:
            r["sector"] = self.sector_of(r["base"]) or "Other"
            r["cat"] = "gainer" if r["chg"] > 0 else "loser"
        top = [r for r in radar if r.get("sector") != "Stablecoin"][:100]
        up = sum(1 for r in top if r["chg"] > 0)
        n = len(top)
        breadth = up / n if n else prev.get("breadth", 0.5)
        # category rotation: cap-weighted 24h change per sector, from the radar itself
        by: dict[str, list] = {}
        for r in radar:
            if r.get("sector") and r["sector"] != "Stablecoin":
                by.setdefault(r["sector"], []).append(r)
        sectors = []
        for name, rs in by.items():
            w = sum(x["mcap"] for x in rs) or 1.0
            sectors.append({"name": name, "chg": round(sum(x["chg"] * x["mcap"] for x in rs) / w, 2), "n": len(rs),
                            "mcap": sum(x["mcap"] for x in rs), "lead": max(rs, key=lambda x: x["mcap"])["base"]})
        sectors.sort(key=lambda x: -x["chg"])
        liquid = [r for r in radar if r["dollar_volume"] >= 5e6 and r.get("sector") != "Stablecoin"]
        movers = {"gainers": sorted(liquid, key=lambda r: -r["chg"])[:12],
                  "losers": sorted(liquid, key=lambda r: r["chg"])[:12],
                  "actives": sorted(radar, key=lambda r: -r["dollar_volume"])[:12]}
        btc_dom = float((glob or {}).get("btc_dom") or 0.0)
        chg24 = float((glob or {}).get("chg24") or 0.0)
        flow_1d = float((stables or {}).get("flow_1d") or 0.0)
        breadth_norm = round(clip(2 * breadth - 1), 3)
        dom_sig = round(clip((btc_dom - 55.0) / 15.0), 3) if btc_dom else 0.0     # + = money hiding in BTC, alts lagging
        stable_sig = round(clip(math.tanh(flow_1d * 100.0)), 3)                   # a 1% day of new supply reads 0.76
        risk = 0.6 * breadth_norm + 0.4 * clip(chg24 / 5.0) + 0.2 * stable_sig
        regime = "risk-on" if risk > 0.2 else "risk-off" if risk < -0.2 else "mixed"
        self.state = {
            "t": time.time(), "breadth": round(breadth, 3), "breadth_up": up, "breadth_n": n,
            "global": glob, "stables": stables, "btc_dom": btc_dom or None, "total_mcap": (glob or {}).get("total_mcap"),
            "mcap_chg24": chg24 if glob else None, "stable_flow_1d": flow_1d if stables else None,
            "risk": round(clip(risk), 3), "regime": regime,
            "sectors": sectors, "movers": movers, "radar": radar[:self.radar_top],
            "feat": {"breadth": breadth_norm, "btc_dom": dom_sig, "stable_flow": stable_sig},
        }
        self.status = (f"{regime}, breadth {breadth * 100:.0f}%, {len(radar)} names" +
                       (f", BTC dominance {btc_dom:.1f}%" if btc_dom else ""))
        say(f"market: {regime} — breadth {up}/{n} up" + (f", BTC dominance {btc_dom:.1f}%" if btc_dom else "") +
            (f", total cap {chg24:+.1f}% 24h" if glob else "") +
            (f", stablecoin supply {flow_1d * 100:+.2f}% 1d" if stables else ""))
        if sectors:
            say(f"category leaders: {sectors[0]['name']} {sectors[0]['chg']:+.1f}%, "
                f"{sectors[1]['name']} {sectors[1]['chg']:+.1f}%; laggard {sectors[-1]['name']} {sectors[-1]['chg']:+.1f}%" if len(sectors) > 1 else "")
        return self.state
