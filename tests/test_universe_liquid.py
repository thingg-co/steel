"""The liquidity-floor universe: every dollar pair the spot venues list above the modeling
floor, the venues' volume reads, the manager's merge, and the picker's bookkeeping."""
import asyncio

from steel.config import Config
from steel.engine import liquid_universe
from steel.sources import CoinbaseSource, KrakenSource, SourceManager


def cfg():
    c = Config()
    c.symbols = ["BTC-USD", "ETH-USD"]
    return c


def test_liquid_universe_takes_every_name_above_the_floor_and_demotes_after_grace():
    core, held = ["BTC-USD", "ETH-USD"], {"HLD-USD"}
    vols = {"BTC-USD": 5e9, "SOL-USD": 2e8, "DOGE-USD": 5e7, "THIN-USD": 2e5, "USDT-USD": 9e9, "HLD-USD": 1e3, "PEPE-USD": 3e6, "EUR-USD": 4e7, "USDG-USD": 2e7}
    u, rec, promoted, demoted = liquid_universe(core, held, core + ["HLD-USD"], vols, {}, floor=1e6, max_universe=100, today="2026-10-11")
    assert u == ["BTC-USD", "ETH-USD", "HLD-USD", "SOL-USD", "DOGE-USD", "PEPE-USD"]       # by volume; stables, fiat and the thin name never
    assert promoted == ["SOL-USD", "DOGE-USD", "PEPE-USD"] and demoted == []
    # PEPE goes thin: it stays through the grace period, then leaves
    vols2 = dict(vols, **{"PEPE-USD": 4e5})
    u2, rec2, p2, d2 = liquid_universe(core, held, u, vols2, rec, floor=1e6, max_universe=100, today="2026-10-12")
    assert u2 == u and p2 == [] and d2 == []
    u3, rec3, p3, d3 = liquid_universe(core, held, u2, vols2, rec2, floor=1e6, max_universe=100, today="2026-11-01")
    assert d3 == ["PEPE-USD"] and "PEPE-USD" not in u3 and p3 == []
    # a fiat pair or a stable that got in (an older rule) leaves at the next pass, grace or not
    u5, _, _, d5 = liquid_universe(core, held, u + ["EUR-USD", "USDG-USD"], vols, rec, floor=1e6, max_universe=100, today="2026-10-12")
    assert "EUR-USD" not in u5 and "USDG-USD" not in u5 and u5 == u
    # the cap is a hard cap: the most liquid fill the room
    u4, _, p4, _ = liquid_universe(core, held, core + ["HLD-USD"], vols, {}, floor=1e6, max_universe=4, today="2026-10-11")
    assert u4 == ["BTC-USD", "ETH-USD", "HLD-USD", "SOL-USD"] and p4 == ["SOL-USD"]


class _R:
    def __init__(self, payload, code=200):
        self.payload, self.status_code = payload, code

    def json(self):
        return self.payload


def test_coinbase_volumes_are_base_units_times_a_price(monkeypatch):
    src = CoinbaseSource(cfg(), ["BTC-USD"])
    asked = []

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            asked.append(url)
            if url.endswith("/products/volume-summary"):
                return _R([{"id": "BTC-USD", "quote_currency": "USD", "spot_volume_24hour": "1500"},
                           {"id": "HOPR-USD", "quote_currency": "USD", "spot_volume_24hour": "1650000"},
                           {"id": "DEAD-USD", "quote_currency": "USD", "spot_volume_24hour": "0"},
                           {"id": "BTC-EUR", "quote_currency": "EUR", "spot_volume_24hour": "100"}])
            if url.endswith("/products/HOPR-USD/ticker"):
                return _R({"price": "0.03"})
            return _R({}, 404)
    monkeypatch.setattr("steel.sources.coinbase.client", lambda *a, **k: C())
    vols = asyncio.run(src.volumes({"BTC-USD": 80_000.0}))
    assert vols == {"BTC-USD": 1500 * 80_000.0, "HOPR-USD": 1650000 * 0.03}       # the EUR pair and the dead one are left out
    assert sum(1 for u in asked if u.endswith("/ticker")) == 1                     # one ticker read, for the unpriced name only


def test_kraken_volumes_are_volume_times_vwap(monkeypatch):
    src = KrakenSource(cfg(), ["BTC-USD"])

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if url.endswith("/AssetPairs"):
                return _R({"result": {"XXBTZUSD": {"wsname": "XBT/USD", "altname": "XBTUSD", "status": "online"},
                                      "SOLUSD": {"wsname": "SOL/USD", "altname": "SOLUSD", "status": "online"},
                                      "XETHZEUR": {"wsname": "ETH/EUR", "altname": "ETHEUR", "status": "online"}}})
            if url.endswith("/Ticker"):
                return _R({"result": {"XXBTZUSD": {"v": ["50", "500"], "p": ["82000", "82500"]},
                                      "SOLUSD": {"v": ["10", "10000"], "p": ["110", "111"]},
                                      "XETHZEUR": {"v": ["1", "2"], "p": ["3", "4"]}}})
            return _R({}, 404)
    monkeypatch.setattr("steel.sources.kraken.httpx.AsyncClient", lambda *a, **k: C())
    vols = asyncio.run(src.volumes())
    assert vols == {"BTC-USD": 500 * 82500.0, "SOL-USD": 10000 * 111.0}
    assert src.knows("BTC-USD") and src.knows("SOL-USD") and not src.knows("ETH-USD")


def test_manager_merges_the_spot_venues_and_survives_a_failure():
    c = cfg()
    m = SourceManager(c, c.symbols, on_tick=lambda t: None)

    async def cb(prices=None):
        return {"BTC-USD": 1e9, "SOL-USD": 2e8}

    async def kr(prices=None):
        return {"BTC-USD": 2e9, "DOGE-USD": 3e7}

    async def boom(prices=None):
        raise RuntimeError("down")
    m.sources["coinbase"].volumes = cb
    m.sources["kraken"].volumes = kr
    m.sources["hyperliquid"].volumes = boom                                         # a perp venue is never asked
    m.enabled["bitget"] = True
    m.sources["bitget"].volumes = boom
    vols = asyncio.run(m.volumes({}))
    assert vols == {"BTC-USD": 2e9, "SOL-USD": 2e8, "DOGE-USD": 3e7}                 # the most any venue reports; a failure costs nothing
