"""Skim the crypto news desks with a headless browser and turn the headlines into per-asset signals.

The skimmer renders each source in headless Chromium (Playwright), pulls
headline-shaped link text out of the DOM, tags each headline with the assets it
mentions and a lexicon sentiment score, and aggregates that into a per-asset
tone and attention level. If the browser is unavailable it falls back to plain
HTTP and regex extraction.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from dataclasses import dataclass
import httpx

from .sentiment import _NEG, _POS, NEGATIVE, POSITIVE, get_scorer, lexicon_tone  # noqa: F401 -- re-exported for callers and tests that read the lists

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

CRYPTO_SOURCES = [
    ("CoinDesk", "https://www.coindesk.com/"),
    ("Cointelegraph", "https://cointelegraph.com/"),
    ("The Block", "https://www.theblock.co/latest"),
    ("Decrypt", "https://decrypt.co/news"),
    ("Google News crypto", "https://news.google.com/search?q=bitcoin%20OR%20ethereum%20OR%20solana%20OR%20xrp%20OR%20crypto&hl=en-US&gl=US&ceid=US%3Aen"),
]
DEFAULT_SOURCES = CRYPTO_SOURCES


def is_crypto(sym: str) -> bool:
    return True                             # every name here is a BASE-USD pair


def default_sources(symbols: list[str]) -> list[tuple[str, str]]:
    """The crypto desks. Kept as a function for callers that pass the universe."""
    return list(CRYPTO_SOURCES)


# bases that are also English words: matched only through their aliases, never as a bare word
TICKER_STOPLIST = {"ALL", "NOW", "APP", "NET", "LOW", "COST", "CAT", "KEYS", "MET", "ARM", "DASH", "SNAP", "TEAM",
                   "FAST", "GOLD", "PLAY", "TMC", "SU", "BE", "ON", "ET", "IT", "SO", "AN", "AM", "PM", "WELL", "LIVE",
                   "LOVE", "REAL", "BIG", "RUN", "BOOT", "OPEN", "HOLD", "SEE", "ONE", "TWO", "TEN", "ACT", "FIT", "FUN",
                   "FLOW", "MOON", "DEEP", "HIGH", "SAFE", "GRASS", "TREE", "HOME", "EDGE", "TRUST", "PRIME", "MAGIC", "META",
                   "DATA", "INDEX", "UP", "ME", "CAP", "SKY", "LAYER", "WET", "DEGEN", "HYPE", "MASK", "CHECK", "WAL", "IO"}


def asset_patterns(bases: dict[str, str]) -> dict[str, re.Pattern]:
    """One regex per modeled base: the names and aliases from ASSET_TERMS, else the ticker as
    an upper-case word of three or more letters or a $-prefixed one (shorter ones and the
    stoplist are too often ordinary words; "$HYPE" is a name, "hype" is not)."""
    out: dict[str, re.Pattern] = {}
    for base in set(bases.values()):
        if base in ASSET_TERMS:
            out[base] = _ASSET[base]
        elif len(base) >= 3 and base.isalnum() and base not in TICKER_STOPLIST:
            out[base] = re.compile(rf"(?<![A-Za-z])\$?{re.escape(base)}(?![A-Za-z])")
        else:
            out[base] = re.compile(rf"\${re.escape(base)}\b")
    return out

ASSET_TERMS = {
    "BTC": [r"bitcoin", r"\bbtc\b"],
    "ETH": [r"ethereum", r"\bether\b", r"\beth\b"],
    "SOL": [r"solana", r"\bsol\b"],
    "XRP": [r"\bxrp\b", r"\bripple\b"],
    "DOGE": [r"dogecoin", r"\bdoge\b"],
    "ADA": [r"cardano", r"\bada\b"],
    "AVAX": [r"avalanche", r"\bavax\b"],
    "LINK": [r"chainlink", r"\blink\b"],
    "DOT": [r"polkadot", r"\bdot\b"],
    "LTC": [r"litecoin", r"\bltc\b"],
    "BCH": [r"bitcoin cash", r"\bbch\b"],
    "BNB": [r"\bbnb\b", r"binance coin"],
    "POL": [r"\bpolygon\b", r"\bmatic\b", r"\bpol\b"],
    "ATOM": [r"\bcosmos\b", r"\batom\b"],
    "UNI": [r"\buniswap\b", r"\buni\b"],
    "AAVE": [r"\baave\b"],
    "NEAR": [r"near protocol", r"\bnear\b"],
    "SUI": [r"\bsui\b"],
    "APT": [r"\baptos\b", r"\bapt\b"],
    "ARB": [r"\barbitrum\b", r"\barb\b"],
    "OP": [r"\boptimism\b", r"\$op\b"],
    "XLM": [r"\bstellar\b", r"\bxlm\b"],
    "HBAR": [r"\bhedera\b", r"\bhbar\b"],
    "ICP": [r"internet computer", r"\bicp\b"],
    "FIL": [r"\bfilecoin\b", r"\bfil\b"],
    "ETC": [r"ethereum classic", r"\$etc\b"],
    "INJ": [r"\binjective\b", r"\binj\b"],
    "TIA": [r"\bcelestia\b", r"\btia\b"],
    "SEI": [r"\bsei\b"],
    "RENDER": [r"\brender\b"],
    "FET": [r"fetch\.ai", r"\bfet\b", r"artificial superintelligence alliance"],
    "ONDO": [r"\bondo\b"],
    "TAO": [r"\bbittensor\b", r"\btao\b"],
    "ALGO": [r"\balgorand\b", r"\balgo\b"],
    "VET": [r"\bvechain\b", r"\bvet\b"],
    "CRO": [r"\bcronos\b", r"crypto\.com", r"\bcro\b"],
    "STX": [r"\bstacks\b", r"\bstx\b"],
    "IMX": [r"\bimmutable\b", r"\bimx\b"],
    "GRT": [r"the graph", r"\bgrt\b"],
    "LDO": [r"\blido\b", r"\bldo\b"],
    "ENS": [r"ethereum name service", r"\bens\b"],
    "CRV": [r"\bcurve\b", r"\bcrv\b"],
    "SAND": [r"the sandbox", r"\bsand\b"],
    "MANA": [r"\bdecentraland\b", r"\bmana\b"],
    "SHIB": [r"shiba inu", r"\bshib\b"],
    "PEPE": [r"\bpepe\b"],
    "BONK": [r"\bbonk\b"],
    "WIF": [r"dogwifhat", r"\bwif\b"],
    "ZEC": [r"\bzcash\b", r"\bzec\b"],
    "HYPE": [r"\bhyperliquid\b", r"\$hype\b"],
    "ENA": [r"\bethena\b", r"\bena\b"],
    "WLD": [r"\bworldcoin\b", r"\bwld\b"],
    "PENDLE": [r"\bpendle\b"],
    "JTO": [r"\bjito\b", r"\bjto\b"],
    "PYTH": [r"\bpyth\b"],
    "QNT": [r"\bquant\b", r"\bqnt\b"],
    "STRK": [r"\bstarknet\b", r"\bstrk\b"],
    "TON": [r"\btoncoin\b", r"the open network", r"\bton\b"],
    "ZK": [r"\bzksync\b", r"\$zk\b"],
    "EIGEN": [r"\beigenlayer\b", r"\beigen\b"],
    "MORPHO": [r"\bmorpho\b"],
    "TRX": [r"\btron\b", r"\btrx\b"],
    "XMR": [r"\bmonero\b", r"\bxmr\b"],
}
GENERIC_TERMS = [r"crypto", r"cryptocurrenc", r"digital asset", r"stablecoin", r"altcoin", r"\betf\b", r"\bsec\b",
                 r"blockchain", r"\bdefi\b", r"\btoken", r"\bperp", r"\bfunding rate", r"liquidation", r"\bexchange\b",
                 r"\bfed\b", r"inflation", r"treasur", r"\btariff", r"\bmarkets?\b", r"\bwhale", r"on-chain", r"\bmining\b",
                 r"\bhalving\b", r"\bairdrop\b", r"\blayer.?2\b"]

_ASSET = {k: re.compile("|".join(v), re.I) for k, v in ASSET_TERMS.items()}
_GENERIC = re.compile("|".join(GENERIC_TERMS), re.I)


def attention_of(mentions: int, top: int) -> float:
    """A name's share of the pass's leading coverage: 1 for the most-covered name, 0 for none,
    the square root of the share between, so the low end is not crushed."""
    if not top or mentions <= 0:
        return 0.0
    return round(min(1.0, math.sqrt(mentions / top)), 3)


def name_tone(direct, market: float, prior: float = 1.0) -> float:
    """The tone of a name's own headlines, with the market tone as one headline's worth of
    prior: a name with no coverage reads the market, a well-covered one reads itself."""
    n = len(direct)
    if n == 0:
        return float(market)
    return (sum(h.sentiment for h in direct) + prior * float(market)) / (n + prior)


async def rescore(scorer, headlines) -> None:
    """Replace the word-list tone with the scorer's, off the event loop; a no-op without one."""
    if scorer is None or not headlines:
        return
    vals = await asyncio.to_thread(scorer.score, [h.title for h in headlines])
    for h, v in zip(headlines, vals):
        h.sentiment = float(v)


@dataclass
class Headline:
    title: str
    source: str
    url: str
    assets: list[str]
    generic: bool
    sentiment: float
    new: bool
    ts: float

    def to_json(self) -> dict:
        return dict(self.__dict__)


def score_headline(text: str, asset_rx: dict[str, re.Pattern] | None = None) -> tuple[list[str], bool, float]:
    assets = [a for a, rx in (asset_rx if asset_rx is not None else _ASSET).items() if rx.search(text)]
    generic = bool(_GENERIC.search(text))
    return assets, generic, lexicon_tone(text)


JS_EXTRACT = r"""() => {
  const sel = 'h1 a, h2 a, h3 a, h4 a, a h1, a h2, a h3, a h4, article a, article h2, article h3, article h4, [class*="headline"] a, [class*="title"] a';
  const seen = new Set(); const out = [];
  document.querySelectorAll(sel).forEach(el => {
    const a = el.closest('a') || el.querySelector('a');
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length < 25 || text.length > 220 || !/[a-zA-Z]{3}/.test(text)) return;
    if (seen.has(text)) return; seen.add(text);
    out.push([text, a && a.href ? a.href : '']);
  });
  return out.slice(0, 400);
}"""

_TAG = re.compile(r"<[^>]+>")
_ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_HEAD = re.compile(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", re.I | re.S)


def _clean_text(fragment: str) -> str:
    t = html.unescape(_TAG.sub(" ", fragment))
    return re.sub(r"\s+", " ", t).strip()


def _http_candidates(page: str, base: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _ANCHOR.finditer(page):
        t = _clean_text(m.group(2))
        if 25 <= len(t) <= 220 and t not in seen and re.search(r"[a-zA-Z]{3}", t):
            seen.add(t)
            href = m.group(1)
            try:
                url = str(httpx.URL(base).join(href)) if href else ""
            except Exception:  # noqa: BLE001
                url = ""
            out.append((t, url))
    for m in _HEAD.finditer(page):
        t = _clean_text(m.group(1))
        if 25 <= len(t) <= 220 and t not in seen and re.search(r"[a-zA-Z]{3}", t):
            seen.add(t)
            out.append((t, ""))
    return out[:400]


def parse_sources(spec: str) -> list[tuple[str, str]]:
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, url = item.partition("|")
        if url:
            out.append((name.strip(), url.strip()))
    return out


class NewsSkimmer:
    id = "browser"
    name = "Headless browser"

    def __init__(self, symbols: list[str], sources_spec: str = "", use_browser: bool = True, scorer=None):
        self.scorer = scorer                      # graded tone (flint/sentiment.py); None keeps the word lists
        self.symbols = symbols
        self.bases = {s: s.split("-")[0].upper() for s in symbols}
        self.asset_rx = asset_patterns(self.bases)
        self.sources = parse_sources(sources_spec) or default_sources(symbols)
        self.use_browser = use_browser
        self.browser_ok: bool | None = None
        self.seen: dict[str, float] = {}
        self.status: dict[str, str] = {}
        self.method = "none"

    async def skim(self, say=None) -> dict:
        say = say or (lambda text, level="info": None)
        now = time.time()
        self.seen = {t: ts for t, ts in self.seen.items() if now - ts < 86400}
        raw: dict[str, list[tuple[str, str]]] = {}
        if self.use_browser and self.browser_ok is not False:
            try:
                raw = await self._with_browser(say)
                self.browser_ok = True
                self.method = "headless chromium"
            except Exception as e:  # noqa: BLE001
                self.browser_ok = False
                self.method = "http"
                say(f"headless browser unavailable ({type(e).__name__}: {str(e)[:80]}); using plain HTTP", "warn")
        else:
            self.method = "http"
        for name, url in self.sources:
            if raw.get(name):
                continue
            try:
                raw[name] = await self._with_http(url)
                self.status[name] = f"http: {len(raw[name])} candidates"
                say(f"{name}: fetched over HTTP, {len(raw[name])} headline candidates")
            except Exception as e:  # noqa: BLE001
                self.status[name] = f"http failed: {type(e).__name__}"
                raw[name] = []
                say(f"{name}: fetch failed ({type(e).__name__})", "warn")

        headlines: list[Headline] = []
        titles: set[str] = set()
        for name, _ in self.sources:
            for title, url in raw.get(name, []):
                key = title.lower()
                if key in titles:
                    continue
                titles.add(key)
                assets, generic, sent = score_headline(title, self.asset_rx)
                if not assets and not generic:
                    continue
                is_new = key not in self.seen
                self.seen.setdefault(key, now)
                headlines.append(Headline(title, name, url, assets, generic, sent, is_new, now))
        await rescore(self.scorer, headlines)

        per_asset: dict[str, dict] = {}
        generic = [h for h in headlines if h.generic and not h.assets]
        market = sum(h.sentiment for h in generic) / len(generic) if generic else 0.0
        for sym, base in self.bases.items():
            direct = [h for h in headlines if base in h.assets]
            per_asset[sym] = {
                "mentions": len(direct),
                "generic": len(generic),
                "sentiment": round(name_tone(direct, market), 3),
                "attention": attention_of(len(direct), max((len([h for h in headlines if b in h.assets]) for b in self.bases.values()), default=0)),
                "top": [h.title for h in sorted(direct, key=lambda h: -abs(h.sentiment))[:3]],
            }
        return {
            "t": now,
            "method": self.method,
            "status": dict(self.status),
            "headlines": [h.to_json() for h in headlines],
            "per_asset": per_asset,
            "ideas": self._ideas(per_asset, headlines),
            "new": sum(1 for h in headlines if h.new),
        }

    def _ideas(self, per_asset: dict, headlines: list[Headline]) -> list[str]:
        """One line per name with coverage, most covered first, and one line for the rest:
        one line per quiet name would bury the panel."""
        ideas = []
        covered = sorted(((s, pa) for s, pa in per_asset.items() if pa["mentions"]),
                         key=lambda kv: (-kv[1]["mentions"], -abs(kv[1]["sentiment"])))
        for sym, pa in covered[:24]:
            base = self.bases[sym]
            s = pa["sentiment"]
            tilt = "bullish" if s > 0.15 else "bearish" if s < -0.15 else "mixed"
            lead = pa["top"][0] if pa["top"] else ""
            line = f"{base}: {pa['mentions']} headline{'s' if pa['mentions'] != 1 else ''}, tone {s:+.2f} ({tilt}), attention {pa['attention']:.2f}"
            if lead:
                line += f' | "{lead[:100]}"'
            ideas.append(line)
        quiet = len(per_asset) - len(covered)
        gen = [h for h in headlines if h.generic and not h.assets]
        tail = f"{quiet} names without direct coverage this pass" if quiet else ""
        if gen:
            mean = sum(h.sentiment for h in gen) / len(gen)
            tail += ("; " if tail else "") + f"market: {len(gen)} general headlines, tone {mean:+.2f}"
        if tail:
            ideas.append(tail[0].upper() + tail[1:])
        return ideas

    async def _with_browser(self, say) -> dict[str, list[tuple[str, str]]]:
        from playwright.async_api import async_playwright

        out: dict[str, list[tuple[str, str]]] = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="en-US")
                for name, url in self.sources:
                    page = await ctx.new_page()
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(2000)
                        cands = await page.evaluate(JS_EXTRACT)
                        out[name] = [(c[0], c[1]) for c in cands]
                        self.status[name] = f"browser: {len(cands)} candidates"
                        say(f"{name}: rendered in headless chromium, {len(cands)} headline candidates")
                    except Exception as e:  # noqa: BLE001
                        self.status[name] = f"browser failed: {type(e).__name__}"
                        say(f"{name}: page failed in browser ({type(e).__name__}), will retry over HTTP", "warn")
                    finally:
                        await page.close()
            finally:
                await browser.close()
        return out

    async def _with_http(self, url: str) -> list[tuple[str, str]]:
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            return _http_candidates(r.text, str(r.url))


class NewsHub:
    """Holds the toggleable news sources and merges their output per asset."""

    def __init__(self, cfg, symbols: list[str]):
        self.cfg = cfg
        self.symbols = symbols
        self.sources: dict[str, object] = {}
        self.enabled: dict[str, bool] = {}
        off = {x.strip() for x in cfg.news_off.split(",") if x.strip()}
        self.scorer = get_scorer(cfg)
        browser = NewsSkimmer(symbols, cfg.news_sources, cfg.news_browser, scorer=self.scorer)
        self.sources[browser.id] = browser
        self.enabled[browser.id] = "browser" not in off

    def toggle(self, src_id: str, on: bool) -> dict:
        if src_id not in self.sources:
            return {"error": "unknown news source"}
        self.enabled[src_id] = on
        return {"ok": True}

    def status(self) -> list[dict]:
        return [{"id": sid, "name": src.name, "enabled": self.enabled.get(sid, False)}
                for sid, src in self.sources.items()]

    async def skim(self, say=None) -> dict:
        say = say or (lambda text, level="info": None)
        results = []
        for sid, src in self.sources.items():
            if not self.enabled.get(sid):
                continue
            try:
                results.append((sid, await asyncio.wait_for(src.skim(say), timeout=240)))
            except Exception as e:  # noqa: BLE001
                say(f"{src.name} skim failed: {type(e).__name__}: {str(e)[:100]}", "error")
        headlines: list[dict] = []
        seen: set[str] = set()
        for _, res in results:
            for h in res.get("headlines", []):
                key = h["title"].lower()[:120]
                if key and key not in seen:
                    seen.add(key)
                    headlines.append(h)
        headlines.sort(key=lambda h: -h.get("ts", 0))
        per_asset = {}
        for sym in self.symbols:
            weighted, wsum, mentions, tops = 0.0, 0.0, 0, []
            attn = 0.0
            for _, res in results:
                pa = res.get("per_asset", {}).get(sym)
                if not pa:
                    continue
                w = 1.0 + pa["mentions"]
                weighted += pa["sentiment"] * w
                wsum += w
                mentions += pa["mentions"]
                attn = max(attn, pa["attention"])
                tops += pa.get("top", [])
            per_asset[sym] = {"mentions": mentions, "generic": 0, "sentiment": round(weighted / wsum, 3) if wsum else 0.0,
                              "attention": round(attn, 3), "top": tops[:3]}
        # Attention is relative: the name with the most headlines this pass is 1, one with none is 0,
        # the rest by the square root of their share, so 1 of 10 reads 0.32 and 5 of 10 reads 0.71.
        # Before 0.5.14 every name's weight carried half of the same general headlines and a log
        # scale against 120, so 8 and 10 headlines both read 0.76 and the feature said nothing.
        top_n = max((pa["mentions"] for pa in per_asset.values()), default=0)
        for pa in per_asset.values():
            pa["attention"] = attention_of(pa["mentions"], top_n)
        methods = ", ".join(r.get("method", sid) for sid, r in results) or "no sources enabled"
        ideas = self._ideas(per_asset)
        return {"t": time.time(), "method": methods, "sources": self.status(), "headlines": headlines,
                "per_asset": per_asset, "ideas": ideas, "new": sum(1 for h in headlines if h.get("new")),
                "scorer": getattr(self.scorer, "name", "lexicon")}

    def _ideas(self, per_asset: dict) -> list[str]:
        """Names with coverage, most covered first, then one line for the rest."""
        ideas = []
        covered = sorted(((s, pa) for s, pa in per_asset.items() if pa["mentions"]),
                         key=lambda kv: (-kv[1]["mentions"], -abs(kv[1]["sentiment"])))
        for sym, pa in covered[:24]:
            base = sym.split("-")[0].upper()
            s = pa["sentiment"]
            tilt = "bullish" if s > 0.15 else "bearish" if s < -0.15 else "mixed"
            lead = pa["top"][0] if pa["top"] else ""
            line = f"{base}: {pa['mentions']} headline{'s' if pa['mentions'] != 1 else ''}, tone {s:+.2f} ({tilt}), attention {pa['attention']:.2f}"
            if lead:
                line += f' | "{lead[:100]}"'
            ideas.append(line)
        quiet = len(per_asset) - len(covered)
        if quiet:
            ideas.append(f"{quiet} names without direct coverage this pass")
        return ideas
