"""Prediction markets: Polymarket's open crypto markets (public Gamma API, no key), the
yes-price as the crowd's probability, refreshed on a cadence. Read-only: there is no order
entry here and never will be."""
from __future__ import annotations

import json
import time

import httpx

GAMMA = "https://gamma-api.polymarket.com/markets"
CRYPTO_TAG = 21
UA = "steel/1.0"


def parse(rows: list[dict]) -> list[dict]:
    out = []
    for m in rows:
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            outcomes = json.loads(m.get("outcomes") or "[]")
        except ValueError:
            continue
        if not prices or not outcomes:
            continue
        yes = next((float(p) for o, p in zip(outcomes, prices) if str(o).lower() == "yes"), float(prices[0]))
        out.append({"question": str(m.get("question") or ""), "slug": str(m.get("slug") or ""), "yes": round(yes, 4), "outcomes": outcomes[:4],
                    "prices": [round(float(p), 4) for p in prices[:4]], "volume24h": round(float(m.get("volume24hr") or 0.0), 2),
                    "liquidity": round(float(m.get("liquidity") or 0.0), 2), "end": str(m.get("endDate") or "")})
    out.sort(key=lambda x: -x["volume24h"])
    return out


async def fetch(limit: int = 60, timeout: float = 20.0) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}) as c:
        r = await c.get(GAMMA, params={"closed": "false", "limit": limit, "tag_id": CRYPTO_TAG, "order": "volume24hr", "ascending": "false"})
        r.raise_for_status()
        d = r.json()
        return parse(d if isinstance(d, list) else d.get("data") or [])


class PredictionHub:
    def __init__(self):
        self.markets: list[dict] = []
        self.at = 0.0
        self.error = ""

    async def refresh(self) -> list[dict]:
        try:
            self.markets = await fetch()
            self.at = time.time()
            self.error = ""
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {str(e)[:80]}"
        return self.markets

    def status(self) -> dict:
        return {"n": len(self.markets), "at": self.at, "error": self.error, "source": "polymarket"}
