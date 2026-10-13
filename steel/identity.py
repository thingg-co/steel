"""Asset identity across venues: a ticker is not a token.

NIGHT, PONS, US and CHIP mean different tokens on different exchanges, and a model that matches
venues by ticker would pair a Coinbase listing with an unrelated token's book on another venue
(the first live hour of the dislocation monitor showed 300-bps "gaps" on exactly such names).
The book here resolves what it can: CoinGecko's ranked list (the top 1,000 by market cap, four
calls a day) gives every symbol its candidates, so a symbol with more than one ranked coin is
`ambiguous`; Coinbase's currency list gives a name per listing, matched to a CoinGecko name,
so a Coinbase listing of an ambiguous symbol resolves to one coin. Venues that publish no name
resolve nothing. The dislocation monitor pairs an ambiguous name only between venues that
resolved to the same coin (which today means it stays out of the cross-venue table), the
manager lets only an identity-verified venue serve an ambiguous name, and the Names detail says
which coin a name is. Persisted in state/identity.json; refreshed daily.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .sources import UA, base_of

CG = "https://api.coingecko.com/api/v3"
COINBASE = "https://api.exchange.coinbase.com"
PAGES = 4                                      # 4 x 250: the top 1,000 by market cap


def _norm(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


class IdentityBook:
    def __init__(self, state_dir: str | Path = "state", key: str = ""):
        self.path = Path(state_dir) / "identity.json"
        self.key = key
        self.coins: dict[str, list[dict]] = {}          # BASE -> [{id, name, rank}], best rank first
        self.venue_names: dict[str, dict[str, str]] = {}  # venue -> BASE -> the venue's name for the listing
        self.t = 0.0
        self.error = ""
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            self.coins = {k: list(v) for k, v in (d.get("coins") or {}).items()}
            self.venue_names = {k: dict(v) for k, v in (d.get("venue_names") or {}).items()}
            self.t = float(d.get("t") or 0.0)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"t": self.t, "coins": self.coins, "venue_names": self.venue_names}))
        except OSError:
            pass

    @property
    def stale(self) -> bool:
        return time.time() - self.t > 86400.0

    def load_coins(self, rows: list[dict]) -> None:
        """Index CoinGecko market rows by symbol (any source: the API, or the market scan's pages)."""
        coins: dict[str, list[dict]] = {}
        for r in rows:
            sym = str(r.get("symbol") or "").upper()
            if not sym or not r.get("id"):
                continue
            coins.setdefault(sym, []).append({"id": r["id"], "name": str(r.get("name") or ""), "rank": int(r.get("market_cap_rank") or 10**6)})
        for v in coins.values():
            v.sort(key=lambda c: c["rank"])
        self.coins = coins

    async def refresh(self) -> None:
        """CoinGecko's top 1,000 and Coinbase's currency names, once a day."""
        rows: list[dict] = []
        headers = {"User-Agent": UA} | ({"x-cg-demo-api-key": self.key} if self.key else {})
        async with httpx.AsyncClient(timeout=25) as c:
            for page in range(1, PAGES + 1):
                try:
                    r = await c.get(f"{CG}/coins/markets", headers=headers,
                                    params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page})
                    if r.status_code != 200:
                        self.error = f"coingecko {r.status_code}"
                        break
                    rows += [x for x in r.json() if isinstance(x, dict)]
                except Exception as e:  # noqa: BLE001
                    self.error = f"coingecko: {type(e).__name__}"
                    break
                import asyncio
                await asyncio.sleep(2.5)
            try:
                r = await c.get(f"{COINBASE}/currencies", headers={"User-Agent": UA})
                if r.status_code == 200:
                    self.venue_names["coinbase"] = {str(x["id"]).upper(): str(x.get("name") or "") for x in r.json() if x.get("id")}
            except Exception as e:  # noqa: BLE001
                self.error = f"coinbase currencies: {type(e).__name__}"
        if rows:
            self.load_coins(rows)
            self.t = time.time()
            self.error = ""
        self._save()

    # Queries ---------------------------------------------------------------------------
    def candidates(self, sym: str) -> list[dict]:
        return list(self.coins.get(base_of(sym), []))

    def ambiguous(self, sym: str) -> bool:
        """More than one ranked coin uses this symbol."""
        return len(self.coins.get(base_of(sym), [])) > 1

    def known(self, sym: str) -> bool:
        return bool(self.coins.get(base_of(sym)))

    def resolve(self, venue: str, sym: str) -> str | None:
        """The CoinGecko id a venue's listing is, when it can be told: the only ranked coin with
        that symbol, or the one whose name matches the name the venue publishes."""
        cands = self.coins.get(base_of(sym), [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]["id"]
        name = (self.venue_names.get(venue) or {}).get(base_of(sym))
        if name:
            n = _norm(name)
            for c in cands:
                if _norm(c["name"]) == n:
                    return c["id"]
        return None

    def label(self, sym: str, venue: str | None = None) -> dict:
        """What the dashboard shows for a name: the coin it is (or the best guess), and whether
        the ticker is shared."""
        cands = self.coins.get(base_of(sym), [])
        rid = self.resolve(venue, sym) if venue else None
        pick = next((c for c in cands if c["id"] == rid), cands[0] if cands else None)
        return {"id": pick["id"] if pick else None, "name": pick["name"] if pick else None, "rank": pick["rank"] if pick else None,
                "ambiguous": len(cands) > 1, "resolved": rid is not None or len(cands) == 1,
                "others": [c["name"] for c in cands if pick is None or c["id"] != pick["id"]][:4]}

    def status(self) -> dict:
        return {"t": self.t, "symbols": len(self.coins), "ambiguous": sum(1 for v in self.coins.values() if len(v) > 1),
                "venues_named": sorted(self.venue_names), "error": self.error, "stale": self.stale}
