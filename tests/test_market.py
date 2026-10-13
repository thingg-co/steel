"""The whole-market scan: categories, breadth, dominance, stablecoin flow, the radar. No network."""
import asyncio

from steel.config import CORE
from steel.market import SECTOR_MAP, MarketScanner, sector_from_categories


def test_every_core_name_has_a_category():
    missing = [b for b in CORE.split(",") if b not in SECTOR_MAP]
    assert not missing, missing


def test_sector_from_categories():
    assert sector_from_categories(["Meme", "Solana Ecosystem"]) == "Meme"
    assert sector_from_categories(["Layer 2 (L2)", "Ethereum Ecosystem"]) == "Layer 2"
    assert sector_from_categories(["Decentralized Finance (DeFi)"]) == "DeFi"
    assert sector_from_categories(["Smart Contract Platform"]) == "Smart contract L1"
    assert sector_from_categories(["Something new"]) == "Other" and sector_from_categories([]) == "Other"


def test_scan_from_canned_data(tmp_path):
    sc = MarketScanner("", radar_top=250, state_dir=str(tmp_path))
    rows = []
    for k in range(120):
        base = f"C{k}" if k >= 3 else ["BTC", "ETH", "USDT"][k]
        px = 100.0
        rows.append({"symbol": f"{base}-USD", "base": base, "name": base, "id": base.lower(), "chg": (3.0 if k % 4 else -2.0),
                     "chg1h": 0.1, "chg7d": 1.0, "price": px, "vol": 1e6, "dollar_volume": 1e8 / (k + 1), "mcap": 1e9 / (k + 1), "rank": k + 1})
    rows[2]["chg"] = 0.0

    async def markets(c):
        return rows

    async def glob(c):
        return {"total_mcap": 3e12, "btc_dom": 60.0, "eth_dom": 12.0, "chg24": 2.5, "volume24": 1e11}

    async def stables(c):
        return {"supply": 2e11, "flow_1d": 0.005, "flow_7d": 0.01}

    async def fill(c, bases, budget=8):
        return None
    sc._markets, sc._global, sc._stables, sc._fill_sectors = markets, glob, stables, fill

    class C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
    import steel.market as mm
    orig = mm.httpx.AsyncClient
    mm.httpx.AsyncClient = lambda *a, **k: C()
    try:
        st = asyncio.run(sc.scan())
    finally:
        mm.httpx.AsyncClient = orig
    assert st["breadth_n"] == 100 and 0.7 < st["breadth"] < 0.8          # the stable is excluded from the top 100
    assert st["btc_dom"] == 60.0 and st["feat"]["btc_dom"] > 0 and st["feat"]["breadth"] > 0 and st["feat"]["stable_flow"] > 0
    assert st["regime"] == "risk-on"
    assert st["radar"][0]["sector"] == "Store of value" and st["radar"][2]["sector"] == "Stablecoin"
    assert all(r["sector"] == "Other" for r in st["radar"][3:])
    assert st["movers"]["gainers"][0]["chg"] == 3.0 and st["movers"]["losers"][0]["chg"] == -2.0
    assert "Stablecoin" not in {s["name"] for s in st["sectors"]}
    assert st["sectors"][0]["chg"] >= st["sectors"][-1]["chg"]
    assert "risk-on" in sc.status and "BTC dominance 60.0%" in sc.status
