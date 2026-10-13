"""The engine: feed -> bars -> features -> model -> suggestions, plus everything the UI sees."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

from .accounts import ACCOUNTS, fetch_balances, held_symbols
from .autotune import PRESETS, free_memory_gb
from .backtest import combine_reports, run_backtest
from .bars import Bar, BarBuilder
from .config import read_creds, canonical
from .features import FEATURES, N_FEATURES, FeatureBuilder
from .feed import Tick
from .learner import OnlineLearner, Prediction
from .news import TICKER_STOPLIST, NewsHub
from .paper import PaperBook
from .signals import SignalHub
from .target import hedge_targets, index_columns
from .sources import SourceManager, asset_class, base_of
from . import alerts as _alerts
from .market import SECTOR_MAP
from . import auth as _auth
from . import arbreplay as _arbreplay
from . import backup as _backup
from .handoff import Handoff
from . import clockcheck as _clockcheck
from . import vault as _vault
from .dislocation import DislocationMonitor
from .identity import IdentityBook
from .arb import ArbExecutor
from .live import LiveBook, Promotion
from .execution import EXECUTORS, ExecutionGate
from .sources.fees import fee_profile, transfer_cost
from .trace import Trace
from . import clock, policy

log = logging.getLogger(__name__)

TUNABLE = {"score_threshold": float, "prob_margin": float, "cost_bps": float, "max_size": float,
           "lr": float, "steps_per_label": int, "min_labels": int, "news_minutes": float,
           "overlay_aggr": float, "overlay_fade_at": float, "overlay_safety": float, "signals_minutes": float,
           "kelly_fraction": float, "move_floor_bps": float, "min_hit_rate": float,
           "deep_backfill_days": float, "train_epochs": float,
           "skill_min_n": int, "confirm_bars": int, "min_hold_bars": int, "short_min_hold_bars": int, "max_spread_bps": float,
           "min_dollar_volume": float, "short_leverage": float, "short_max_frac": float, "perp_fee_bps": float, "spot_fee_bps": float,
           "long_leverage": float,
           "impact_eta": float, "max_impact_bps": float, "funding_in_edge": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
           "stop_loss_bps": float, "take_profit_bps": float, "max_hold_bars": int,
           "exit_cooldown_bars": int, "daily_loss_limit": float,
           "horizon_agree": lambda v: str(v).lower() in ("1", "true", "yes", "on"), "vol_target": float,
           "max_cluster_weight": float, "max_beta_exposure": float,
           "backtest_weekly": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
           "backtest_weekday": lambda v: max(0, min(6, int(float(v)))), "backtest_hour": lambda v: max(0, min(23, int(float(v)))),
           "backtest_hours": float, "backtest_days": int, "backtest_cost_bps": float, "backtest_night_epochs": float,
           "backtest_target": lambda v: str(v).lower() if str(v).lower() in ("absolute", "relative", "both") else "both",
           "hedge_index": lambda v: str(v).lower() in ("1", "true", "yes", "on"), "hedge_min": float,
           "overfit_patience": int, "memory_floor_gb": float,
           "universe_refresh": lambda v: str(v).lower() in ("1", "true", "yes", "on"), "interesting_count": int,
           "size_by_coverage": lambda v: str(v).lower() in ("1", "true", "yes", "on")}


def clean(o):
    """Make a value JSON safe: numpy types to Python, NaN and inf to null."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _ema(prev: float | None, value: float, a: float) -> float:
    return value if prev is None else (1 - a) * prev + a * value


def summarize_holds(suggestions: dict[str, dict]) -> list[dict]:
    """Summarize why Steel is holding for each symbol into a count-by-reason list.

    Takes per-symbol suggestion dicts (with "reasons" and "muted" keys) and returns
    a list of {"reason": label, "n": count} sorted by count descending. Parenthesised
    suffixes like "(3/8 calls)" are stripped from the label.

    A muted symbol (muted: True with reasons ["muted"]) counts under "muted".
    """
    counts: dict[str, int] = {}
    for sym, sug in suggestions.items():
        if not isinstance(sug, dict):
            continue
        if sug.get("muted") is True and sug.get("reasons") == ["muted"]:
            counts["muted"] = counts.get("muted", 0) + 1
            continue
        reasons = sug.get("reasons") or []
        for r in reasons:
            # Strip parenthesised suffix: "no track record (3/8 calls)" -> "no track record"
            label = re.sub(r"\s*\([^)]*\)\s*$", "", r).strip()
            counts[label] = counts.get(label, 0) + 1
    # Sort by count descending
    return [{"reason": r, "n": n} for r, n in sorted(counts.items(), key=lambda x: -x[1])]


COINGECKO_SERVICE = {"id": "coingecko", "name": "CoinGecko", "file": "coingecko.json", "kind": "json",
                     "url": "https://www.coingecko.com/en/api/pricing", "venue": "data",
                     "note": "Optional. A free demo key raises the rate limit for the market radar and the aggregate price feed.",
                     "fields": [{"id": "key", "label": "Demo API key"}]}
HISTORY_FILE = "account_history.jsonl"
SCORECARD_FILE = "scorecard.jsonl"         # the model's daily scorecard for Research, one row per UTC day
FEE_TIERS_FILE = "fees.json"               # each configured venue's real fee tier, refreshed daily


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _flat(d: dict, prefix: str = "") -> dict:
    """{"a": {"b": 1}, "legs": [{"v": "x"}]} -> {"a.b": 1, "legs": "[...]"}: one CSV column per scalar."""
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v)
        else:
            out[key] = v
    return out


def _trade_service(cls) -> dict:
    """A trade key's panel entry: id `<venue>-trade`, its own gitignored file, never the read-only key."""
    d = cls.describe()
    return {**d, "id": f"{cls.id}-trade", "name": f"{cls.label} trade key", "file": cls.file(), "venue": "trade",
            "url": "", "note": f"Execution only. A key with TRADE permission (no withdrawals) for {cls.label}; used only when "
                               f"execution is on and the venue is armed. Keep it separate from the read-only key."}


# every read-only account reader (steel/accounts), the one data key, and every execution client's trade key;
# the panel, set_key and the tests walk this
KEY_SERVICES = [cls.describe() for cls in ACCOUNTS] + [COINGECKO_SERVICE] + [_trade_service(cls) for cls in EXECUTORS] + [_alerts.SERVICE]


# upper-case words in headlines that look like tickers and are not
NOT_TICKERS = TICKER_STOPLIST | {"CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETFS", "GDP", "CPI", "PPI", "PCE", "FED", "FOMC", "SEC", "USA",
                                 "NYSE", "EPS", "YTD", "PMI", "OPEC", "GOP", "DOJ", "FTC", "FDA", "NFL", "NBA", "MLB", "NATO", "LLC",
                                 "INC", "ESG", "AND", "THE", "FOR", "NEW", "TOP", "BUY", "SELL", "HOLD", "USD", "EUR", "GBP", "JPY",
                                 "ECB", "BOJ", "BOE", "IMF", "WHO", "WSJ", "CNBC", "AI", "EV", "UK", "US", "EU", "Q1", "Q2", "Q3", "Q4",
                                 "H1", "H2", "FY", "YOY", "QOQ", "MOM", "WOW", "ATH", "DCF", "DEFI", "NFT", "NFTS", "DAO", "DEX", "CEX",
                                 "TVL", "APY", "APR", "RWA", "ICO", "IDO", "KYC", "AML", "OTC", "CME", "API", "AWS", "GPU", "YES", "NOT",
                                 "ARE", "YOU", "ITS", "HAS", "WAS", "CAN", "MAY", "OUT", "OFF", "SET", "PRO", "MAX", "WEB3", "WEB"}
_TICKER_RX = re.compile(r"\$?\b[A-Z][A-Z0-9]{1,6}\b")


def headline_tickers(headlines: list[dict], known: set[str]) -> dict[str, int]:
    """Names named in headline text: an upper-case word that is a known base (on the radar), as
    long as it is three or more letters and not an ordinary acronym, or any known base written
    with a dollar sign ("$OP", "$ZK"). The organic way a new name shows up: the press is talking
    about it."""
    counts: dict[str, int] = {}
    for h in headlines or []:
        for tok in set(_TICKER_RX.findall(str(h.get("title") or ""))):
            dollar = tok.startswith("$")
            tok = tok.lstrip("$")
            if tok in known and (dollar or (len(tok) >= 3 and tok not in NOT_TICKERS)):
                counts[tok] = counts.get(tok, 0) + 1
    return counts


def rank_scores(ordered: list[str], span: int) -> dict[str, float]:
    """100 for the first name, falling linearly to 0 at position `span`: one scale for every
    candidate source, so a top mover, the most-mentioned name and a fund's biggest position
    rank as equally interesting."""
    span = max(1, span)
    return {s: max(0.0, 100.0 * (1.0 - i / span)) for i, s in enumerate(ordered)}


def liquid_universe(core: list[str], held, current: list[str], volumes: dict[str, float], record: dict,
                    *, floor: float, max_universe: int, today: str, grace_days: int = 14, min_tenure_days: int = 5):
    """The universe in liquid mode: the core and every holding always, and every listed dollar
    pair whose daily dollar volume clears `floor`, by volume, in the room under max_universe.
    `pick_universe` does the bookkeeping with the floor as the only ranking: a name that drops
    below it leaves the candidate set and is demoted after the grace period, so a thin day does
    not churn the universe (every demotion forfeits a learned row). Stables are never modeled."""
    cands = {s: math.log10(max(dv, 10.0)) for s, dv in volumes.items()
             if dv >= floor and "-" in s and asset_class(s) == "crypto"}
    keep = set(core) | set(held)
    current = [s for s in current if s in keep or asset_class(s) == "crypto"]   # a stable or a fiat pair never earns a grace period
    fixed = len(keep)
    room = max(0, max_universe - fixed)
    syms, rec, promoted, demoted = pick_universe(core, held, current, cands, record, count=room, max_universe=max_universe, today=today,
                                                 grace_days=grace_days, min_tenure_days=min_tenure_days)
    return syms, rec, promoted, demoted


def pick_universe(core: list[str], held, current: list[str], candidates: dict[str, float], record: dict,
                  *, count: int, max_universe: int, today: str, grace_days: int = 14, min_tenure_days: int = 5):
    """The universe Steel should model: the configured core and every holding always, plus the
    `count` most interesting names in the room left under max_universe, with a promotion and
    a demotion path. A name is promoted when it ranks in the top `count` candidates. Once in,
    it stays while it keeps ranking inside the top 1.5 x count (`last_hit`), and is demoted
    only after `grace_days` without a hit and `min_tenure_days` of tenure, because every
    demotion forfeits that name's learned row and history. Returns (symbols, record, promoted,
    demoted); `record` maps interesting names to their since/last_hit dates."""
    fixed: list[str] = []
    for s in list(core) + sorted(set(held) - set(core)):
        if s not in fixed:
            fixed.append(s)
    fixed = fixed[:max_universe]
    room = max(0, min(int(count), max_universe - len(fixed)))
    record = {s: v for s, v in (record or {}).items() if s in current}   # names that never made it in carry no record
    ranked = [s for s, _ in sorted(candidates.items(), key=lambda kv: -kv[1]) if s not in fixed]
    band = set(ranked[:int(room * 1.5)])
    today_d = datetime.fromisoformat(today).date()
    rec = {s: dict(v) for s, v in (record or {}).items()}
    interesting = [s for s in current if s not in fixed]
    keep, demoted = [], []
    for s in interesting:
        r = rec.setdefault(s, {"since": today, "last_hit": today})
        if s in band:
            r["last_hit"] = today
        idle = (today_d - datetime.fromisoformat(r.get("last_hit", today)).date()).days
        tenure = (today_d - datetime.fromisoformat(r.get("since", today)).date()).days
        if idle > grace_days and tenure >= min_tenure_days:
            demoted.append(s)
        else:
            keep.append(s)
    if len(keep) > room:                                     # the room shrank (holdings grew): the weakest go first
        keep.sort(key=lambda s: -candidates.get(s, 0.0))
        demoted += keep[room:]
        keep = keep[:room]
    promoted = []
    for s in ranked:
        if len(keep) + len(promoted) >= room:
            break
        if s not in keep and s not in demoted:
            promoted.append(s)
            rec[s] = {"since": today, "last_hit": today}
    for s in demoted:
        rec.pop(s, None)
    for s in list(rec):
        if s in fixed:
            rec.pop(s, None)
    final_set = set(fixed) | set(keep) | set(promoted)
    out = [s for s in current if s in final_set]            # retained names keep their place
    out += [s for s in fixed + keep + promoted if s not in out]
    return out, rec, promoted, demoted


def memory_low(device: str, floor_gb: float, reader=free_memory_gb, settle: float = 3.0) -> float | None:
    """The free memory figure when it sits under `floor_gb` on two readings `settle` seconds
    apart, else None. One reading is not enough: a compile's autotuning or a cache release in
    flight can dip free memory for a moment, and a stop on that moment threw a weekend run away
    on 2026-10-10 two minutes in."""
    f = reader(device)
    if f is None or f >= floor_gb:
        return None
    time.sleep(settle)
    g = reader(device)
    if g is None or g >= floor_gb:
        return None
    return min(f, g)


def backtest_preset_for(free_gb: float | None, live_d_model: int, live_peak_gb: float, *, floor: float = 10.0, presets=PRESETS,
                        fallback=None):
    """The widest preset, no wider than the live one, whose training peak fits in the memory free
    right now with `floor` GB left for everything else. A training peak scales about linearly
    with d_model (Flint's 2026-10-10 ladder at 232 names: 768 -> 51 GB, 512 -> 34, 384 -> 25), so
    the live preset's measured peak is scaled by width. None when nothing fits. With no free-memory
    figure (a CPU box) the live preset is returned. A live width that is no preset (auto-sizing
    off: the config's own d_model) uses `fallback`, the live net's own shape, never the top rung:
    Flint's version fell through to 5XL there, and a CPU smoke run of Steel spent its first
    minute building a 768-wide net. Exists because two 5XL nets training in one 121 GB box took
    the Spark down on 2026-10-10."""
    live = next((p for p in presets if p[1] == live_d_model), None)
    if live is None:
        live = tuple(fallback) if fallback else presets[0]
        presets = sorted([*presets, live], key=lambda p: p[1])
    if free_gb is None or live_peak_gb <= 0:
        return live
    for p in reversed(presets):
        if p[1] > live_d_model:
            continue
        need = live_peak_gb * (p[1] / float(live_d_model)) * 1.25 + floor
        if need <= free_gb:
            return p
    return None


def _stalled(vals: list[float], patience: int) -> bool:
    """True when the last `patience` checks brought no improvement over the best before them."""
    if patience <= 0 or len(vals) <= patience:
        return False
    return min(vals[-patience:]) >= min(vals[:-patience])


def _mask(v: str) -> str:
    v = (v or "").strip()
    return (v[:2] + "***" + v[-2:]) if len(v) >= 6 else ("***" if v else "")


@dataclass(slots=True)
class Pending:
    index: int
    ts: float
    x: np.ndarray
    closes: np.ndarray
    real: np.ndarray
    pred: Prediction | None
    suggestions: dict[str, dict]


@dataclass
class Metrics:
    labels: int = 0
    steps: int = 0
    loss: float | None = None
    pinball: float | None = None
    bce: float | None = None
    decisions: int = 0
    hits: int = 0
    hit_ema: float | None = None
    coverage_n: int = 0
    covered: int = 0
    coverage_ema: float | None = None
    sharpness_ema: float | None = None
    suggestions: int = 0
    suggestion_wins: int = 0
    pnl_bps: float = 0.0
    pnl_by_symbol: dict = field(default_factory=dict)
    trusted: bool = False
    trusted_by: str = ""                   # "live" (min_labels live labels) or "held-out" (the history scorecard), else ""
    live_labels: int = 0
    live_pinball: float | None = None
    holdout_hit: float | None = None       # the held-out scorecard over the last trust_days: direction hit rate, calls, 10-90 coverage
    holdout_calls: int = 0
    holdout_coverage: float | None = None
    coverage_raw_n: int = 0
    covered_raw: int = 0
    holdout_pinball: float | None = None   # loss on the 5% of windows the trainer never samples
    holdout_bce: float | None = None
    band_scale: float = 2.0   # conformal multiplier on the predicted band widths; starts wide, earns its way down
    p_scale: float = 0.3      # temperature on the direction logit (1 = raw); starts sceptical

    def as_dict(self) -> dict:
        return {
            "labels": self.labels, "steps": self.steps, "loss": self.loss, "pinball": self.pinball, "bce": self.bce,
            "decisions": self.decisions, "hit_rate": self.hits / self.decisions if self.decisions else None,
            "hit_ema": self.hit_ema, "coverage": self.covered / self.coverage_n if self.coverage_n else None,
            "coverage_ema": self.coverage_ema, "sharpness": self.sharpness_ema,
            "holdout_pinball": self.holdout_pinball, "holdout_bce": self.holdout_bce,
            "suggestions": self.suggestions, "win_rate": self.suggestion_wins / self.suggestions if self.suggestions else None,
            "pnl_bps": self.pnl_bps, "pnl_by_symbol": dict(self.pnl_by_symbol), "trusted": self.trusted, "trusted_by": self.trusted_by,
            "live_labels": self.live_labels, "live_pinball": self.live_pinball,
            "holdout_hit": self.holdout_hit, "holdout_calls": self.holdout_calls, "holdout_coverage": self.holdout_coverage,
            "coverage_raw": self.covered_raw / self.coverage_raw_n if self.coverage_raw_n else None,
            "band_scale": self.band_scale, "p_scale": self.p_scale,
        }


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.all_symbols = [canonical(s) for s in cfg.symbols]  # full configured universe
        self.core_symbols = list(self.all_symbols)             # the configured names: always modeled
        self._held: set[str] = set()                           # latest holdings (portfolio loop): always modeled
        self._universe_refreshed = ""                          # UTC date of the last promotion/demotion pass
        self._interesting: dict = {}                           # name -> {since, last_hit} for promoted names
        self._built_universe: list[str] = []                   # the full list the current model was built for
        # Merge the persisted universe (state/universe.json): symbols added at runtime by the
        # portfolio/movers/news loops must survive a restart, or the rebuilt
        # engine starts at the config default, mismatches the checkpoint's symbol list, and
        # resets the trained model. Delete the file to intentionally shrink the universe.
        try:
            uni = json.load(open(os.path.join(cfg.state_dir, "universe.json")))
            saved = [canonical(s) for s in uni.get("symbols", [])]
            extra_syms = [s for s in saved if s not in set(self.all_symbols) and asset_class(s) == "crypto"]
            room = cfg.max_universe - len(self.all_symbols)
            self.all_symbols.extend(extra_syms[:max(0, room)])
            self._universe_refreshed = str(uni.get("refreshed") or "")
            self._interesting = dict(uni.get("interesting") or {})
        except (OSError, ValueError):
            pass
        self.muted = {x.strip().upper() for x in cfg.muted_symbols.split(",") if x.strip()} & set(self.all_symbols)
        self.subs: set[asyncio.Queue] = set()
        self.trace = Trace(self._publish)
        self.vault = _vault.get(cfg.state_dir)                 # every key file sealed at rest; plaintext ones found here are sealed in place
        self.handoff = Handoff(cfg)
        self.fee_tiers: dict[str, dict] = {}                 # venue -> its real fee rates (bps per leg) from its reader, once a day
        self.token, fresh_token = _auth.load(cfg.state_dir, str(getattr(cfg, "token", "") or ""))
        if fresh_token:
            log.warning("operator token generated and sealed in %s/token.json; `steel token` prints it (the dashboard asks for it once)", cfg.state_dir)
        sealed = self.vault.seal_all([svc["file"] for svc in KEY_SERVICES])
        self.alerts = _alerts.Alerts(self.vault.read_json(Path(_alerts.SERVICE["file"])) if Path(_alerts.SERVICE["file"]).exists() else {})
        self.trace.on_emit = self.alerts.on_trace
        self._alerted_action: dict[str, str] = {}
        if sealed:
            log.warning("vault: sealed %d plaintext key file(s): %s", len(sealed), ", ".join(sealed))
        self.accounts = {cls.id: cls(**read_creds(cls.id, [f["id"] for f in cls.fields])) for cls in ACCOUNTS}   # read-only readers, one per venue
        self.log: deque[dict] = deque(maxlen=cfg.log_size)
        self.holdout_outcomes: dict[str, list[bool]] = {}   # per-name hits on held-out history, the skill gate's seed
        self.operator: dict[str, dict] = {}                 # per-symbol human bias {sent, attn, t, text}
        self.operator_notes: deque[dict] = deque(maxlen=50)  # raw injected notes, for display
        self.status = "starting"
        self.started = time.time()
        self.learning_enabled = True
        self.overlay_enabled = cfg.overlay_enabled
        self.brief_enabled = cfg.brief_enabled
        self.news: dict = {"t": None, "method": None, "status": {}, "headlines": [], "per_asset": {}, "ideas": [], "new": 0}
        self.market_status: dict = clock.session_label()   # always open; carries the UTC day for the dashboard
        self.backtest: dict = {}                  # last walk-forward report (state/backtest.json)
        self.backtest_progress: dict = {"running": False}
        self._backtesting = False
        self._backtest_requested = False
        self._backtest_stop = False
        self._backtest_stop_reason = ""
        self._backtest_waiting = False
        self._lowmem_said = False
        self._ring_full_said = False
        try:
            with open(os.path.join(self.cfg.state_dir, "backtest.json")) as f:
                self.backtest = json.load(f)
        except (OSError, ValueError):
            pass
        self.sweep: dict = {}                     # the last policy sweep over a backtest's stored forecasts (state/sweep.json)
        try:
            with open(os.path.join(self.cfg.state_dir, "sweep.json")) as f:
                self.sweep = json.load(f)
        except (OSError, ValueError):
            pass
        self.brief: dict = {"text": None, "takes": {}, "models": {}, "t": None, "generating": False, "error": None}
        self.autotune_info = None
        self._skimming = False
        self._gathering = False
        self._reconfiguring = False
        self._tasks: list[asyncio.Task] = []
        self._lifecycle: asyncio.Task | None = None
        self._build(self._active())

    def _active(self) -> list[str]:
        a = [s for s in self.all_symbols if s not in self.muted]
        return a or self.all_symbols[:1]

    def _build(self, symbols: list[str]) -> None:
        """(Re)build all per-symbol state for the active universe. Muted symbols are left
        out entirely, so they cost no model compute and no data/API calls."""
        cfg = self.cfg
        cfg.symbols = list(symbols)                            # active set drives model + feeds
        self.symbols = list(symbols)
        self.base = {s: s.split("-")[0] for s in self.symbols}
        if cfg.auto_size:
            from .autotune import autotune
            self.autotune_info = autotune(cfg, N_FEATURES, say=lambda m: print("  [tune]", m, flush=True))
            ai = self.autotune_info
            cfg.device, cfg.torch_threads = ai["device"], ai["threads"]
            cfg.d_model, cfg.dilations = ai["d_model"], tuple(ai["dilations"])
            cfg.n_experts, cfg.n_heads, cfg.window = ai["n_experts"], ai["n_heads"], ai["window"]
        else:
            from .autotune import pick_device
            cfg.device = pick_device(cfg.device)
        self.builder = BarBuilder(self.symbols, cfg.bar_seconds)
        self.features = FeatureBuilder(self.symbols, cfg.window)
        self.news_base = {s: (0.0, 0.0) for s in self.symbols}  # last pure news skim, before operator blend
        self.learner = OnlineLearner(cfg, N_FEATURES, features=FEATURES)
        self.sources = SourceManager(cfg, self.symbols, on_tick=self._on_live_tick, state_path=Path(cfg.state_dir) / "sources.json",
                                     on_provider_change=self._on_provider_change, trace=self.trace, on_quote=self._on_quote,
                                     on_book=self._on_book)
        self.identity = IdentityBook(cfg.state_dir, getattr(cfg, "coingecko_key", ""))
        self.sources.identity_ok = self._identity_ok
        self.execution = ExecutionGate(cfg, cfg.state_dir)
        self.executors = {cls.id: cls.load(self.execution, dry_run=bool(getattr(cfg, "execution_dry_run", True)),
                                           testnet=bool(getattr(cfg, "execution_testnet", False))) for cls in EXECUTORS}
        self.promotion = Promotion(cfg)
        self.live = LiveBook(cfg, self.executors, self.execution, cfg.state_dir, trace=self.trace)
        self.dislocations = DislocationMonitor(self._taker_bps, min_edge_bps=float(getattr(cfg, "dislocation_min_bps", 0.0) or 0.0),
                                               latency_bps=float(getattr(cfg, "dislocation_latency_bps", 0.0) or 0.0),
                                               stale_s=float(getattr(cfg, "dislocation_stale_s", 10.0) or 10.0),
                                               log_path=Path(cfg.state_dir) / "dislocations.jsonl")
        self.arb = ArbExecutor(cfg, self.executors, self.execution, venue_kind={vid: src.venue for vid, src in self.sources.sources.items()},
                               state_dir=cfg.state_dir, trace=self.trace)
        self.dislocations.on_open = self.arb.consider
        self.dislocations.on_close = self.arb.on_close
        self.news_hub = NewsHub(cfg, self.symbols)
        self.signals = SignalHub(cfg, self.symbols)
        self.prices = {s: {"price": None, "bid": None, "ask": None, "ts": None} for s in self.symbols}
        self.tick_counts = {s: 0 for s in self.symbols}
        self.bars: dict[str, deque[Bar]] = {s: deque(maxlen=cfg.chart_bars) for s in self.symbols}
        self.funding: dict[str, float] = {}               # current per-interval perp funding rate per name: the policy's carry and the book's accrual
        self._cooldown: dict[str, int] = {}               # bar index of a name's last exit (stop, target, time limit)
        self._day_open_equity: tuple[str, float] | None = None   # (UTC date, book equity at the day's first bar) for the daily loss limit
        self._book_equity = 0.0
        self._streak: dict[str, tuple[int, int]] = {}    # symbol -> (raw side, consecutive bars) for the confirmation gate
        self._held_since: dict[str, int] = {}             # symbol -> bar index a nonzero paper target began
        self._last_target: dict[str, float] = {}
        # the venues a position would trade on: their fee profiles are what the policy charges against an edge and the book on a fill
        self.fees = {"spot": fee_profile(getattr(cfg, "trade_venue_spot", "coinbase")), "perp": fee_profile(getattr(cfg, "trade_venue_perp", "hyperliquid"))}
        self.paper = PaperBook(cfg.paper_start, self.spot_fee_bps(), min_trade_frac=cfg.paper_min_trade_frac,
                               perp_fee_bps=self.perp_fee_bps(), short_max_frac=cfg.short_max_frac,
                               short_leverage=cfg.short_leverage, short_min_hold_s=cfg.short_min_hold_bars * cfg.bar_seconds,
                               funding_interval_s=self.funding_hours() * 3600.0, long_mode=str(getattr(cfg, "long_mode", "spot")),
                               long_leverage=float(getattr(cfg, "long_leverage", 1.0) or 1.0),
                               maintenance=self.fees["perp"].perp_maintenance,   # $100k paper book: perps are margined, every loss capped
                               venues=(str(getattr(cfg, "trade_venue_spot", "coinbase")), str(getattr(cfg, "trade_venue_perp", "hyperliquid"))),
                               perp_cash_frac=float(getattr(cfg, "paper_perp_cash_frac", 0.5) or 0.0), transfer=transfer_cost,
                               transfer_asset=str(getattr(cfg, "transfer_asset", "USDC")), min_transfer_usd=float(getattr(cfg, "min_transfer_usd", 250.0) or 0.0))
        self.portfolio: dict = {}                               # exchange account balances (read-only), for the Portfolio tab
        self.bar_index = 0
        self.pending: deque[Pending] = deque()
        self.latest: dict[str, dict] = {}
        self.gate: list[float] = []
        self.outcomes = {s: deque(maxlen=40) for s in self.symbols}
        self.history = {k: deque(maxlen=240) for k in ("loss", "hit", "coverage", "pnl")}
        self._history_new: dict[str, list] = {k: [] for k in self.history}
        self.metrics = Metrics(pnl_by_symbol={s: 0.0 for s in self.symbols})
        self.signals_state = self.signals.state
        self.crowding: dict[str, float] = {}

    def start(self) -> None:
        self._lifecycle = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._lifecycle:
            self._lifecycle.cancel()
        for t in self._tasks:
            t.cancel()
        await self.sources.stop()
        await asyncio.to_thread(self._save, True)

    async def reconfigure(self) -> None:
        """Rebuild for the current active universe (all_symbols minus muted) and restart
        the lifecycle. Muted / removed symbols then cost no compute or API calls."""
        if self._reconfiguring:
            return
        self._reconfiguring = True
        try:
            self.muted &= set(self.all_symbols)
            if self._backtesting:                                   # its net shares the GPU: a benchmark next to it reads nonsense peaks
                self._backtest_stop = True
                self.trace.emit("system", "universe change: stopping the backtest first", "act")
            for t in self._tasks:
                t.cancel()
            if self._lifecycle:
                self._lifecycle.cancel()
            try:
                await self.sources.stop()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.15)
            for _ in range(900):                                    # the worker stops at its next day boundary
                if not self._backtesting:
                    break
                await asyncio.sleep(1)
            self._tasks = []
            self.status = "starting"
            prev = (list(self.symbols), list(self._built_universe or self.all_symbols), dict(self.base), list(self.cfg.symbols))
            try:
                await asyncio.to_thread(self._save, True)           # the replay in memory is hours of paging; keep it
            except Exception:  # noqa: BLE001
                log.exception("save before rebuild failed")
            self.learner.release_replay()                            # the new learner allocates its own; load() migrates from disk
            self.learner.release_cache()
            try:
                await asyncio.to_thread(self._build, self._active())   # benchmark/build off the event loop
                self._built_universe = list(self.all_symbols)          # the full list the model now matches
                self.trace.emit("system", f"universe rebuilt: {len(self.symbols)} active "
                                          f"({len(self.muted)} muted) — {', '.join(self.base[s] for s in self.symbols)}", "act")
            except Exception as e:  # noqa: BLE001
                # A rebuild that fails (no memory to benchmark, a bad symbol) must not leave the
                # engine with every loop cancelled and no lifecycle, as it did on 2026-10-10.
                # Put the previous universe back and resume with the model we still have.
                log.exception("universe rebuild failed")
                self.symbols, self.all_symbols, self.base, self.cfg.symbols = prev
                self.muted &= set(self.all_symbols)
                self._interesting = {k: v for k, v in self._interesting.items() if k in self.all_symbols}
                self.trace.emit("system", f"universe rebuild failed: {type(e).__name__}: {str(e)[:160]}; "
                                          f"keeping the previous {len(self.symbols)} names", "error")
            self._publish(self.snapshot())                         # UI rebuilds cards for the new set
            self._lifecycle = asyncio.create_task(self.run())
        finally:
            self._reconfiguring = False

    # Pub/sub ------------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def _publish(self, msg: dict) -> None:
        if not self.subs:
            return
        text = json.dumps(clean(msg), separators=(",", ":"))
        for q in list(self.subs):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                pass

    def _hpush(self, key: str, value) -> None:
        self.history[key].append(value)
        self._history_new[key].append(value)

    def _drain_history(self) -> dict[str, list]:
        out = {k: v for k, v in self._history_new.items() if v}
        self._history_new = {k: [] for k in self.history}
        return out

    def _log(self, text: str, kind: str = "info") -> None:
        self.log.append({"t": time.time(), "kind": kind, "text": text})

    _BULL = {"buy", "bull", "bullish", "long", "longs", "breakout", "moon", "squeeze", "rip", "rally",
             "pump", "strong", "beat", "beats", "upgrade", "surge", "calls", "bounce", "green", "add"}
    _BEAR = {"sell", "bear", "bearish", "short", "shorts", "crash", "dump", "puke", "fade", "tank",
             "weak", "miss", "misses", "downgrade", "drop", "puts", "risk", "red", "trim", "avoid"}

    def _op_effective(self, sym: str):
        """Current decayed operator bias for a symbol, or None once it has faded out."""
        op = self.operator.get(sym)
        if not op:
            return None
        decay = 0.5 ** ((time.time() - op["t"]) / max(60.0, self.cfg.operator_half_life))
        if decay < 0.05:
            self.operator.pop(sym, None)
            return None
        return op["sent"], op["attn"] * decay

    def _blend_news(self, sym: str, base_sent: float, base_attn: float):
        """Blend a human note over the news skim: attention takes the max, sentiment is
        pulled toward the note in proportion to its (decayed) attention."""
        eff = self._op_effective(sym)
        if not eff:
            return base_sent, base_attn
        osent, oattn = eff
        return base_sent * (1 - oattn) + osent * oattn, max(base_attn, oattn)

    def inject_note(self, text: str) -> dict:
        """Human input from the dashboard: log it to the operator console and, for any
        watched ticker it names, nudge that symbol's news sentiment/attention feature."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        self.operator_notes.append({"t": time.time(), "text": text})
        self.trace.emit("operator", text, "act")
        self._log("note: " + text, "operator")

        words = [w.lower() for w in re.findall(r"[A-Za-z']+", text)]
        hits = [w for w in words if w in self._BULL or w in self._BEAR]
        score = sum(1 for w in words if w in self._BULL) - sum(1 for w in words if w in self._BEAR)
        sent = max(-1.0, min(1.0, score / 2.0)) if hits else 0.0

        bases = {self.base[s]: s for s in self.symbols}       # NVDA->NVDA, XAU->XAU-USD
        mentioned, seen = [], set()
        for dollar, tok in re.findall(r"(\$?)([A-Za-z][A-Za-z.\-]{0,6})", text):
            u = tok.upper().strip(".-")
            if u in bases and bases[u] not in seen and (dollar or tok.isupper() or len(tok) >= 3):
                mentioned.append(bases[u]); seen.add(bases[u])

        now, applied = time.time(), []
        for sym in mentioned:
            self.operator[sym] = {"sent": sent, "attn": 0.85, "t": now, "text": text}
            es, ea = self._blend_news(sym, *self.news_base.get(sym, (0.0, 0.0)))
            self.features.set_news(sym, es, ea)
            applied.append(sym)
        if applied:
            self.trace.emit("features", f"operator bias on {', '.join(applied)}: sentiment {sent:+.2f}, "
                            f"attention 85%" + (f" ({', '.join(sorted(set(hits)))})" if hits else " (attention only)"), "act")
        elif not mentioned:
            self.trace.emit("system", "note logged as context (named no watched ticker)", "info")
        self._publish({"type": "operator", "operator": self.operator_state()})
        return {"ok": True, "applied": applied, "sentiment": round(sent, 3)}

    def operator_state(self) -> dict:
        return {"notes": list(self.operator_notes)[-20:],
                "bias": {s: {"sent": o["sent"], "text": o["text"], "t": o["t"]} for s, o in self.operator.items()}}

    def _live_prices(self) -> dict:
        return {s: self.prices[s]["price"] for s in self.symbols if self.prices.get(s) and self.prices[s].get("price")}

    def status_dict(self) -> dict:
        providers = self.sources.provider_map()
        active = sorted({p for p in providers.values() if p})
        return {"phase": self.status, "feed": ", ".join(active) or "none", "providers": providers,
                "started": self.started, "now": time.time(), "bar_index": self.bar_index,
                "pending": len(self.pending), "learning": self.learning_enabled, "subscribers": len(self.subs),
                "market_status": clock.session_label(),
                "stalled": bool(getattr(self, "_stalled", False)),
                "last_bar_age": round(time.time() - getattr(self, "_last_row_at", time.time())),
                "warmup": getattr(self, "warmup", None),
                "handoff": self.handoff.status() if getattr(self, "handoff", None) is not None else None,
                "fees": {cls: {"venue": p.venue, "spot_taker_bps": p.spot_taker_bps, "spot_maker_bps": p.spot_maker_bps, "perp_taker_bps": p.perp_taker_bps,
                               "perp_maker_bps": p.perp_maker_bps, "live": p.venue in getattr(self, "fee_tiers", {})} for cls, p in (getattr(self, "fees", None) or {}).items()},
                "target": self.target_info()}

    def target_info(self) -> dict:
        """What the net forecasts: each name's return, or its return relative to the index."""
        L = self.learner
        return {"mode": L.target_mode, "configured": str(self.cfg.target), "index": [self.symbols[c] for c in L.cols],
                "configured_index": [s.strip().upper() for s in str(self.cfg.index_symbols).split(",") if s.strip()],
                "hedge": bool(L.cols and self.cfg.hedge_index)}

    def controls(self) -> dict:
        return {k: getattr(self.cfg, k) for k in TUNABLE} | {"learning": self.learning_enabled, "overlay": self.overlay_enabled, "brief_enabled": self.brief_enabled}

    def _key_values(self) -> dict:
        # One entry per KEY_SERVICES id, or the panel shows a saved key as "not set" forever.
        vals = {sid: acct.values() for sid, acct in self.accounts.items()}
        vals["coingecko"] = {"key": self.cfg.coingecko_key}
        for vid, ex in getattr(self, "executors", {}).items():
            vals[f"{vid}-trade"] = {f["id"]: getattr(ex, f["id"], "") for f in ex.fields}
        vals["alerts"] = self.alerts.values()
        return vals

    def keys_status(self) -> list:
        vals = self._key_values()
        out = []
        for svc in KEY_SERVICES:
            v = vals.get(svc["id"], {})
            fields = [{"id": f["id"], "label": f["label"], "present": bool(v.get(f["id"])), "multiline": bool(f.get("multiline")),
                       "masked": _mask(v.get(f["id"], ""))} for f in svc["fields"]]
            acct = self.accounts.get(svc["id"]) or getattr(self, "executors", {}).get(svc["id"].removesuffix("-trade")) if svc["id"].endswith("-trade") else self.accounts.get(svc["id"])
            if svc["id"] == "alerts":
                acct = self.alerts                      # configured = any one destination
            out.append({"id": svc["id"], "name": svc["name"], "url": svc["url"], "note": svc["note"], "venue": svc.get("venue", ""),
                        "present": acct.configured if acct is not None else all(f["present"] for f in fields), "fields": fields})
        return out

    def set_key(self, service: str, values: dict) -> dict:
        import json as _json
        svc = next((x for x in KEY_SERVICES if x["id"] == service), None)
        if not svc:
            return {"error": "unknown service"}
        vals = {f["id"]: str(values.get(f["id"], "")).strip() for f in svc["fields"]}
        if not any(vals.values()):
            return {"error": "no value provided"}
        path = Path(svc["file"])
        vault = getattr(self, "vault", None) or _vault.get(self.cfg.state_dir)
        try:
            if svc["kind"] == "json":
                vault.write_json(path, {"key": vals["key"]})
            else:  # json_multi (every account reader and every trade key)
                existing = vault.read_json(path) if path.exists() else {}
                existing.update({k: v for k, v in vals.items() if v})
                vault.write_json(path, existing)
        except OSError as e:
            return {"error": f"could not write {svc['file']}: {e.__class__.__name__}"}
        self._apply_key(service, vals)
        self.trace.emit("system", f"{svc['name']} key saved to {svc['file']} (sealed)", "act")
        self._publish({"type": "keys", "keys": self.keys_status()})
        return {"ok": True, "keys": self.keys_status()}

    def _apply_key(self, service: str, vals: dict) -> None:
        """Live reload so a saved key takes effect without a restart: the account readers and
        the CoinGecko scanner copy their keys at construction, so push the new value into each."""
        cfg = self.cfg
        if service == "alerts":
            self.alerts.configure(vals)
            self.alerts.send("info", "alerts configured: this is Steel saying hello", key="hello")
            return
        if service.endswith("-trade"):
            ex = getattr(self, "executors", {}).get(service.removesuffix("-trade"))
            if ex is not None:
                for f in ex.fields:
                    v = str(vals.get(f["id"]) or "")
                    if v.strip():
                        setattr(ex, f["id"], v.replace("\\n", "\n") if f.get("multiline") else v.strip())   # a PEM pasted on one line
            return
        acct = self.accounts.get(service)
        if acct is not None:
            acct.configure(vals)
            if service == "coinbase":
                cfg.coinbase_creds = (acct.key_name, acct.private_key)
            elif service == "kraken":
                cfg.kraken_creds = (acct.key, acct.secret)
        elif service == "coingecko":
            cfg.coingecko_key = vals.get("key", "")
            self.signals.market.key = cfg.coingecko_key          # the source reads cfg on every call

    def snapshot(self) -> dict:
        cfg = self.cfg
        return {
            "type": "snapshot",
            "config": {
                "symbols": self.symbols, "bar_seconds": cfg.bar_seconds, "horizon": cfg.horizon, "horizons": list(cfg.horizons), "window": cfg.window, "max_universe": cfg.max_universe, "chart_bars": cfg.chart_bars,
                "quantiles": list(cfg.quantiles), "min_labels": cfg.min_labels, "features": FEATURES,
                "model": {"params": self.learner.n_params, "d_model": cfg.d_model, "experts": cfg.n_experts,
                          "dilations": list(cfg.dilations), "heads": cfg.n_heads, "window": cfg.window,
                          "receptive_field": self.learner.model.receptive_field,
                          "device": cfg.device, "ms_per_step": (self.autotune_info or {}).get("ms_per_step"),
                          "preset": (self.autotune_info or {}).get("preset")},
            },
            "status": self.status_dict(),
            "backtest": self.backtest_summary(),         # a page opened mid-run sees the run, not "no run yet"
            "controls": self.controls(),
            "sources": self.sources.status(),
            "news_sources": self.news_hub.status(),
            "signals": self.signals_state,
            "signal_providers": self.signals.status(),
            "keys": self.keys_status(),
            "overlay": {"enabled": self.overlay_enabled, "aggr": self.cfg.overlay_aggr, "fade_at": self.cfg.overlay_fade_at,
                        "safety": self.cfg.overlay_safety},
            "classes": {s: asset_class(s) for s in self.all_symbols},
            "muted": sorted(self.muted),
            "holds": summarize_holds(self.latest),
            "universe": self.all_symbols,
            "prices": self.prices,
            "bars": {s: [b.to_json() for b in self.bars.get(s, ())] for s in self.symbols},
            "latest": self.latest,
            "gate": self.gate,
            "outcomes": {s: list(self.outcomes.get(s, ())) for s in self.symbols},
            "metrics": self.metrics.as_dict(),
            "history": {k: list(v) for k, v in self.history.items()},
            "log": list(self.log),
            "trace": self.trace.recent(),
            "operator": self.operator_state(),
            "news": self.news,
            "brief": self.brief,
            "paper": self.paper.snapshot(self._live_prices()),
            "portfolio": self.portfolio,
            "dislocations": self.dislocations.stats(30),
            "execution": self.execution_status(),
            "live": self.live_status(),
            "arb": self.arb.status(),
            "vault": self.vault_status(),
            "identity": self.identity_view(),
        }

    def snapshot_json(self) -> str:
        return json.dumps(clean(self.snapshot()), separators=(",", ":"))

    # Lifecycle ------------------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._start()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("engine failed")
            self.status = "error"
            self.trace.emit("system", f"engine failed: {e}", "error")
            self._publish({"type": "status", "status": self.status_dict()})

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self._panic_on_stop()
        await self.sources.stop()
        await asyncio.to_thread(self._save)
        try:
            await asyncio.wait_for(self.alerts.flush(), 5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    async def _panic_on_stop(self) -> None:
        """A stop never leaves a resting order behind; with flatten_on_stop it closes every
        live position too. Only when execution is on and a venue is armed; a paper box skips it."""
        if not (self.execution.enabled and self.execution.armed):
            return
        try:
            if bool(getattr(self.cfg, "flatten_on_stop", False)) and (self.live.live or self.live.expected):
                res = await asyncio.wait_for(self.live.flatten(self.prices, reason="stop"), 60.0)
                self.alerts.send("live", f"stopped: flattened {len(res['closed'])} position(s), {len(res['left'])} left", level="error")
            else:
                cancelled = await asyncio.wait_for(self.live.cancel_open(), 30.0)
                if cancelled:
                    self.trace.emit("system", f"stop: {len(cancelled)} open order(s) cancelled", "act")
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            self.trace.emit("system", f"stop: could not clean up orders ({type(e).__name__})", "error")

    async def _deadman(self) -> None:
        """A live book nobody has saved for live_deadman_minutes (the box was down, the process
        hung) comes back demoted: it is flattened when execution can, else left for the operator."""
        mins = float(getattr(self.cfg, "live_deadman_minutes", 0.0) or 0.0)
        if mins <= 0 or not self.live.live:
            return
        stale = self.live.stale()
        if stale <= mins:
            return
        why = f"dead-man: the live book was last heard from {stale:.0f} min ago (limit {mins:g})"
        if self.execution.enabled and self.execution.armed:
            await self.live.flatten(self.prices, reason=why)
        else:
            self.live.demote(why)
        self.alerts.send("demoted", why, level="error")

    async def _start(self) -> None:
        cfg = self.cfg
        extra = self.learner.load(cfg.state_dir)
        if extra is not None:
            if "norm" in extra:
                kept = self.features.norm.load(extra["norm"], self.symbols)   # mapped by name: a universe change keeps every old name's stats
                if kept < len(self.symbols):
                    self.trace.emit("system", f"feature normalizer: {kept} of {len(self.symbols)} names restored, the rest start fresh", "warn" if kept == 0 else "info")
            self.metrics.labels = self.learner.labels
            self.metrics.steps = self.learner.steps
            md = extra.get("metrics") or {}
            m = self.metrics
            for k in self._PERSIST_METRICS:
                if md.get(k) is not None:
                    setattr(m, k, md[k])
            # Calibration state is versioned: values saved by an older calibration regime measured
            # the wrong thing (the pre-batch updater pinned the scales at their clips and poisoned
            # the EMAs), so a pre-v2 checkpoint re-enters with the fresh-model priors and lets live
            # labels re-earn everything. Bump CALIB_V whenever the calibration math changes shape.
            if int(md.get("calib_v") or 1) < self.CALIB_V:
                m.band_scale, m.p_scale = 2.0, 0.3
                m.hit_ema = m.coverage_ema = None
            for s, outs in (extra.get("outcomes") or {}).items():   # per-name track records survive a restart (the skill gate needs them)
                if s in self.outcomes:
                    self.outcomes[s] = deque(list(outs)[-40:], maxlen=40)
            for sym, v in (md.get("pnl_by_symbol") or {}).items():
                if sym in m.pnl_by_symbol:
                    m.pnl_by_symbol[sym] = v
            self._update_trust()
            if extra.get("paper"):
                self.paper.load_state(extra["paper"], set(self.symbols))
            self.trace.emit("system", f"restored checkpoint from {cfg.state_dir}: {self.learner.steps} steps, "
                                      f"{self.learner.labels} labels, {m.live_labels}/{cfg.min_labels} live labels, "
                                      f"{self.learner.size} bars in replay, {self.learner.n_labeled} labeled")
        self._built_universe = list(self.all_symbols)
        ai = self.autotune_info
        dev = cfg.device + (f", {ai['preset']} preset, {ai['ms_per_step']} ms/step" if ai else "")
        self.trace.emit("system", f"SteelNet: {self.learner.n_params:,} parameters on {dev}; {cfg.n_assets} assets, "
                                  f"{cfg.window} x {cfg.bar_seconds:.0f}s window, {cfg.horizon}-bar horizon, "
                                  f"{cfg.n_experts} regime experts, {cfg.n_heads} heads")

        self.status = "backfilling"
        self._publish({"type": "status", "status": self.status_dict()})
        enabled = [sid for sid, on in self.sources.enabled.items() if on]
        self.trace.emit("feed", f"sources enabled: {', '.join(enabled)}; backfilling "
                                f"{cfg.backfill_seconds / 86400:.1f} days per symbol from the best available")
        cutoff = time.time() - cfg.backfill_seconds
        self._deep_cutoff = cutoff                     # deep-backfill loop pages further back from here
        held = self.learner.rt[:self.learner.size]
        held = held[held > 0]
        if self.learner.migrated_new:                  # new names: walk the whole history again; fill() takes their columns
            self.trace.emit("system", f"universe changed: re-paging history to fill {len(self.learner.migrated_new)} new "
                                      f"name(s) into the replay ({', '.join(self.learner.migrated_new[:12])}"
                                      f"{'…' if len(self.learner.migrated_new) > 12 else ''})", "act")
        elif len(held):                                # resume where the last run's paging stopped, not from today
            self._deep_cutoff = min(self._deep_cutoff, float(held.min()))
        ticks, chosen = await self.sources.backfill(cutoff)
        if not ticks:
            self.trace.emit("feed", "no source returned history; the simulator will provide a live fallback", "warn")
        for s_id in sorted(set(chosen.values())):
            covered = ", ".join(self.base[s] for s, c in chosen.items() if c == s_id)
            self.trace.emit("feed", f"history for {covered} from {self.sources.sources[s_id].name}")
        per_sym = {sym: 0 for sym in self.symbols}
        for t in ticks:
            per_sym[t.symbol] = per_sym.get(t.symbol, 0) + 1
        self.trace.emit("feed", f"{len(ticks)} ticks backfilled (" +
                        ", ".join(f"{self.base[s]} {per_sym[s]}" for s in self.symbols) + ")")
        self._ingest_history(ticks)
        await self._warmup()
        card = await asyncio.to_thread(self.rescore_holdout)
        if card:
            self.trace.emit("learn", f"held-out scorecard over the last {float(getattr(cfg, 'trust_days', 7.0)):g} days: "
                                     f"{card['calls']} calls, {(card['hit'] or 0) * 100:.0f}% right, pinball {card['pinball']:.2f} bps, "
                                     f"10-90 coverage {(card['coverage'] or 0) * 100:.0f}%; "
                                     + ("trusted on history" if self.metrics.trusted_by == "held-out" else
                                        "trusted" if self.metrics.trusted else f"not trusted yet (needs {cfg.min_labels} calls at {cfg.min_hit_rate * 100:.0f}%)"), "act")
        await self._deadman()
        if self.vault.backup_pending:
            self.alerts.send("vault", "the vault's recovery key has not been saved yet: open the dashboard once", key="pending", level="warn")
        self._seed_forecast()
        self.status = "live"
        self._publish(self.snapshot())   # full resync: clients that connected during backfill/warmup now get the built bars (no reload needed)
        self.trace.emit("system", "live: streaming ticks, forecasting every bar, learning as labels mature")
        self.sources.start()
        self._tasks = [asyncio.create_task(c) for c in
                       (self._clock(), self._ticker(), self._checkpoints(), self._news_loop(), self._signals_loop(), self._market_loop(), self._brief_loop(), self._portfolio_loop(), self._deep_backfill_loop(), self._train_loop(), self._backtest_loop(), self._universe_loop(), self._dislocation_loop(), self._identity_loop(), self._alerts_loop(), self._clock_check_loop(), self._backup_loop(), self._handoff_loop(), self._fees_loop())]
        await asyncio.gather(*self._tasks)

    def _ingest_history(self, ticks: list[Tick]) -> None:
        for t in ticks:
            self.builder.add(t)
            self._on_tick(t, live=False)
        # Symbols with no deep history (e.g. equities on free feeds backfill one seed tick,
        # commodities a spot price) would otherwise block every aligned row and leave the
        # feature window empty until ~64 live bars pass. Seed each symbol's last close from
        # its earliest backfilled tick so historical rows emit, flat-filling the sparse ones.
        first: dict[str, float] = {}
        for t in ticks:
            first.setdefault(t.symbol, t.price)
        blind = [s for s in self.symbols if s not in first]
        if blind:                                    # a symbol with zero history would block EVERY joint row -> no bars at all
            self.trace.emit("feed", f"no backfill history for {', '.join(self.base[s] for s in blind)}; "
                                    f"flat-seeding so they don't stall the rest of the universe", "warn")
            for s in blind:
                first[s] = 0.0                       # zero seed: flat bars read as all-zero neutral features, and the first real
                                                     # bar has prev==0 so its return is 0 (no fake spike when the symbol recovers)
        for sym, px in first.items():
            self.builder.last_close.setdefault(sym, px)
        rows = self.builder.roll(time.time())
        self.trace.muted = True
        try:
            for row in rows:
                self._history_row(row)
        finally:
            self.trace.muted = False
        self.trace.emit("bars", f"built {len(rows)} bars of {self.cfg.bar_seconds:.0f}s from history; "
                                f"{self.learner.size} bars, {self.learner.n_labeled} labeled, ready for training")

    def _history_row(self, row: dict[str, Bar]) -> None:
        self.bar_index += 1
        for s in self.symbols:
            self.bars.setdefault(s, deque(maxlen=self.cfg.chart_bars)).append(row[s])
        closes = np.array([row[s].close for s in self.symbols], dtype=np.float32)
        real_now = np.array([row[s].volume > 0.0 for s in self.symbols])
        self._mature(closes, real_now)
        x = self.features.push(row)
        if x is not None:
            self.pending.append(Pending(self.bar_index, row[self.symbols[0]].ts, x, closes, real_now, None, {}))

    def warmup_plan(self) -> int:
        """Steps the go-live warmup will run: `warmup_epochs` passes over the labeled bars
        (at least `warmup_steps`), so a deeper backfill trains longer before the first
        forecast; `max_warmup_minutes` and the held-out guard end it early."""
        cfg = self.cfg
        n = self.learner.n_labeled
        epochs = float(getattr(cfg, "warmup_epochs", 0.0) or 0.0)
        by_epochs = int(epochs * n / max(1, cfg.batch_size)) if epochs > 0 else 0
        return max(int(cfg.warmup_steps), by_epochs)

    async def _warmup(self) -> None:
        cfg = self.cfg
        n = self.learner.n_labeled
        total = self.warmup_plan()
        if n < 16 or total <= 0:
            self.trace.emit("learn", f"only {n} labeled bars; skipping warmup, learning starts live")
            return
        self.status = "training"
        self.warmup = {"done": 0, "total": total, "holdout": None, "stopped": ""}
        self._publish({"type": "status", "status": self.status_dict()})
        cap = float(getattr(cfg, "max_warmup_minutes", 0.0) or 0.0) * 60.0
        self.trace.emit("learn", f"warmup: {total} steps of batch {min(cfg.batch_size, n)} over {n} labeled bars "
                                 f"({float(getattr(cfg, 'warmup_epochs', 0) or 0):g} pass(es)"
                                 f"{f', at most {cap / 60:.0f} min' if cap else ''}, held-out guard on)")
        done, t0, held = 0, time.time(), []
        while done < total:
            chunk = min(20, total - done)
            res = await asyncio.to_thread(self.learner.train_steps, chunk)
            if not res:
                break
            done += chunk
            self._absorb_train(res)
            self.warmup["done"] = done
            if done % 40 < chunk or done >= total:
                val = await asyncio.to_thread(self.learner.validate)
                note = ""
                if val:
                    self.metrics.holdout_pinball, self.metrics.holdout_bce = val["pinball"], val["bce"]
                    held.append(val["pinball"])
                    self.warmup["holdout"] = val["pinball"]
                    note = f", held-out {val['pinball']:.2f} bps"
                    if _stalled(held, int(getattr(cfg, "overfit_patience", 0) or 0)):
                        self.warmup["stopped"] = "held-out loss stopped improving"
                        self.trace.emit("learn", f"warmup ends at {done}/{total}: held-out pinball {val['pinball']:.2f} bps has not "
                                                 f"improved over {cfg.overfit_patience} checks (best {min(held):.2f}); the history is learned", "info")
                        break
                self.trace.emit("learn", f"warmup {done}/{total}: loss {res['loss']:.3f} "
                                         f"(pinball {res['pinball']:.2f} bps, bce {res['bce']:.3f}{note})")
                self._publish({"type": "metrics", "metrics": self.metrics.as_dict(), "history": self._drain_history()})
                self._publish({"type": "status", "status": self.status_dict()})
            if cap and time.time() - t0 >= cap and done < total:
                self.warmup["stopped"] = f"{cap / 60:.0f}-minute cap"
                self.trace.emit("learn", f"warmup ends at {done}/{total}: the {cap / 60:.0f}-minute cap; the background trainer continues live", "warn")
                break
        self.warmup["done"] = done
        self.learner.release_cache()

    # Live loops -------------------------------------------------------------------------

    async def _deep_backfill_loop(self) -> None:
        """Page history back beyond the startup backfill (in `deep_chunk_days` chunks, down to
        deep_backfill_days), add the labeled windows to the replay buffer, and train on them a
        little; the background trainer does the rest. The market never closes, so this runs
        between bars: a chunk's ingest happens off the event loop, and only its short training
        burst holds the learner lock, so a forecast waits a few seconds at most."""
        wait = 60.0
        while True:
            await asyncio.sleep(wait)
            wait = 600.0                                  # nothing to fetch: check back every 10 min
            cfg = self.cfg
            days = float(getattr(cfg, "deep_backfill_days", 0) or 0)
            target = time.time() - days * 86400.0
            if days <= 0 or not self.learning_enabled or self._deep_cutoff <= target:
                continue
            if self._backtesting:                         # the backtest owns the GPU and the replay must not grow under it
                wait = 60.0
                continue
            cap = self.learner.capacity
            if self.learner.size >= cap - self._chunk_windows() - 100:   # the ring must not wrap onto the newest bars
                if not self._ring_full_said:
                    self.trace.emit("feed", f"deep backfill stops: the replay is full ({self.learner.size} of {cap} bars)", "info")
                    self._ring_full_said = True
                continue
            free = free_memory_gb(str(self.learner.device))
            if free is not None and free < float(cfg.memory_floor_gb) + 4.0:
                if not self._lowmem_said:
                    self.trace.emit("feed", f"deep backfill paused: {free:.0f} GB free, floor {cfg.memory_floor_gb:g} GB", "warn")
                    self._lowmem_said = True
                wait = 300.0
                continue
            self._lowmem_said = False
            wait = max(30.0, float(getattr(cfg, "deep_backfill_seconds", 150.0)))   # a universe-wide burst every couple of minutes stays under the venues' public limits
            until = self._deep_cutoff
            cutoff = max(target, until - float(getattr(cfg, "deep_chunk_days", 5.0)) * 86400.0)
            try:
                ticks = await self.sources.backfill_range(cutoff, until)
                self._deep_cutoff = cutoff
                if not ticks:
                    self.trace.emit("feed", "deep backfill: no history returned for "
                                            f"{datetime.fromtimestamp(cutoff):%b %d} – {datetime.fromtimestamp(until):%b %d}", "warn")
                    continue
                self._deep_filled = 0
                added = await asyncio.to_thread(self._ingest_offline, ticks)
                filled = self._deep_filled
                res = await asyncio.to_thread(self.learner.train_steps, 8) if (added or filled) else None
                if res:
                    self._absorb_train(res)
                self.learner.release_cache()
                self.trace.emit("learn", f"deep backfill {datetime.fromtimestamp(cutoff):%b %d} – "
                                         f"{datetime.fromtimestamp(until):%b %d}: {len(ticks)} ticks, "
                                         f"{added} bars labeled ({self.learner.size} in replay)" +
                                         (f", {filled} columns filled for new names" if filled else "") +
                                         (f", 8 training steps (loss {res['loss']:.3f})" if res else ""))
                self._publish({"type": "metrics", "metrics": self.metrics.as_dict(), "history": self._drain_history()})
            except Exception as e:  # noqa: BLE001
                self.trace.emit("learn", f"deep backfill failed: {type(e).__name__}: {str(e)[:80]}", "warn")

    def _chunk_windows(self) -> int:
        """Windows one deep-backfill chunk adds: its days in bars."""
        return int(float(getattr(self.cfg, "deep_chunk_days", 5.0)) * self.cfg.bars_per_day)

    def universe_candidates(self) -> dict[str, float]:
        """Interesting names and a score on one 0-100 scale, from two organic sources: the
        market radar (move weighted by dollar volume, liquid names only) and the names the press
        is naming (headline mentions). Only names an enabled venue lists can be candidates: a
        top mover with no Coinbase pair can be neither traded nor priced. The best rank across
        sources wins, with a small bonus for showing up in both."""
        cfg = self.cfg
        radar = (self.signals_state.get("market", {}) or {}).get("radar", []) or []
        known: dict[str, str] = {}                        # base -> canonical symbol
        for r in radar:
            sym = canonical(str(r.get("symbol") or ""))
            if sym and "-" in sym:
                known.setdefault(base_of(sym), sym)
        movers = []
        for r in radar:
            sym = known.get(base_of(str(r.get("symbol") or "")))
            if not sym or asset_class(sym) != "crypto" or not self.sources.tradable(sym):
                continue
            dv, chg = float(r.get("dollar_volume") or 0), abs(float(r.get("chg") or 0))
            if dv < float(cfg.interesting_min_dollar_volume):
                continue
            movers.append((chg * math.log10(max(dv, 10.0)), sym))
        movers.sort(reverse=True)
        mentions = headline_tickers((self.news or {}).get("headlines") or [], set(known))
        named = sorted(((n, known[b]) for b, n in mentions.items() if n >= 2 and self.sources.tradable(known[b])), reverse=True)
        span = max(10, int(cfg.interesting_count) * 3)
        sources = [rank_scores([s for _, s in movers], span), rank_scores([s for _, s in named], span)]
        out: dict[str, float] = {}
        for sc in sources:
            for s, v in sc.items():
                if v <= 0:
                    continue
                seen_before = s in out
                out[s] = max(out.get(s, 0.0), v) + (5.0 if seen_before else 0.0)
        return out

    def _known_prices(self) -> dict[str, float]:
        """A last price per name from what Steel already has: the live feeds and the radar."""
        out: dict[str, float] = {}
        for r in (self.signals_state.get("market", {}) or {}).get("radar", []) or []:
            sym = canonical(str(r.get("symbol") or ""))
            px = float(r.get("price") or 0.0)
            if sym and px > 0:
                out[sym] = px
        for sym, p in (self.prices or {}).items():
            px = float((p or {}).get("price") or 0.0)
            if px > 0:
                out[sym] = px
        return out

    async def _universe_loop(self) -> None:
        """Once a day: in liquid mode (the default) the universe becomes the core, every
        holding, and every dollar pair the enabled spot venues list above `model_dollar_volume`
        (liquid_universe; a fresh start runs this pass within minutes, a thin day demotes only
        after the grace period); in core mode, after the first hour of the UTC day, the core,
        the holdings and the most interesting names (pick_universe: promotion by rank, demotion
        after a grace period). The rebuild keeps retained names' embedding rows and replay
        columns and re-pages history to fill the new ones."""
        while True:
            await asyncio.sleep(60)
            cfg = self.cfg
            if not getattr(cfg, "universe_refresh", True) or self._reconfiguring or self._backtesting:
                continue
            now = clock.utc_now()
            today = now.date().isoformat()
            liquid = str(getattr(cfg, "universe_mode", "liquid")).lower() == "liquid"
            if self._universe_refreshed == today or (now.hour < 1 and not liquid):
                continue                                  # core mode waits for the day's first hour: the radar and the news report first
            if liquid:
                volumes = await self.sources.volumes(self._known_prices())
                if len(volumes) < 20:
                    continue                              # the venues' lists have not come in yet
                floor = float(getattr(cfg, "model_dollar_volume", 0.0) or 0.0)
                new, rec, promoted, demoted = liquid_universe(
                    self.core_symbols, self._held, self.all_symbols, volumes, self._interesting,
                    floor=floor, max_universe=int(cfg.max_universe), today=today,
                    grace_days=int(cfg.interesting_grace_days), min_tenure_days=int(cfg.interesting_min_tenure_days))
                above = sum(1 for dv in volumes.values() if dv >= floor)
                self.trace.emit("system", f"liquid universe: {len(volumes)} dollar pairs listed, {above} above ${floor / 1e6:g}M a day, "
                                          f"cap {cfg.max_universe}", "info")
            else:
                radar = (self.signals_state.get("market", {}) or {}).get("radar", []) or []
                if len(radar) < 100 or not (self.news or {}).get("t"):
                    continue                              # the radar and the news skim must have reported
                cands = self.universe_candidates()
                if len(cands) < 20:
                    continue
                new, rec, promoted, demoted = pick_universe(
                    self.core_symbols, self._held, self.all_symbols, cands, self._interesting,
                    count=int(cfg.interesting_count), max_universe=int(cfg.max_universe), today=today,
                    grace_days=int(cfg.interesting_grace_days), min_tenure_days=int(cfg.interesting_min_tenure_days))
            self._universe_refreshed = today
            self._interesting = rec
            if not promoted and not demoted and new == self.all_symbols:
                self.trace.emit("system", f"universe refresh: no change ({len(new)} names)", "info")
                continue
            self.all_symbols = new
            self.muted -= set(demoted)
            self.trace.emit("system", f"universe refresh: promoted {len(promoted)} ({', '.join(base_of(s) for s in promoted[:10])}"
                                      f"{'…' if len(promoted) > 10 else ''}), demoted {len(demoted)} "
                                      f"({', '.join(base_of(s) for s in demoted[:10])}{'…' if len(demoted) > 10 else ''}); {len(new)} names: "
                                      f"{len(self.core_symbols)} core, {len(set(self._held) - set(self.core_symbols))} holdings, "
                                      f"{len(new) - len(set(self.core_symbols) | set(self._held))} interesting", "act")
            asyncio.get_running_loop().create_task(self.reconfigure())
            return                                        # the rebuild cancels this task; the new lifecycle starts another

    def backtest_summary(self) -> dict:
        """The report the dashboard shows: totals (live partials while a run is on), the day
        rows, the best and worst names, and the run's configuration."""
        b = self.backtest or {}
        pr = self.backtest_progress or {}
        syms = [(k, v) for k, v in (b.get("symbols") or {}).items() if v.get("signals")]
        syms.sort(key=lambda kv: kv[1]["net_bps"])
        return {"progress": pr, "ran_at": b.get("ran_at"), "elapsed_s": b.get("elapsed_s"), "stopped": b.get("stopped"),
                "error": b.get("error"), "config": b.get("config"),
                "totals": pr.get("totals") if pr.get("running") else b.get("totals"),
                "days": (pr.get("days") or []) if pr.get("running") else (b.get("days") or [])[-260:],
                "baseline": b.get("baseline"),
                "best": [dict(sym=k, **v) for k, v in syms[-8:][::-1]], "worst": [dict(sym=k, **v) for k, v in syms[:8]],
                "sweep": self.sweep or None}

    def _backtest_due(self, now: datetime | None = None) -> bool:
        """Once a week, inside the scheduled window (`backtest_weekday` at `backtest_hour` UTC:
        the quiet early-Saturday tape by default), or on request. A run that stopped before
        scoring a day (a memory squeeze at the start) does not count as the week's run, so the
        schedule tries again."""
        if self._backtest_requested:
            return True
        if not getattr(self.cfg, "backtest_weekly", True):
            return False
        now = now or clock.utc_now()
        if not clock.backtest_window(now, weekday=int(getattr(self.cfg, "backtest_weekday", 5)),
                                     hour=int(getattr(self.cfg, "backtest_hour", 0))):
            return False
        last = self.backtest or {}
        if last.get("stopped") and not (last.get("totals") or {}).get("days"):
            return True
        return now.timestamp() - float((self.backtest or {}).get("ran_at") or 0) > 4 * 86400

    async def _backtest_loop(self) -> None:
        """Weekly walk-forward backtest (steel/backtest.py) on the live preset, in process so
        the replay is shared and the background trainer yields the GPU. Waits for the deep backfill
        to finish paging so the ring cannot move under the run. Never touches the live model."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(30)
            if self._backtesting or not self._backtest_due():
                continue
            cfg = self.cfg
            if getattr(self, "handoff", None) is not None and self.handoff.mode == "shared" and not self._backtest_requested:
                if not getattr(self, "_backtest_shared_said", False):
                    self._backtest_shared_said = True
                    self.trace.emit("learn", "backtest deferred: the GPU is shared with the peer; it runs in the next solo window", "info")
                continue
            self._backtest_shared_said = False
            if self._paging_pending():
                if self._backtest_requested and not self._backtest_waiting:
                    self._backtest_waiting = True
                    self.trace.emit("learn", "backtest queued: waiting for the deep backfill to finish paging", "info")
                continue                                  # still paging history: the replay must not grow under a run
            self._backtest_waiting = False
            L = self.learner
            n = L.size
            if n < 64 or L.n_labeled < 64 or float(L.rt[:n].max()) <= 0:
                if self._backtest_requested:
                    self._backtest_requested = False
                    self.trace.emit("learn", "backtest: no timestamped replay windows yet", "warn")
                continue
            self._backtest_requested = False
            self._backtest_stop = False
            self._backtest_stop_reason = ""
            # Own the GPU before measuring it: from here the background trainer and the deep loop start
            # nothing new, and the chunk one of them may have in flight finishes first, or the free
            # figure is read with its working set still up and the run is sized into a squeeze
            # (two such starts stopped at 1 GB free on 2026-10-10).
            self._backtesting = True
            await asyncio.to_thread(L.wait_idle)
            L.release_cache()
            dev = str(L.device)
            free = free_memory_gb(dev)
            live_peak = float((self.autotune_info or {}).get("peak_gb") or 0)
            choice = backtest_preset_for(free, int(cfg.d_model), live_peak, floor=float(cfg.memory_floor_gb),
                                         fallback=("config", int(cfg.d_model), tuple(cfg.dilations), int(cfg.n_experts),
                                                   int(cfg.n_heads), int(cfg.window)))
            if choice is None:
                self.trace.emit("learn", f"backtest skipped: {free:.0f} GB free leaves no room for any net next to the live model "
                                         f"(floor {cfg.memory_floor_gb:g} GB)", "warn")
                self.backtest_progress = {"running": False}
                self._backtesting = False
                continue
            name, d_model, dil, n_exp, n_heads, _win = choice
            cfg_bt = copy.copy(cfg)
            cfg_bt.d_model, cfg_bt.dilations, cfg_bt.n_experts, cfg_bt.n_heads = int(d_model), tuple(dil), int(n_exp), int(n_heads)
            preset = str(name) + ("" if d_model == cfg.d_model else f" (sized to {free:.0f} GB free; live is "
                                                                     f"{(self.autotune_info or {}).get('preset') or cfg.d_model})")
            variants = {"absolute": ["absolute"], "relative": ["relative"],
                        "both": ["absolute", "relative"]}.get(str(cfg.backtest_target).lower(), ["absolute"])
            if "relative" in variants and not index_columns(self.symbols, cfg.index_symbols):
                self.trace.emit("learn", f"backtest: none of {cfg.index_symbols} is modeled, so the relative target cannot run; "
                                         f"absolute only", "warn")
                variants = ["absolute"]
            hours_each = float(cfg.backtest_hours) / len(variants) if float(cfg.backtest_hours) > 0 else 0.0
            started = time.time()
            self.backtest_progress = {"running": True, "day": 0, "days": 0, "started": started,
                                      "variant": variants[0], "variants": variants}
            self.trace.emit("learn", f"backtest: walking {n} replay windows day by day with a fresh {preset} "
                                     f"(budget {cfg.backtest_hours:g} h, {cfg.backtest_night_epochs:g} night epochs/day, "
                                     f"cost {cfg.backtest_cost_bps:g} bps; target{'s' if len(variants) > 1 else ''} "
                                     f"{' then '.join(variants)}); live training waits", "act")
            self._publish({"type": "backtest", "backtest": self.backtest_summary()})

            def should_stop() -> bool:
                if self._backtest_stop:
                    return True
                low = memory_low(dev, float(cfg.memory_floor_gb))
                if low is not None:
                    self._backtest_stop_reason = f"memory low ({low:.0f} GB free)"
                    self._backtest_stop = True
                    return True
                return False

            def prog(day: int, total: int, totals: dict, info: dict | None = None) -> None:
                self.backtest_progress = {**self.backtest_progress, "running": True, "day": day, "days": total, "started": started,
                                          "totals": totals, **(info or {})}
                loop.call_soon_threadsafe(self._publish, {"type": "backtest", "backtest": self.backtest_summary()})

            def job() -> dict:
                # Runs on the worker thread: the variants back to back on one net's worth of memory,
                # each on its share of the budget; the second report carries the first as its baseline.
                reports: list[dict] = []
                for vi, variant in enumerate(variants):
                    if self._backtest_stop:
                        break
                    self.backtest_progress = {"running": True, "day": 0, "days": 0, "started": started,
                                              "variant": variant, "variants": variants}
                    if len(variants) > 1:
                        loop.call_soon_threadsafe(self.trace.emit, "learn",
                                                  f"backtest {vi + 1}/{len(variants)}: the {variant} target", "info")
                    reports.append(run_backtest(
                        cfg_bt, L.store, device=dev,
                        symbols=list(self.symbols), progress=prog, should_stop=should_stop,
                        hours=hours_each, days=int(cfg.backtest_days), warm_days=int(cfg.backtest_warm_days),
                        cost_bps=float(cfg.backtest_cost_bps), preset=preset, night_epochs=float(cfg.backtest_night_epochs),
                        target=variant, index_symbols=str(cfg.index_symbols),
                        beta_windows=int(cfg.beta_windows), beta_shrink=float(cfg.beta_shrink),
                        store=os.path.join(cfg.state_dir, "backtest_forecasts")))
                return combine_reports(reports)

            def finish(f: asyncio.Future) -> None:
                # Runs on the event loop when the worker thread exits, whether or not the loop task
                # that started it is still alive: a universe rebuild cancels this loop, but the
                # thread keeps going until its next day boundary and its report is still wanted.
                self._backtesting = False
                L.release_cache()
                self.backtest_progress = {"running": False}
                res = None
                try:
                    res = f.result()
                except Exception as e:  # noqa: BLE001
                    log.error("backtest failed", exc_info=e)
                    self.trace.emit("learn", f"backtest failed: {type(e).__name__}: {str(e)[:120]}", "error")
                if res:
                    self._finish_backtest(res)
                self._publish({"type": "backtest", "backtest": self.backtest_summary()})

            fut = loop.run_in_executor(None, job)
            fut.add_done_callback(finish)
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                self._backtest_stop = True                          # the worker checks at each day boundary; finish() runs when it exits
                raise
            except Exception:  # noqa: BLE001 -- reported by finish()
                pass

    def _paging_pending(self) -> bool:
        """Whether the deep backfill still has chunks to add: history left to page and room in the
        ring for a chunk. A full ring ends paging early (the deep loop stops within a chunk of
        full), so the backtest must not wait on the cutoff alone: before 0.5.14 it did, and a
        queued run waited forever once the replay filled before the configured depth."""
        days = float(getattr(self.cfg, "deep_backfill_days", 0) or 0)
        if days <= 0 or self._deep_cutoff <= time.time() - days * 86400.0:
            return False
        L = self.learner
        return L.size < L.capacity - self._chunk_windows() - 100

    def _finish_backtest(self, res: dict) -> None:
        cfg = self.cfg
        self.backtest = res
        try:
            tmp = os.path.join(cfg.state_dir, "backtest.json.tmp")
            with open(tmp, "w") as f:
                json.dump(clean(res), f)
            os.replace(tmp, os.path.join(cfg.state_dir, "backtest.json"))
        except OSError as e:
            log.warning("could not save backtest.json: %s", e)
        t = res.get("totals") or {}
        if res.get("error"):
            self.trace.emit("learn", f"backtest: {res['error']}", "warn")
        else:
            how = ("stopped (" + self._backtest_stop_reason + ")" if res.get("stopped") and self._backtest_stop_reason
                   else "stopped" if res.get("stopped") else "done")
            self.trace.emit("learn", f"backtest {how} in {res.get('elapsed_s', 0) / 60:.0f} min: "
                                     f"{t.get('days', 0)} days, net {t.get('net_bps', 0):+.0f} bps "
                                     f"({t.get('net_bps_per_day', 0):+.1f}/day, Sharpe {t.get('sharpe') if t.get('sharpe') is not None else '·'}), "
                                     f"hit {((t.get('hit_rate') or 0) * 100):.0f}%, coverage {((t.get('coverage') or 0) * 100):.0f}%, "
                                     f"{t.get('signals', 0)} signals; always-long {t.get('long_bps', 0):+.0f} bps"
                                     + (f" [{(res.get('config') or {}).get('target')} target]" if (res.get('config') or {}).get('target') else "")
                                     + ((lambda b: f"; the {b.get('target')} target made net {b.get('totals', {}).get('net_bps', 0):+.0f} bps "
                                                   f"(Sharpe {b.get('totals', {}).get('sharpe') if b.get('totals', {}).get('sharpe') is not None else '·'})")
                                        (res["baseline"]) if res.get("baseline") else ""), "act")
        path = (res.get("config") or {}).get("forecasts")
        if path and not res.get("error"):
            # The policy sweep over the forecasts this run stored: seconds of arithmetic on the
            # CPU, so it follows every run; the result is a proposal on the dashboard, never applied.
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self._sweep_job, str(path)).add_done_callback(self._finish_sweep)
            except RuntimeError:                                    # no loop (a test calling this directly)
                self._finish_sweep_result(self._sweep_job(str(path)))

    def _sweep_job(self, path: str) -> dict:
        from .sweep import load_forecasts, run_sweep
        return run_sweep(load_forecasts(path), copy.copy(self.cfg), float(self.cfg.backtest_cost_bps))

    def _finish_sweep(self, f) -> None:
        try:
            res = f.result()
        except Exception as e:  # noqa: BLE001
            log.error("policy sweep failed", exc_info=e)
            self.trace.emit("learn", f"policy sweep failed: {type(e).__name__}: {str(e)[:120]}", "error")
            return
        self._finish_sweep_result(res)

    def _finish_sweep_result(self, res: dict) -> None:
        self.sweep = res
        try:
            tmp = os.path.join(self.cfg.state_dir, "sweep.json.tmp")
            with open(tmp, "w") as f:
                json.dump(clean(res), f)
            os.replace(tmp, os.path.join(self.cfg.state_dir, "sweep.json"))
        except OSError as e:
            log.warning("could not save sweep.json: %s", e)
        if res.get("error"):
            self.trace.emit("learn", f"policy sweep: {res['error']}", "warn")
        else:
            cur, prop = res.get("current") or {}, res.get("proposal")
            fmt = lambda r: f"{(r or {}).get('net_bps_per_day', 0):+.1f}/day"  # noqa: E731
            text = (f"policy sweep over {res.get('days')} days ({res.get('evaluations')} settings in {res.get('elapsed_s', 0):.0f} s): "
                    + (f"proposes {', '.join(f'{k} {v}' for k, v in prop['knobs'].items())}; tune {fmt(prop['tune'])} vs "
                       f"{fmt(cur.get('tune'))} now, confirm {fmt(prop['confirm'])} vs {fmt(cur.get('confirm'))}; "
                       + ("worth a look on the Backtest tab" if res.get("accepted") else "not confirmed, so not proposed")
                       if prop else "no setting beat the current gates"))
            self.trace.emit("learn", text, "act" if res.get("accepted") else "info")
        self._publish({"type": "backtest", "backtest": self.backtest_summary()})

    async def _train_loop(self) -> None:
        """Train on the replay buffer between bars, up to train_epochs passes over it per UTC
        day (recomputed as backfill grows the replay), so the budget is bounded by data, not by
        hours, and cannot grind the same windows all day. Small chunks keep the learner lock
        short, so a forecast is delayed by one step at most. Yields to a backtest."""
        done = 0
        self._train_day = None
        wait = 20.0
        held: list[float] = []                            # held-out pinball at each check this day
        while True:
            await asyncio.sleep(wait)
            wait = 20.0                                   # idle poll; drops to ~0 while there is budget to burn
            cfg = self.cfg
            if self._backtesting:
                continue                                  # the backtest owns the GPU
            if getattr(self, "handoff", None) is not None and self.handoff.released:
                continue                                  # the peer is about to measure the GPU: stay out of its way
            free = free_memory_gb(str(self.learner.device))
            if free is not None and free < float(cfg.memory_floor_gb) + 4.0:
                if not self._lowmem_said:
                    self.trace.emit("learn", f"background training paused: {free:.0f} GB free, floor {cfg.memory_floor_gb:g} GB", "warn")
                    self._lowmem_said = True
                self.learner.release_cache()
                continue
            today = clock.utc_day()
            if today != self._train_day:                  # each UTC day gets its own budget
                done, self._train_day, held = 0, today, []
            epochs = float(getattr(cfg, "train_epochs", 0) or 0)
            size = self.learner.size
            if epochs <= 0 or not self.learning_enabled or size < 64:
                continue
            budget = int(epochs * size / max(1, cfg.batch_size))
            if done >= budget:
                continue
            wait = 0.5                                    # back to back: keep the GPU busy, yield to the event loop
            chunk = min(4, budget - done)
            res = await asyncio.to_thread(self.learner.train_steps, chunk)
            if not res:
                continue
            done += chunk
            self._absorb_train(res)
            if done % 40 < chunk or done >= budget:
                val = await asyncio.to_thread(self.learner.validate)
                note = ""
                if val:
                    self.metrics.holdout_pinball, self.metrics.holdout_bce = val["pinball"], val["bce"]
                    held.append(val["pinball"])
                    note = f", held-out {val['pinball']:.2f} bps"
                    if self.metrics.live_labels < cfg.min_labels:      # trust still rests on history: keep the scorecard current
                        await asyncio.to_thread(self.rescore_holdout)
                    if _stalled(held, int(getattr(cfg, "overfit_patience", 0) or 0)):
                        # The in-sample loss keeps falling while this stops improving: the net is
                        # memorising the replay. End the day's budget here, not at the epoch count.
                        self.trace.emit("learn", f"overfit guard: held-out pinball {val['pinball']:.2f} bps has not improved "
                                                 f"over {cfg.overfit_patience} checks (best {min(held):.2f}); ending today's "
                                                 f"background training at {done}/{budget} steps", "warn")
                        done = budget
                self.trace.emit("learn", f"background training {done}/{budget} steps today "
                                         f"({size} bars in replay): loss {res['loss']:.3f} "
                                         f"(pinball {res['pinball']:.2f} bps, bce {res['bce']:.3f}{note})")
                self._publish({"type": "metrics", "metrics": self.metrics.as_dict(), "history": self._drain_history()})
                self.learner.release_cache()              # every 40 steps: the caching allocator never accumulates

    def _ingest_offline(self, ticks: list[Tick]) -> int:
        """Turn a chunk of old history into labeled replay windows without touching the live
        pipeline: a throwaway bar builder and feature builder (seeded with the live
        normalization state) walk the chunk chronologically and feed the learner directly."""
        cfg = self.cfg
        builder = BarBuilder(self.symbols, cfg.bar_seconds)
        feats = FeatureBuilder(self.symbols, cfg.window)
        feats.norm.load(self.features.norm.state())
        first: dict[str, float] = {}
        for t in ticks:
            first.setdefault(t.symbol, t.price)
        for s in self.symbols:
            first.setdefault(s, 0.0)                   # flat-seed history-less symbols, same as startup backfill
        for sym, px in first.items():
            builder.last_close.setdefault(sym, px)
        for t in ticks:
            builder.add(t)
        rows = builder.roll(ticks[-1].ts + cfg.bar_seconds)
        added, filled, idx = 0, 0, 0
        pend: deque[tuple[int, np.ndarray, np.ndarray, np.ndarray, float]] = deque()
        for row in rows:
            idx += 1
            closes = np.array([row[s].close for s in self.symbols], dtype=np.float32)
            real_now = np.array([row[s].volume > 0.0 for s in self.symbols])
            while pend and pend[0][0] + cfg.horizon <= idx:
                _, x0, c0, r0, t0 = pend.popleft()
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = (np.log(closes / c0) * 1e4).astype(np.float32)
                y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
                mk = (r0 & real_now).astype(np.float32)
                if self.learner.add(x0, y, mk, ts=t0, closes=c0, real=r0):
                    added += 1
                else:
                    filled += self.learner.fill(t0, x0, y, mk)
            x = feats.push(row)
            if x is not None:
                pend.append((idx, x, closes, real_now, float(next(iter(row.values())).ts)))
        self.metrics.labels = self.learner.labels
        self._deep_filled = filled
        return added

    def _on_live_tick(self, tick: Tick) -> None:
        """Called by the source manager with the active provider's tick for a symbol.
        Quote/heartbeat ticks update the live price but never build candles -- otherwise a
        market-closed feed echoing the last quote would fabricate flat bars from stale data.
        A non-quote tick with zero size and no OHLC is a stale price echo, not a real trade
        (a real trade has volume), so it updates the price but must not build a candle -- one
        such echo would mark the interval active and fabricate flat bars across every symbol."""
        if not tick.quote and (tick.size > 0 or tick.o is not None):
            self.builder.add(tick)
        self._on_tick(tick, live=True)

    def _on_provider_change(self, sym: str, prev, active) -> None:
        pn = self.sources.sources[prev].name if prev else "none"
        an = self.sources.sources[active].name if active else "none"
        lvl = "warn" if active is None or active == "sim" else "act"
        self.trace.emit("feed", f"{self.base[sym]} provider: {pn} -> {an}", lvl)
        self._publish({"type": "status", "status": self.status_dict()})

    def vault_status(self) -> dict:
        v = getattr(self, "vault", None) or _vault.get(self.cfg.state_dir)
        return v.status([svc["file"] for svc in KEY_SERVICES])

    def vault_backup(self) -> dict:
        """The recovery copy, once: shown while pending; the acknowledgement ends it for good."""
        v = getattr(self, "vault", None) or _vault.get(self.cfg.state_dir)
        key = v.backup()
        if key is None:
            return {"error": "the recovery key was already shown; `steel vault rotate` makes a new one"}
        return {"key": key, "key_file": str(v.key_path)}

    def vault_ack(self) -> dict:
        v = getattr(self, "vault", None) or _vault.get(self.cfg.state_dir)
        v.acknowledge()
        self.trace.emit("system", "vault: recovery key acknowledged; it will not be shown again", "act")
        self._publish({"type": "vault", "vault": self.vault_status()})
        return {"ok": True, "vault": self.vault_status()}

    def arb_action(self, payload: dict) -> dict:
        action = str(payload.get("action") or "")
        if action in ("on", "off"):
            self.cfg.arb = action == "on"
            self.trace.emit("system", f"arbitrage executor {'ON' if self.cfg.arb else 'off'}", "error" if self.cfg.arb else "act")
        else:
            return {"error": "unknown action"}
        self._publish({"type": "arb", "arb": self.arb.status()})
        return {"ok": True, "arb": self.arb.status()}

    def live_status(self) -> dict:
        return self.live.status(self.promotion.check(self.backtest, self.paper.snapshot(self._live_prices()) if self.paper else None, self.metrics.trusted))

    def live_action(self, payload: dict) -> dict:
        action = str(payload.get("action") or "")
        if action == "promote":
            res = self.live.promote(self.promotion.check(self.backtest, self.paper.snapshot(self._live_prices()), self.metrics.trusted),
                                    force=bool(payload.get("force")))
        elif action == "demote":
            res = self.live.demote(str(payload.get("reason") or "operator"))
        else:
            return {"error": "unknown action"}
        self._publish({"type": "live", "live": self.live_status()})
        return {**res, "live_status": self.live_status()}

    async def live_flatten(self, reason: str = "operator") -> dict:
        """The panic button: cancel every open order, close every live position, demote."""
        res = await self.live.flatten(self.prices, reason=reason)
        self._publish({"type": "live", "live": self.live_status()})
        return {**res, "live_status": self.live_status()}

    def alerts_status(self) -> dict:
        return self.alerts.status()

    async def arb_replay(self, days: float = 7.0, force: bool = False) -> dict:
        """The arbitrage rules replayed over the dislocation log (steel/arbreplay.py), cached ten minutes."""
        cached = getattr(self, "_arb_replay", None)
        if cached and not force and time.time() - cached.get("computed", 0.0) < 600 and cached.get("days") == days:
            return cached
        path = self.dislocations.log_path
        if not path:
            return {"error": "no dislocation log"}
        c = self.cfg
        res = await asyncio.to_thread(_arbreplay.summary, path, days=days, min_edge_bps=float(getattr(c, "arb_min_edge_bps", 5.0) or 5.0),
                                      max_order_usd=float(getattr(c, "arb_max_order_usd", 200.0) or 200.0),
                                      cooldown_s=float(getattr(c, "arb_cooldown_s", 60.0) or 60.0), stable=str(getattr(c, "transfer_asset", "USDC") or "USDC"))
        self._arb_replay = res
        return res

    async def _alerts_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            if self.alerts.queue:
                try:
                    await self.alerts.flush()
                except Exception as e:  # noqa: BLE001
                    self.alerts.last_error = type(e).__name__

    async def _clock_check_loop(self) -> None:
        """The box clock against the venues', hourly; the gate refuses orders while it is off."""
        await asyncio.sleep(5)
        while True:
            try:
                self.execution.clock = await _clockcheck.measure()
                off = self.execution.clock.get("offset_s")
                tol = float(getattr(self.cfg, "clock_max_offset_s", 1.0) or 0.0)
                if off is not None and tol > 0 and abs(off) > tol:
                    self.trace.emit("system", f"clock: the box is {off:+.2f} s off the venues' time (limit {tol:g} s): execution is refused until it is fixed", "error")
                elif off is not None:
                    self.trace.emit("system", f"clock: {off:+.3f} s against {', '.join(self.execution.clock['venues'])}")
                self._publish({"type": "execution", "execution": self.execution_status()})
            except Exception as e:  # noqa: BLE001
                self.trace.emit("system", f"clock check: {type(e).__name__}", "warn")
            await asyncio.sleep(3600)

    def _record_scorecard(self) -> None:
        """Once per UTC day: the model's scorecard, so Research can show it over time."""
        day = clock.utc_day()
        if getattr(self, "_scorecard_day", "") == day:
            return
        self._scorecard_day = day
        m = self.metrics
        row = {"day": day, "t": round(time.time()), "holdout_hit": m.holdout_hit, "holdout_calls": m.holdout_calls, "holdout_coverage": m.holdout_coverage,
               "holdout_pinball": m.holdout_pinball, "hit_ema": m.hit_ema, "coverage_ema": m.coverage_ema, "live_pinball": m.live_pinball,
               "labels": m.labels, "live_labels": m.live_labels, "trusted": m.trusted, "band_scale": m.band_scale, "p_scale": m.p_scale,
               "pnl_bps": m.pnl_bps, "names": len(self.symbols)}
        try:
            with open(os.path.join(self.cfg.state_dir, SCORECARD_FILE), "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def monitor(self) -> dict:
        """Model monitoring for Research: calibration per name from the resolved calls, the
        drift of each standardized feature over the last day of replay rows (a mean far from 0
        or a spread far from 1 says the normalizer or the tape moved), and the daily scorecard."""
        cal = []
        for s, outs in (self.outcomes or {}).items():
            rows = [o for o in outs if o.get("hit") is not None]
            if not rows:
                continue
            n = len(rows)
            cal.append({"symbol": s, "n": n, "hit": round(sum(1 for o in rows if o["hit"]) / n, 3),
                        "inside": round(sum(1 for o in rows if o.get("inside")) / n, 3),
                        "mae_bps": round(sum(abs(float(o.get("y", 0.0)) - float(o.get("q50", 0.0))) for o in rows) / n, 1),
                        "pnl_bps": round(sum(float(o.get("pnl") or 0.0) for o in rows), 1)})
        cal.sort(key=lambda r: (r["hit"], -r["n"]))
        drift = []
        try:
            L = self.learner
            n = int(L.size)
            k = min(n, int(self.cfg.bars_per_day))
            if k >= 12:
                x = np.asarray(L.store.x[n - k:n], dtype=np.float64)          # (rows, assets, features), standardized
                flat = x.reshape(-1, x.shape[-1])
                mean = np.nanmean(flat, axis=0)
                sd = np.nanstd(flat, axis=0)
                for j, name in enumerate(FEATURES[:flat.shape[1]]):
                    mu, s_ = float(mean[j]), float(sd[j])
                    flag = "shifted" if abs(mu) > 1.0 else "wide" if s_ > 2.0 else "dead" if s_ < 0.05 else ""
                    drift.append({"feature": name, "mean": round(mu, 3), "sd": round(s_, 3), "flag": flag})
        except Exception:  # noqa: BLE001
            drift = []
        card = []
        try:
            with open(os.path.join(self.cfg.state_dir, SCORECARD_FILE)) as f:
                for line in f:
                    try:
                        card.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return {"calibration": cal, "drift": drift, "scorecard": card[-60:], "window_bars": int(self.cfg.bars_per_day),
                "metrics": self.metrics.as_dict() if hasattr(self.metrics, "as_dict") else {}}

    def ticket(self, sym: str) -> dict:
        """An order ticket for the current call on a name, for a human to place by hand (on
        Insilico, say): the venue and its spelling, side, size, limit, stop, target, time limit,
        liquidation for a short. Nothing here sends anything."""
        cfg = self.cfg
        L = self.latest.get(sym) or {}
        pr = self.prices.get(sym) or {}
        px = float(pr.get("price") or 0.0)
        bid, ask = pr.get("bid"), pr.get("ask")
        side = int(L.get("side") or 0)
        action = str(L.get("action") or "HOLD")
        if not px:
            return {"error": "no price for this name yet"}
        venue = str(L.get("venue") or "")
        as_perp = side < 0 or str(getattr(cfg, "long_mode", "spot")).lower() == "perp"
        if not venue:
            venue = str(getattr(cfg, "trade_venue_perp" if as_perp else "trade_venue_spot", "") or "")
        spelling = sym
        ex = getattr(self, "executors", {}).get(venue)
        for name in ("product", "pair", "inst", "contract", "coin"):
            fn = getattr(ex, name, None) if ex is not None else None
            if callable(fn):
                try:
                    out = fn(sym)
                    spelling = str(out[0] if isinstance(out, tuple) else out)
                    break
                except Exception:  # noqa: BLE001
                    continue
        eq = float(getattr(self, "_book_equity", 0.0) or 0.0) or float(getattr(cfg, "paper_start", 100_000.0))
        weight = float(L.get("size") or 0.0) * (1 if side >= 0 else 1)
        paper_notional = eq * weight
        scale = float(getattr(cfg, "live_scale", 0.0) or 0.0)
        live_notional = paper_notional * scale
        qty = paper_notional / px if px else 0.0
        limit = (ask if side > 0 else bid) if (bid and ask) else px
        stop_bps = float(getattr(cfg, "stop_loss_bps", 0.0) or 0.0)
        tp_bps = float(getattr(cfg, "take_profit_bps", 0.0) or 0.0)
        hold_s = int(getattr(cfg, "max_hold_bars", 0) or 0) * float(cfg.bar_seconds)
        lev = float(getattr(cfg, "short_leverage" if side < 0 else "long_leverage", 1.0) or 1.0) if as_perp else 1.0
        stop = px * (1 - stop_bps / 1e4) if side > 0 else px * (1 + stop_bps / 1e4) if side < 0 else None
        target = px * (1 + tp_bps / 1e4) if side > 0 and tp_bps else px * (1 - tp_bps / 1e4) if side < 0 and tp_bps else None
        liq = L.get("liq") if side < 0 else (policy.liquidation_price(px, lev, 1, self.fees["perp"].perp_maintenance) if (as_perp and side > 0 and lev > 1) else None)
        hz = L.get("horizons") or {}
        t = {"symbol": sym, "action": action, "side": side, "venue": venue, "venue_symbol": spelling, "kind": "perp" if as_perp else "spot",
             "leverage": lev, "price": px, "limit": limit, "qty": round(qty, 8), "paper_notional_usd": round(paper_notional, 2),
             "live_scale": scale, "live_notional_usd": round(live_notional, 2), "stop": stop, "target": target, "liquidation": liq,
             "time_limit_s": hold_s, "expected_bps": (L.get("q") or [None] * 5)[2], "p_up": L.get("p_up"), "p_down": L.get("p_down"),
             "why": L.get("why", ""), "t": time.time(), "trusted": bool(getattr(self.metrics, "trusted", False))}
        verb = {1: "BUY", -1: "SELL / SHORT"}.get(side, "HOLD (no order)")
        lines = [f"Steel ticket  {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", f"{verb} {spelling} on {venue} ({t['kind']}{f', {lev:g}x isolated' if as_perp and lev > 1 else ''})"]
        if side:
            lines += [f"qty {qty:.6g} {base_of(sym)}  (~{paper_notional:,.0f} USD paper; {live_notional:,.0f} USD at {scale:g}x live scale)",
                      f"limit {limit:,.6g}  (last {px:,.6g}" + (f", bid {bid:,.6g} / ask {ask:,.6g}" if bid and ask else "") + ")",
                      f"stop {stop:,.6g} ({stop_bps:g} bps)" + (f"  target {target:,.6g} ({tp_bps:g} bps)" if target else ""),
                      f"time limit {hold_s / 3600:g} h" + (f"  liquidation {float(liq):,.6g}" if liq else ""),
                      f"model: {t['expected_bps']:+.0f} bps expected, P(up) {float(L.get('p_up') or 0):.2f} / P(down) {float(L.get('p_down') or 0):.2f}" if t["expected_bps"] is not None else ""]
        else:
            lines.append("no order: " + str(L.get("why", ""))[:200])
        if not t["trusted"]:
            lines.append("NOTE: the model is not trusted yet")
        t["text"] = "\n".join(l for l in lines if l)
        return t

    def apply_fee_tier(self, venue: str, rates: dict, *, when: float | None = None) -> dict:
        """A venue's actual fee rates (bps per leg, from its reader) replace the static profile's
        for that venue; the paper book's rates follow when it is one of the trade venues. Returns
        what changed."""
        from dataclasses import replace
        rates = {k: float(v) for k, v in (rates or {}).items() if v is not None}
        changed = {}
        for cls, prof in self.fees.items():
            if prof.venue != venue:
                continue
            new = replace(prof, **{k: v for k, v in rates.items() if k in prof.__dataclass_fields__})
            if new != prof:
                changed[cls] = {k: (getattr(prof, k), getattr(new, k)) for k in rates if getattr(prof, k, None) != getattr(new, k, None)}
                self.fees[cls] = new
        if changed and getattr(self, "paper", None) is not None:
            if not float(getattr(self.cfg, "spot_fee_bps", 0.0) or 0.0):
                self.paper.cost_bps = self.fees["spot"].spot_taker_bps
            if not float(getattr(self.cfg, "perp_fee_bps", 0.0) or 0.0):
                self.paper.perp_fee_bps = self.fees["perp"].perp_taker_bps
        self.fee_tiers[venue] = {**rates, "t": when or time.time()}
        return changed

    async def _fees_loop(self) -> None:
        """Once a day, the real fee tier of every configured venue (its reader's fee endpoint);
        the trade venues' rates replace the static table's in the policy and the paper book."""
        self.fee_tiers = getattr(self, "fee_tiers", {})
        path = os.path.join(self.cfg.state_dir, FEE_TIERS_FILE)
        try:
            with open(path) as f:
                saved = json.load(f)
            for venue, rates in (saved or {}).items():
                if time.time() - float(rates.get("t") or 0.0) < 7 * 86400:
                    self.apply_fee_tier(venue, {k: v for k, v in rates.items() if k != "t"}, when=float(rates.get("t") or 0.0))
        except (OSError, ValueError, TypeError):
            pass
        await asyncio.sleep(40)
        while True:
            for vid, acct in list(self.accounts.items()):
                if not acct.configured:
                    continue
                try:
                    rates = await asyncio.wait_for(acct.fees(), 30.0)
                except Exception as e:  # noqa: BLE001
                    self.trace.emit("system", f"{vid} fee tier: {type(e).__name__}: {str(e)[:80]}", "warn")
                    continue
                if not rates:
                    continue
                changed = self.apply_fee_tier(vid, rates)
                if changed:
                    self.trace.emit("system", f"{vid} fee tier: " + ", ".join(f"{k} {a:g}->{b:g} bps" for cls, d in changed.items() for k, (a, b) in d.items())
                                    + " (the policy and the paper book now charge the venue's real rate)", "act")
            try:
                with open(path + ".tmp", "w") as f:
                    json.dump(self.fee_tiers, f)
                os.replace(path + ".tmp", path)
            except OSError:
                pass
            await asyncio.sleep(86400)

    async def _handoff_loop(self) -> None:
        """Every minute: is Flint here, or about to be? Switch modes, and in the lead-up give the
        GPU back (background training pauses on `handoff.released`; the CUDA cache is emptied)."""
        if not self.handoff.enabled:
            return
        await asyncio.sleep(15)
        while True:
            try:
                mode, why, changed = await asyncio.to_thread(self.handoff.evaluate)
                if changed:
                    cfg = self.cfg
                    self.trace.emit("system", f"GPU {mode}: {why} (train_epochs {cfg.train_epochs:g}, memory floor {cfg.memory_floor_gb:g} GB)", "act")
                    self._publish({"type": "handoff", "handoff": self.handoff.status()})
                if self.handoff.released and str(self.learner.device).startswith("cuda"):
                    self.learner.release_cache()
            except Exception as e:  # noqa: BLE001
                self.trace.emit("system", f"handoff: {type(e).__name__}", "warn")
            await asyncio.sleep(60)

    def handoff_action(self, payload: dict) -> dict:
        mode = str(payload.get("mode") or "auto").lower()
        if mode not in ("auto", "shared", "solo"):
            return {"error": "mode must be auto, shared or solo"}
        self.handoff.pinned = mode
        m, why, changed = self.handoff.evaluate()
        self.trace.emit("system", f"GPU handoff {'pinned ' + mode if mode != 'auto' else 'back to auto'}: now {m} ({why})", "act")
        self._publish({"type": "handoff", "handoff": self.handoff.status()})
        return {"ok": True, "handoff": self.handoff.status()}

    async def _backup_loop(self) -> None:
        """One archive a day into backup_dir when it is set (steel/backup.py)."""
        out = str(getattr(self.cfg, "backup_dir", "") or "")
        if not out:
            return
        await asyncio.sleep(120)
        while True:
            try:
                await asyncio.to_thread(self._save)
                p = await asyncio.to_thread(_backup.make, self.cfg.state_dir, out, replay=bool(getattr(self.cfg, "backup_replay", False)),
                                            keep=int(getattr(self.cfg, "backup_keep", 14) or 0))
                self.trace.emit("system", f"backup written: {p} ({p.stat().st_size / 1e6:.1f} MB)")
            except Exception as e:  # noqa: BLE001
                self.trace.emit("system", f"backup failed: {type(e).__name__}: {e}", "error")
            await asyncio.sleep(86400)

    def execution_status(self) -> dict:
        st = self.execution.status()
        st["venues"] = {vid: {"configured": ex.configured, "venue": ex.venue, "dry_run": ex.dry_run, "testnet": ex.testnet}
                        for vid, ex in self.executors.items()}
        return st

    def execution_action(self, payload: dict) -> dict:
        """The operator's hand on the switch: kill, revive, arm or disarm a venue (the latter two
        for this process only; the config file decides at the next start)."""
        action = str(payload.get("action") or "")
        if action == "kill":
            self.execution.kill(str(payload.get("reason") or "operator"))
            self.trace.emit("system", "execution KILL SWITCH on: every order is refused until lifted", "error")
        elif action == "revive":
            self.execution.revive()
            self.trace.emit("system", "execution kill switch lifted by the operator", "act")
        elif action in ("arm", "disarm"):
            vid = str(payload.get("venue") or "").lower()
            armed = set(self.execution.armed)
            (armed.add if action == "arm" else armed.discard)(vid)
            self.cfg.execution_venues = ",".join(sorted(armed))
            self.trace.emit("system", f"execution: {vid} {'armed' if action == 'arm' else 'disarmed'} ({', '.join(sorted(armed)) or 'none armed'})", "act")
            self.alerts.send("live", f"execution: {vid} {'armed' if action == 'arm' else 'disarmed'}", key=vid)
        else:
            return {"error": "unknown action"}
        self._publish({"type": "execution", "execution": self.execution_status()})
        return {"ok": True, "execution": self.execution_status()}

    def _taker_bps(self, venue: str) -> float:
        """The taker fee a leg on `venue` pays: its perp rate on a perp venue, its spot rate otherwise."""
        src = self.sources.sources.get(venue)
        prof = fee_profile(venue)
        return float(prof.perp_taker_bps if src is not None and src.venue == "perp" else prof.spot_taker_bps)

    def _identity_ok(self, venue: str, sym: str) -> bool:
        """A venue may serve a name whose ticker several ranked coins share only when its listing
        resolved to a coin; a name no coin claims, or only one, is fine from anywhere."""
        return not self.identity.ambiguous(sym) or self.identity.resolve(venue, sym) is not None

    def _on_book(self, venue: str, tick: Tick) -> None:
        """Every live venue's top of book, active or standby: the dislocation monitor's input."""
        src = self.sources.sources.get(venue)
        if src is None:
            return
        amb = self.identity.ambiguous(tick.symbol)
        closed = self.dislocations.update(tick.symbol, venue, tick.bid, tick.ask, tick.ts, quote=src.quote, kind=src.venue,
                                          bid_size=tick.bid_size, ask_size=tick.ask_size,
                                          identity=self.identity.resolve(venue, tick.symbol) if amb else None, ambiguous=amb)
        if closed and closed["window_ms"] >= 1000:
            self.trace.emit("feed", f"dislocation: {self.base.get(tick.symbol, tick.symbol)} buy {closed['buy']} sell {closed['sell']} "
                                    f"{closed['peak_bps']:+.1f} bps net for {closed['window_ms'] / 1000:.1f} s", "info")

    def _on_quote(self, sym: str, bid: float, ask: float) -> None:
        """Bid/ask update from a non-active quote source (keeps the paper-book spread current)."""
        p = self.prices.get(sym)
        if p:
            p["bid"] = bid
            p["ask"] = ask

    def _on_tick(self, t: Tick, live: bool) -> None:
        p = self.prices.get(t.symbol)
        if p is None:
            return
        p["price"] = t.price
        p["ts"] = t.ts
        if t.bid:
            p["bid"] = t.bid
        if t.ask:
            p["ask"] = t.ask
        if live:
            self.tick_counts[t.symbol] += 1

    STALL_BARS = 3   # no bar for this many intervals = no source is delivering trades

    async def _clock(self) -> None:
        self._last_row_at = time.time()
        self._stalled = False
        while True:
            await asyncio.sleep(0.25)
            for row in self.builder.roll(time.time()):
                try:
                    await self._process_row(row)
                except Exception:  # noqa: BLE001 -- one bad bar must not kill the clock for the rest of the session
                    log.exception("bar #%d failed; the clock keeps running", self.bar_index)
                    self.trace.emit("system", f"bar #{self.bar_index} failed; see the log", "warn")
                self._last_row_at = time.time()
                if self._stalled:
                    self._stalled = False
                    self.trace.emit("system", "bars are forming again", "info")
            self._check_stall(time.time())

    def _check_stall(self, now: float) -> None:
        """Quotes can keep every price ticking while no candle forms (a trade websocket dies and
        the app sits blind). The market never closes, so a gap is always a stall. Say so,
        loudly, once per stall."""
        gap = now - self._last_row_at
        if self._stalled or gap < self.STALL_BARS * self.cfg.bar_seconds:
            return
        self._stalled = True
        srcs = ", ".join(f"{p['id']} ({p.get('note') or 'ok'})" for p in self.sources.status() if p.get("enabled") and p.get("ticks"))
        msg = (f"no bar for {gap / 60:.0f} minutes: prices tick but no source is delivering trades, so nothing "
               f"forecasts or trades. Sources: {srcs}")
        log.warning(msg)
        self.trace.emit("system", msg, "warn")
        self.alerts.send("stall", msg, level="warn")

    async def _ticker(self) -> None:
        period = 1.0 / max(self.cfg.tick_hz, 0.2)
        while True:
            await asyncio.sleep(period)
            self._publish({"type": "tick", "prices": self.prices, "status": self.status_dict(),
                           "paper_eq": round(self.paper.equity(self._live_prices()), 2)})

    async def _checkpoints(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.checkpoint_minutes * 60)
            await asyncio.to_thread(self._save)
            self._record_scorecard()
            if self.live.live:
                self.live.save()                                     # the dead-man rule's heartbeat
            self.trace.emit("system", f"checkpoint saved to {self.cfg.state_dir} ({self.learner.steps} steps, "
                                      f"{self.learner.size} bars)")

    CALIB_V = 2   # calibration-state schema: 2 = batch (once-per-bar) updates; pre-2 values are untrusted

    _PERSIST_METRICS = ("live_labels", "live_pinball", "band_scale", "p_scale", "decisions", "hits",
                        "hit_ema", "coverage_n", "covered", "coverage_ema", "sharpness_ema",
                        "coverage_raw_n", "covered_raw", "suggestions", "suggestion_wins", "pnl_bps")

    def _save(self, final: bool = False) -> None:
        """Checkpoint. `final` (shutdown) also forces the replay buffer to disk."""
        try:
            m = self.metrics
            metrics = {k: getattr(m, k) for k in self._PERSIST_METRICS}
            metrics["pnl_by_symbol"] = dict(m.pnl_by_symbol)
            metrics["calib_v"] = self.CALIB_V
            self.learner.save(self.cfg.state_dir, extra={"norm": self.features.norm.state(self.symbols), "metrics": metrics,
                                                          "paper": self.paper.to_state(), "target": self.learner.target_mode,
                                                          "outcomes": {s: list(d) for s, d in self.outcomes.items() if d}},
                              force_replay=final)
            tmp = os.path.join(self.cfg.state_dir, "universe.json.tmp")
            with open(tmp, "w") as f:
                json.dump({"symbols": list(self.all_symbols), "refreshed": self._universe_refreshed,
                           "interesting": self._interesting}, f)
            os.replace(tmp, os.path.join(self.cfg.state_dir, "universe.json"))
        except Exception as e:  # noqa: BLE001
            log.warning("checkpoint failed: %s", e)

    # Per-bar work ------------------------------------------------------------------------

    def _fmt_price(self, s: str, v: float) -> str:
        return f"{v:,.2f}" if v >= 100 else f"{v:.4f}"

    def _seed_forecast(self) -> None:
        """Populate forecasts from the backfilled window immediately at go-live, so cards
        aren't blank until the first live bar closes (important at long bar intervals)."""
        if self.pending:
            pp = self.pending[-1]
            x, closes, ts = pp.x, pp.closes, pp.ts
        else:                                        # not enough bars for a full window yet
            x = self.features.peek_window()
            if x is None or not all(self.bars.get(s) for s in self.symbols):
                return
            closes = np.array([self.bars[s][-1].close for s in self.symbols], dtype=np.float32)
            ts = self.bars[self.symbols[0]][-1].ts
        pred = self.learner.predict(x)
        sugg = {s: self._suggest(i, s, pred) for i, s in enumerate(self.symbols)}
        self.gate = [float(g) for g in pred.gate]
        self.latest = {}
        for i, s in enumerate(self.symbols):
            self.latest[s] = {**sugg[s], "price": float(closes[i]), "ts": ts}
        self._publish({"type": "bar", "index": self.bar_index,
                       "bars": {s: self.bars.get(s)[-1].to_json() for s in self.symbols if self.bars.get(s)},
                       "latest": self.latest, "gate": self.gate, "metrics": self.metrics.as_dict(),
                       "history": {}, "outcomes": {s: list(self.outcomes.get(s, ())) for s in self.symbols},
                       "log": list(self.log), "status": self.status_dict()})

    async def _process_row(self, row: dict[str, Bar]) -> None:
        cfg = self.cfg
        self.bar_index += 1
        for s in self.symbols:
            self.bars.setdefault(s, deque(maxlen=self.cfg.chart_bars)).append(row[s])
        closes = np.array([row[s].close for s in self.symbols], dtype=np.float32)
        real_now = np.array([row[s].volume > 0.0 for s in self.symbols])   # a v=0 bar is a carry-forward, not a real trade
        ts = row[self.symbols[0]].ts

        self.trace.emit("feed", f"{cfg.bar_seconds:.0f}s: " + " | ".join(
            f"{self.base[s]} {self.tick_counts[s]} ticks" for s in self.symbols))
        for s in self.symbols:
            self.tick_counts[s] = 0
        parts = []
        for s in self.symbols:
            b = row[s]
            r = math.log(b.close / b.open) * 1e4 if b.open > 0 else 0.0
            parts.append(f"{self.base[s]} {self._fmt_price(s, b.close)} ({r:+.1f}bps, {b.trades} trd, vol {b.volume:.3g})")
        self.trace.emit("bars", f"#{self.bar_index} " + " | ".join(parts))

        matured = self._mature(closes, real_now)
        x = self.features.push(row)

        if x is not None:
            z = x[:, -1, :]
            for i, s in enumerate(self.symbols):
                self.trace.emit("features", f"{self.base[s]} " + " ".join(f"{n} {z[i, j]:+.2f}" for j, n in enumerate(FEATURES)))

        if matured and self.learning_enabled and not self._backtesting:
            res = await asyncio.to_thread(self.learner.train_steps, min(8, cfg.steps_per_label * matured))
            if res:
                self._absorb_train(res)
                self.trace.emit("learn", f"step {res['steps']}: loss {res['loss']:.3f} (pinball {res['pinball']:.2f} bps, "
                                         f"bce {res['bce']:.3f}, gate balance {res.get('balance', 0):.3f}) on {matured} new label(s)")
        elif matured:
            self.trace.emit("learn", f"{matured} new label(s) stored; "
                                     + ("training waits for the backtest" if self._backtesting else "learning is paused"))

        if x is not None:
            closes_d = {s: float(closes[i]) for i, s in enumerate(self.symbols)}
            self._book_equity = float(self.paper.equity(closes_d, ts))
            pred = self.learner.predict(x)
            sugg = {s: self._suggest(i, s, pred) for i, s in enumerate(self.symbols)}
            self.pending.append(Pending(self.bar_index, ts, x, closes, real_now, pred, sugg))
            self.gate = [float(g) for g in pred.gate]
            self.latest = {}
            for i, s in enumerate(self.symbols):
                self.latest[s] = {**sugg[s], "price": float(closes[i]), "ts": ts,
                                  **({"beta": {self.symbols[c]: round(float(self.learner.beta[i, j]), 2)
                                               for j, c in enumerate(self.learner.cols)}} if self.learner.cols else {}),
                                  **({"horizons": self._horizon_view(pred, i)} if pred.horizons else {})}
            # trade only once the model is trusted AND has shown real directional skill (not just tenure)
            tradeable = self.metrics.trusted and (self.metrics.hit_ema or 0.0) > cfg.min_hit_rate
            targets = {s: (self.latest[s]["side"] * self.latest[s]["size"] if tradeable else 0.0) for s in self.symbols}
            # minimum hold: a fading signal does not close a young position; a reversed one does
            for s in self.symbols:
                t, prev = targets[s], self._last_target.get(s, 0.0)
                if t == 0.0 and prev != 0.0:
                    held = self.bar_index - self._held_since.get(s, self.bar_index)
                    reversed_ = ((self.latest[s].get("score") or 0.0) > 0) != (prev > 0)
                    if held < cfg.min_hold_bars and not reversed_ and tradeable:
                        targets[s] = t = prev
                if t != 0.0 and prev == 0.0:
                    self._held_since[s] = self.bar_index
                self._last_target[s] = t
            # Exits: a stop, a profit target and a time limit on open positions (spot and shorts alike),
            # each overriding the minimum hold; a name that exits waits out a cooldown before it may
            # re-enter; and once the book has lost `daily_loss_limit` of the UTC day's opening equity,
            # no new entries today.
            for s, why in policy.exit_decisions(self.paper.pos, self.paper.avg, closes_d, self._held_since, self.bar_index, cfg,
                                                shorts=self.paper.shorts, longs=self.paper.longs).items():
                if targets.get(s, 0.0) != 0.0:
                    targets[s] = self._last_target[s] = 0.0
                    self._cooldown[s] = self.bar_index
                    self.latest[s].update({"side": 0, "size": 0.0, "action": "HOLD", "why": "exit: " + why})
                    self._log(f"{s} exit: {why}", "act")
            cooldown = int(getattr(cfg, "exit_cooldown_bars", 0) or 0)
            day = clock.utc_day(ts)
            if not self._day_open_equity or self._day_open_equity[0] != day:
                self._day_open_equity = (day, self._book_equity)
            limit = float(getattr(cfg, "daily_loss_limit", 0.0) or 0.0)
            down = 1.0 - self._book_equity / self._day_open_equity[1] if self._day_open_equity[1] > 0 else 0.0
            for s in self.symbols:
                if targets[s] == 0.0 or self.paper.pos.get(s) or s in self.paper.perps:
                    continue                                                     # only new entries are held back
                if cooldown and self.bar_index - self._cooldown.get(s, -10**9) < cooldown:
                    targets[s] = self._last_target[s] = 0.0
                    self.latest[s].update({"side": 0, "size": 0.0, "action": "HOLD",
                                           "why": f"cooling down after an exit ({self.bar_index - self._cooldown[s]}/{cooldown} bars)"})
                elif limit > 0 and down >= limit:
                    targets[s] = self._last_target[s] = 0.0
                    self.latest[s].update({"side": 0, "size": 0.0, "action": "HOLD",
                                           "why": f"daily loss limit: the book is down {down * 100:.1f}% today, no new entries"})
            # Cluster limits: no sector carries more than max_cluster_weight gross, and the book's net
            # BTC beta stays under max_beta_exposure; names in a crowded cluster are scaled down together.
            notes = policy.cluster_caps(targets, self._sector_of, self._btc_beta_of,
                                        max_cluster=float(getattr(cfg, "max_cluster_weight", 0.0) or 0.0),
                                        max_beta=float(getattr(cfg, "max_beta_exposure", 0.0) or 0.0),
                                        exempt={self.symbols[c] for c in (self.learner.cols or [])})
            for s, note in notes.items():
                self._last_target[s] = targets[s]
                self.latest[s].update({"size": round(abs(targets[s]), 3), "why": self.latest[s].get("why", "") + "  |  " + note})
            # The relative target says "beats its indices", so the book holds, per index, the weight
            # that cancels the basket's loading on it: an index short under a long basket, index spot
            # over a bearish one. An index name is a hedge instrument here, never a signal of its own.
            hedge_syms: set[str] = set()
            if self.learner.cols and cfg.hedge_index:
                hs = hedge_targets(targets, self.learner.beta, self.symbols, self.learner.cols) if tradeable else {}
                for c in self.learner.cols:
                    isym = self.symbols[c]
                    h = hs.get(isym, 0.0)
                    h = 0.0 if abs(h) < float(cfg.hedge_min) else h
                    targets[isym] = self._last_target[isym] = h
                    hedge_syms.add(isym)
                    self.latest[isym].update({"hedge": round(h, 4), "side": (h > 0) - (h < 0), "size": round(abs(h), 3),
                                              "action": "HEDGE" if h else "HOLD",
                                              "why": (f"index hedge: {h:+.0%} of the book cancels the basket's loading on {base_of(isym)}, as "
                                                      f"{'index spot' if h > 0 else 'an index short'}") if h else "index hedge: nothing to cancel"})
            n_before = self.paper.n_trades
            self.paper.rebalance(targets, closes_d, ts,
                                 quotes={s: {"bid": self.prices[s].get("bid"), "ask": self.prices[s].get("ask")}
                                         for s in self.symbols},
                                 funding=self.funding, uncapped=hedge_syms)
            if self.paper.n_trades > n_before:
                new_trades = list(self.paper.trades)[:self.paper.n_trades - n_before]
                self.alerts.send("fill", "paper: " + "; ".join(f"{t.get('side')} {base_of(str(t.get('symbol')))} {float(t.get('notional') or 0):,.0f} USD @ {float(t.get('price') or 0):,.4g}"
                                                                 for t in reversed(new_trades)))
                if self.live.live:
                    sent = await self.live.mirror(new_trades)
                    for r in sent:
                        o = r.get("order") or {}
                        line = (f"live {o.get('side')} {o.get('qty')} {base_of(str(o.get('symbol')))} on {r.get('venue')}: "
                                f"{r.get('status')}{(' ' + r['error']) if r.get('error') else ''}")
                        self.trace.emit("system", line, "act" if r.get("ok") else "warn")
                        self.alerts.send("live" if r.get("ok") else "refused", line, key=str(o.get("symbol")), level="info" if r.get("ok") else "error")
                    self._publish({"type": "live", "live": self.live_status()})
            if self.metrics.trusted:
                flips = []
                for s2, sg2 in sugg.items():
                    a = str(sg2.get("action") or "HOLD")
                    if a in ("BUY", "SELL") and self._alerted_action.get(s2) != a:
                        flips.append(f"{a} {base_of(s2)} (P {sg2.get('p_up' if a == 'BUY' else 'p_down', 0):.2f}, q50 {sg2.get('q', [0] * 5)[2]:+.0f} bps)")
                    self._alerted_action[s2] = a
                if flips:
                    self.alerts.send("signal", "calls: " + "; ".join(flips), key="all")
            self.trace.emit("model", f"bar #{self.bar_index}: regime gate " + " ".join(
                f"E{k + 1}={g:.2f}" for k, g in enumerate(self.gate)) +
                f" | band scale x{self.metrics.band_scale:.2f}, P(up) temper {self.metrics.p_scale:.2f}")
            for i, s in enumerate(self.symbols):
                q = pred.q[i]
                att = sorted(((float(pred.attn[i, j]), self.base[self.symbols[j]]) for j in range(len(self.symbols)) if j != i), reverse=True)
                sg = sugg[s]
                self.trace.emit("model", f"{self.base[s]} raw q10 {q[0]:+.1f} q25 {q[1]:+.1f} q50 {q[2]:+.1f} q75 {q[3]:+.1f} "
                                         f"q90 {q[4]:+.1f} bps, P(up) {pred.p_up[i]:.2f} | calibrated band {sg['q'][0]:+.1f} to "
                                         f"{sg['q'][4]:+.1f}, P(up) {sg['p_up']:.2f} P(down) {sg['p_down']:.2f} | attends " +
                                " ".join(f"{n} {a:.2f}" for a, n in att[:3]))
                self.trace.emit("policy", f"{self.base[s]} {sg['action']}" + (f" size {sg['size']:.2f}" if sg['side'] else "") +
                                f": {sg['why']}" + ("" if sg["trusted"] else " [warming up, not counted]"),
                                "act" if sg["side"] else "info")

        self._publish({
            "type": "bar", "index": self.bar_index, "bars": {s: row[s].to_json() for s in self.symbols},
            "latest": self.latest, "gate": self.gate, "metrics": self.metrics.as_dict(),
            "history": self._drain_history(),
            "outcomes": {s: list(self.outcomes.get(s, ())) for s in self.symbols}, "log": list(self.log),
            "status": self.status_dict(), "paper": self.paper.snapshot(self._live_prices()),
            "backtest": self.backtest_summary(),
        })

    def _mature(self, closes: np.ndarray, real_now: np.ndarray | None = None) -> int:
        n = 0
        while self.pending and self.pending[0].index + self.cfg.horizon <= self.bar_index:
            pp = self.pending.popleft()
            with np.errstate(divide="ignore", invalid="ignore"):
                y = (np.log(closes / pp.closes) * 1e4).astype(np.float32)
            y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)   # a 0-seeded / history-less symbol yields no return, not a NaN that poisons the whole batch
            mask = (pp.real & real_now).astype(np.float32) if real_now is not None else None   # train only on symbols that really traded at both ends
            self.learner.add(pp.x, y, mask, ts=pp.ts, closes=pp.closes, real=pp.real)   # the replay keeps the absolute label and the closes (the longer horizons' labels)
            if pp.pred is not None:
                self._score(pp, self.learner.target(y))     # the forecast was made on the configured target
            n += 1
        self.metrics.labels = self.learner.labels
        self._update_trust()
        return n

    def spot_fee_bps(self) -> float:
        """The spot taker fee per leg: the config's, or the spot venue's profile."""
        v = float(getattr(self.cfg, "spot_fee_bps", 0.0) or 0.0)
        return v if v > 0 else float(self.fees["spot"].spot_taker_bps)

    def perp_fee_bps(self) -> float:
        v = float(getattr(self.cfg, "perp_fee_bps", 0.0) or 0.0)
        return v if v > 0 else float(self.fees["perp"].perp_taker_bps)

    def funding_hours(self) -> float:
        v = float(getattr(self.cfg, "funding_interval_hours", 0.0) or 0.0)
        return v if v > 0 else float(self.fees["perp"].funding_hours or 8.0)

    def _sector_of(self, s: str) -> str:
        """The name's sector: the market scan's lookup, else the static map, else Other."""
        b = base_of(s)
        scan = getattr(getattr(self, "signals", None), "market", None)
        return str((getattr(scan, "sectors", None) or {}).get(b) or SECTOR_MAP.get(b, "Other"))

    def _btc_beta_of(self, s: str) -> float:
        """The name's loading on BTC-USD from the relative target's fit (0 when there is none)."""
        cols = self.learner.cols or []
        try:
            j = next(k for k, c in enumerate(cols) if self.symbols[c] == "BTC-USD")
            return float(self.learner.beta[self.symbols.index(s), j])
        except (StopIteration, ValueError, IndexError, TypeError, AttributeError):
            return 0.0

    def _horizon_view(self, pred, i: int) -> dict:
        """The extra horizons' raw forecasts for one name, keyed by their length in hours."""
        out = {}
        for h, v in (pred.horizons or {}).items():
            hours = h * self.cfg.bar_seconds / 3600.0
            key = f"{hours:g}h" if hours < 24 else f"{hours / 24:g}d"
            out[key] = {"q": [round(float(x), 2) for x in v["q"][i]], "p_up": round(float(v["p_up"][i]), 3), "p_down": round(float(v["p_down"][i]), 3)}
        return out

    def _update_trust(self) -> None:
        """Trusted by live labels (min_labels of them) or, before those exist, by the held-out
        scorecard over the last trust_days: at least min_labels judged calls at min_hit_rate or
        better. Live labels take over as they mature; a scorecard that fails never blocks a live
        trust already earned."""
        m, cfg = self.metrics, self.cfg
        was = m.trusted
        if m.live_labels >= cfg.min_labels:
            m.trusted, m.trusted_by = True, "live"
        elif m.holdout_hit is not None and m.holdout_calls >= cfg.min_labels and m.holdout_hit >= cfg.min_hit_rate:
            m.trusted, m.trusted_by = True, "held-out"
        else:
            m.trusted, m.trusted_by = False, ""
        if m.trusted != was and getattr(self, "alerts", None) is not None:
            self.alerts.send("live", f"model {'trusted' if m.trusted else 'no longer trusted'}" + (f" ({m.trusted_by})" if m.trusted else ""), key="trust")

    def rescore_holdout(self) -> dict | None:
        """Run the held-out scorecard (a worker-thread call) and apply it: the trust state, the
        per-name track records the skill gate reads before a name has live calls, and the hit
        EMA the paper gate reads, seeded once from history."""
        cfg, m = self.cfg, self.metrics
        card = self.learner.holdout_scorecard(int(float(getattr(cfg, "trust_days", 7.0)) * cfg.bars_per_day))
        if not card:
            return None
        m.holdout_hit, m.holdout_calls, m.holdout_coverage = card["hit"], card["calls"], card["coverage"]
        if card["pinball"] is not None:
            m.holdout_pinball = card["pinball"]
        self.holdout_outcomes = {self.symbols[i]: hits for i, hits in card["per_name"].items() if i < len(self.symbols)}
        if m.hit_ema is None and card["hit"] is not None and card["calls"] >= cfg.min_labels:
            m.hit_ema = card["hit"]                        # the paper gate's skill reading, until live labels move it
        self._update_trust()
        return card

    def _absorb_train(self, res: dict) -> None:
        m = self.metrics
        m.loss, m.pinball, m.bce, m.steps = res["loss"], res["pinball"], res["bce"], res["steps"]

    def _temper(self, p_raw: float) -> float:
        """Apply the online P(up)/P(down) temperature (p_scale)."""
        p_raw = min(max(p_raw, 1e-6), 1 - 1e-6)
        z = max(-6.0, min(6.0, math.log(p_raw / (1 - p_raw))))
        return 1.0 / (1.0 + math.exp(-self.metrics.p_scale * z))

    def _calibrate(self, q, p_raw: float) -> tuple[list[float], float]:
        """Apply the online conformal band scale and the P(up) temperature."""
        m = self.metrics
        q50 = float(q[2])
        qc = [q50 + m.band_scale * (float(v) - q50) for v in q]
        return qc, self._temper(p_raw)

    def _suggest(self, i: int, s: str, pred: Prediction) -> dict:
        cfg = self.cfg
        m = self.metrics
        p_raw = float(pred.p_up[i])
        p_down_raw = float(pred.p_down[i])
        qc, p = self._calibrate(pred.q[i], p_raw)
        p_down = self._temper(p_down_raw)
        q10, q25, q50, q75, q90 = qc
        iqr = max(q75 - q25, 1e-3)
        score = q50 / iqr
        # the live spread is the real cost of a round trip; a flat cost_bps only sets the floor
        pr = self.prices.get(s) or {}
        bid, ask = pr.get("bid"), pr.get("ask")
        spread_bps = (ask - bid) / ((ask + bid) / 2) * 1e4 if bid and ask and ask > bid > 0 else None
        # Beyond the spread, an hour of this position costs the impact of the order it would place and,
        # for a bearish view carried as a perp short, the perp's taker fees and the funding the short
        # pays while rates are negative (a positive rate pays the short; an edge is not allowed to lean
        # on it); the edge has to cover all of it.
        bars_s = self.bars.get(s) or ()
        eq = float(getattr(self, "_book_equity", 0.0) or 0.0) or 100_000.0
        size0 = cfg.max_size * min(1.0, cfg.kelly_fraction * abs(score))        # the size this signal would take
        adv = policy.dollar_volume(bars_s)
        impact = policy.impact_bps(eq * size0, adv, policy.sigma_daily(bars_s), float(getattr(cfg, "impact_eta", 0.1)))
        rate = (getattr(self, "funding", None) or {}).get(s)
        as_perp = score < 0 or str(getattr(cfg, "long_mode", "spot")).lower() == "perp"       # a short is always a perp; a long is one in perp mode
        fees = getattr(self, "fees", None) or {}
        if as_perp:
            fee_leg = float(getattr(cfg, "perp_fee_bps", 0.0) or 0.0) or float(fees["perp"].perp_taker_bps if fees else 0.0)
            hours = float(getattr(cfg, "funding_interval_hours", 0.0) or 0.0) or float(fees["perp"].funding_hours if fees else 8.0)
        else:
            fee_leg = float(getattr(cfg, "spot_fee_bps", 0.0) or 0.0) or float(fees["spot"].spot_taker_bps if fees else 0.0)
            hours = 8.0
        # the funding a perp pays while the rate runs against it (a short pays a negative rate, a long a positive one); an
        # edge is never allowed to lean on funding it would earn
        carry = (max(0.0, policy.funding_bps_per_hour(rate, -1 if score < 0 else 1, hours))
                 if as_perp and rate is not None and getattr(cfg, "funding_in_edge", True) else 0.0)
        perp = 2.0 * fee_leg                                                                  # the taker fee on both legs, spot or perp
        edge = abs(q50) - max(cfg.cost_bps, spread_bps or 0.0) - impact - carry - perp
        kyle = policy.kyle_impact_bps(policy.amihud(bars_s), eq * size0)
        conviction = abs(2 * p - 1)
        reasons = []
        if abs(score) < cfg.score_threshold:
            reasons.append("the expected move is small next to its uncertainty")
        if conviction < 2 * cfg.prob_margin:
            reasons.append(f"direction is near a coin flip ({p * 100:.0f}% up)")
        if edge <= 0:
            parts = [f"{spread_bps:.0f} bps spread"] if spread_bps else []
            if impact >= 0.5:
                parts.append(f"{impact:.1f} bps of impact")
            if carry >= 0.5:
                parts.append(f"{carry:.1f} bps/h of funding")
            if perp >= 0.5:
                parts.append(f"{perp:.0f} bps of {'perp' if as_perp else 'spot'} fees")
            reasons.append("the expected move does not cover trading cost" + (f" ({', '.join(parts)})" if parts else ""))
        if kyle > float(getattr(cfg, "max_impact_bps", 10.0)):
            reasons.append(f"too illiquid: a {size0 * 100:.0f}% position would move the price about {kyle:.0f} bps")
        if spread_bps is not None and spread_bps > cfg.max_spread_bps:
            reasons.append(f"spread {spread_bps:.0f} bps is wider than the {cfg.max_spread_bps:.0f} bps limit")
        # per-name skill: a name earns the right to trade with its own record, not the book's. The
        # held-out history's calls stand in until live calls fill the record, then age out of it.
        live = [bool(o["hit"]) for o in (self.outcomes.get(s) or []) if o.get("hit") is not None]
        seed = list((getattr(self, "holdout_outcomes", None) or {}).get(s) or [])
        judged = (seed + live)[-40:] if len(live) < 40 else live
        if len(judged) < cfg.skill_min_n:
            reasons.append(f"no track record on this name yet ({len(judged)}/{cfg.skill_min_n} calls)")
        else:
            hr = sum(1 for h in judged if h) / len(judged)
            if hr < cfg.min_hit_rate:
                reasons.append(f"its record on this name is {hr * 100:.0f}% over {len(judged)} calls" + (" (held-out history)" if seed and len(live) < cfg.skill_min_n else ""))
        if abs(q50) < cfg.move_floor_bps:
            reasons.append(f"expected move {abs(q50):.0f} bps is below the {cfg.move_floor_bps:.0f} bps floor")
        bar = self.bars[s][-1] if self.bars.get(s) else None
        if not (bar and bar.volume > 0):
            reasons.append("no live trade this bar (stale/flat price)")
        floor_dv = float(getattr(cfg, "min_dollar_volume", 0.0) or 0.0)
        if bar and bar.volume > 0 and adv > 0 and adv < floor_dv:   # thin books fake spreads and fills; a flat-seeded name has no volume to judge
            reasons.append(f"thin: about ${adv / 1e6:.1f}M a day trades here, under the ${floor_dv / 1e6:.0f}M floor")
        if (score > 0) != (p > 0.5):
            reasons.append("its trend and direction signals disagree")
        # the longer view: a 1 h call against a 4 h median pointing the other way is a scalp into a trend
        hz = pred.horizons or {}
        if getattr(cfg, "horizon_agree", False) and hz:
            hb = min(hz)
            hq50 = float(hz[hb]["q"][i][2])
            if (score > 0 and hq50 < 0) or (score < 0 and hq50 > 0):
                reasons.append(f"the {hb * cfg.bar_seconds / 3600:g}-hour view disagrees ({hq50:+.0f} bps)")
        # confirmation: the same side has to persist before the book acts on it
        raw_side = 0 if reasons else (1 if score > 0 else -1)
        prev = self._streak.get(s)
        run = prev[1] + 1 if prev and prev[0] == raw_side else 1
        self._streak[s] = (raw_side, run)
        if raw_side and run < cfg.confirm_bars:
            reasons.append(f"waiting for the signal to persist ({run}/{cfg.confirm_bars} bars)")
        side = 0 if reasons else raw_side
        # Compounding-oriented sizing: allocate by risk-adjusted edge (|q50|/IQR = |score|, a
        # Sharpe-like fractional-Kelly proxy), so capital concentrates where edge-per-unit-risk is
        # highest -- the direction/conviction checks above still gate entry. Fractional and hard-capped,
        # because edge estimates are noisy and drawdowns erode compounding faster than wins build it.
        size = cfg.max_size * min(1.0, cfg.kelly_fraction * abs(score)) if side else 0.0
        if side and cfg.size_by_coverage and m.coverage_ema is not None:
            size *= max(0.25, min(1.0, m.coverage_ema / 0.8))   # overconfident bands -> smaller positions
        # volatility targeting: a name that moves twice the target vol gets half the weight, so every
        # position carries about the same risk and the book's risk does not swing with the tape
        sigma = policy.sigma_daily(bars_s)
        vt = float(getattr(cfg, "vol_target", 0.0) or 0.0)
        vol_scale = max(0.25, min(1.0, vt / sigma)) if (side and vt > 0 and sigma > 0) else 1.0
        size *= vol_scale
        why = "holding: " + "; ".join(reasons) if reasons else f"leaning in: {q50:+.0f} bps expected, {p * 100:.0f}% up / {p_down * 100:.0f}% down"
        if side and vol_scale < 1.0:
            why += f"  |  sized to {vol_scale:.2f}x for {vt * 100:.0f}% daily vol (this name runs {sigma * 100:.0f}%)"
        fat_tails = p > 0.5 and p_down > 0.5
        width = q90 - q10
        if fat_tails:
            why += "  |  both tails fat — expect a big move, direction unclear"
        base_side, base_size = side, size
        muted = s in self.muted
        if muted:
            side, size, reasons = 0, 0.0, ["muted"]
            why = "muted — watching but not suggesting"
        overlay = self._crowd_overlay(s, side, size, score, qc, iqr) if (self.overlay_enabled and not muted) else None
        if overlay:
            side, size = overlay["side"], overlay["size"]
            if overlay["notes"]:
                why = why + "  |  crowd overlay: " + "; ".join(overlay["notes"])
        px = pr.get("price") or (bar.close if bar else None)
        return {"symbol": s, "action": "BUY" if side > 0 else "SELL" if side < 0 else "HOLD", "side": side,
                "fat_tails": fat_tails and not muted, "width": round(width, 1),
                "size": round(size, 3), "score": score, "p_up": p, "p_raw": p_raw, "p_down": p_down, "p_down_raw": p_down_raw, "q": qc,
                "q_raw": [float(v) for v in pred.q[i]], "iqr": iqr, "band_scale": m.band_scale, "p_scale": m.p_scale,
                "trusted": m.trusted, "why": why, "reasons": reasons,
                "crowding": round(self.crowding.get(s, 0.0), 3), "funding": rate, "sigma_daily": round(sigma, 4), "vol_scale": round(vol_scale, 3),
                "liq": (round(policy.liquidation_price(float(px), float(cfg.short_leverage), -1, (getattr(self, "fees", None) or {}).get("perp").perp_maintenance if getattr(self, "fees", None) else 0.0), 6) if side < 0 and px else None),
                "venue": ((getattr(self, "fees", None) or {}).get("perp").venue if side < 0 or str(getattr(cfg, "long_mode", "spot")).lower() == "perp"
                          else (getattr(self, "fees", None) or {}).get("spot").venue) if getattr(self, "fees", None) else None,
                "base_action": "BUY" if base_side > 0 else "SELL" if base_side < 0 else "HOLD",
                "overlay": overlay["notes"] if overlay else [], "muted": muted}

    def _crowd_overlay(self, s: str, side: int, size: float, score: float, qc: list, iqr: float) -> dict:
        """The contrarian overlay on the raw model call: a margin of safety (penalize asymmetric
        downside) and fade the crowd (cut, hold or flip a trade aligned with euphoria: funding,
        the long/short ratio, open interest, the basis, Fear & Greed and social attention all
        leaning its way). Each adjustment says what drove it."""
        cfg = self.cfg
        crowd = self.crowding.get(s, 0.0)
        q10, q90 = qc[0], qc[4]
        notes = []
        o_side, o_size = side, size
        # margin of safety
        if o_side != 0:
            down = max(0.0, -q10) if o_side > 0 else max(0.0, q90)
            up = max(0.0, q90) if o_side > 0 else max(0.0, -q10)
            if down - up > 0:
                pen = min(1.0, cfg.overlay_safety * (down - up) / iqr)
                o_size *= (1 - 0.6 * pen)
                if pen > 0.15:
                    notes.append(f"margin of safety: downside {down:.0f} vs upside {up:.0f} bps, trim {60 * pen:.0f}%")
        # fade the crowd
        if o_side != 0:
            align = o_side * crowd
            fade_at = cfg.overlay_fade_at
            if align > fade_at:
                mag = min(1.0, (align - fade_at) / max(1e-6, 1 - fade_at))
                o_size *= max(0.0, 1 - cfg.overlay_aggr * mag)
                notes.append(f"fade the crowd (crowding {crowd:+.2f}), trim {cfg.overlay_aggr * mag * 100:.0f}%")
                if align > 0.8 and abs(score) < 2 * cfg.score_threshold:
                    o_side, o_size = -o_side, cfg.max_size * 0.3
                    notes.append("extreme crowding + thin edge: contrarian flip")
                elif o_size < 0.05:
                    o_side = 0
                    notes.append("faded to HOLD")
        o_size = round(max(0.0, o_size), 3)
        if o_side != side or abs(o_size - size) > 1e-3:
            return {"side": o_side, "size": o_size, "notes": notes, "crowding": crowd}
        return {"side": side, "size": size, "notes": [], "crowding": crowd}

    def _score(self, pp: Pending, y: np.ndarray) -> None:
        m = self.metrics
        cfg = self.cfg
        taus = np.asarray(cfg.quantiles, dtype=np.float64)
        parts = []
        # Symbols move together, so a bar's 211 labels are one observation of calibration, not 211.
        # Per-symbol updates here compound into one enormous step (and with the horizon's delay,
        # band_scale bang-bangs between its clips). Accumulate the bar, apply each update once.
        pins, hits_b, widths, insides, p_grads = [], [], [], [], []
        for i, s in enumerate(self.symbols):
            q = pp.pred.q[i]
            yi = float(y[i])
            q50 = float(q[2])
            sug = pp.suggestions.get(s, {"side": 0, "size": 0.0, "trusted": False, "action": "HOLD"})
            # Out-of-sample pinball loss of the raw forecast made before this label existed.
            diff = yi - q.astype(np.float64)
            pin = float(np.maximum(taus * diff, (taus - 1) * diff).mean())
            pins.append(pin)
            hit = None
            if yi != 0.0 and q50 != 0.0:
                hit = (q50 > 0) == (yi > 0)
                m.decisions += 1
                m.hits += int(hit)
                hits_b.append(float(hit))
            raw_inside = float(q[0]) <= yi <= float(q[-1])
            m.coverage_raw_n += 1
            m.covered_raw += int(raw_inside)
            qc = sug.get("q") or [float(v) for v in q]
            inside = qc[0] <= yi <= qc[-1]
            m.coverage_n += 1
            m.covered += int(inside)
            insides.append(float(inside))
            widths.append(float(qc[-1] - qc[0]))
            if yi != 0.0:
                p_raw = min(max(float(sug.get("p_raw", pp.pred.p_up[i])), 1e-6), 1 - 1e-6)
                z = max(-6.0, min(6.0, math.log(p_raw / (1 - p_raw))))
                pc = 1.0 / (1.0 + math.exp(-m.p_scale * z))
                p_grads.append((pc - (1.0 if yi > 0 else 0.0)) * z)
            pnl = None
            if sug["side"] != 0 and sug["trusted"]:
                pnl = sug["side"] * sug["size"] * yi - cfg.cost_bps * sug["size"]
                m.pnl_bps += pnl
                m.pnl_by_symbol[s] = m.pnl_by_symbol.get(s, 0.0) + pnl
                m.suggestions += 1
                m.suggestion_wins += int(pnl > 0)
                self._log(f"{s} {sug['action']} x{sug['size']:.2f} resolved {yi:+.1f} bps, paper {pnl:+.2f} bps",
                          "win" if pnl > 0 else "loss")
            self.outcomes.setdefault(s, deque(maxlen=40)).append({"t": pp.ts, "y": yi, "q50": q50, "hit": hit, "inside": inside,
                                     "side": sug["side"], "pnl": pnl, "pin": pin})
            mark = "hit" if hit else "miss" if hit is False else "flat"
            parts.append(f"{self.base[s]} {yi:+.1f} vs q50 {q50:+.1f} {mark}" + ("" if inside else " (outside band)"))
        m.live_pinball = _ema(m.live_pinball, float(np.mean(pins)), 0.05)
        if hits_b:
            m.hit_ema = _ema(m.hit_ema, float(np.mean(hits_b)), 0.02)
        inside_frac = float(np.mean(insides))
        m.coverage_ema = _ema(m.coverage_ema, inside_frac, 0.02)
        m.sharpness_ema = _ema(m.sharpness_ema, float(np.mean(widths)), 0.05)
        # Adaptive conformal update: the band scale settles where 20% of labels fall outside.
        m.band_scale = float(np.clip(m.band_scale * math.exp(cfg.band_gamma * ((1.0 - inside_frac) - 0.2)), 0.5, 25.0))
        # Online temperature on the direction head, one SGD step of the calibration cross-entropy.
        if p_grads:
            m.p_scale = float(np.clip(m.p_scale - cfg.temper_lr * float(np.mean(p_grads)), 0.02, 1.5))
        m.live_labels += 1
        self._update_trust()
        self._hpush("loss", m.live_pinball)
        self._hpush("hit", m.hit_ema)
        self._hpush("coverage", m.coverage_ema)
        self._hpush("pnl", m.pnl_bps)
        self.trace.emit("learn", f"label for bar #{pp.index} matured: " + " | ".join(parts) +
                        f" | live pinball {m.live_pinball:.2f} bps, band scale x{m.band_scale:.2f}, P(up) temper {m.p_scale:.2f}")

    # Signals + the crowd overlay --------------------------------------------------------

    async def _signals_loop(self) -> None:
        await asyncio.sleep(6)
        while True:
            await self.gather_signals()
            await asyncio.sleep(max(1.0, self.cfg.signals_minutes) * 60)

    async def _market_loop(self) -> None:
        """Refresh whole-market movers/sectors/breadth on a tight loop; the heavier radar stays on the signals cadence."""
        if not self.signals.enabled.get("market"):
            return
        await asyncio.sleep(12)
        while True:
            if not self._gathering:                        # don't collide with the full signals gather
                try:
                    await asyncio.wait_for(self.signals.market.scan(light=True), timeout=45)
                    if isinstance(self.signals_state, dict):
                        self.signals_state["market"] = self.signals.market.state
                        self._publish({"type": "signals", "signals": self.signals_state,
                                       "signal_providers": self.signals.status()})
                except Exception as e:  # noqa: BLE001
                    self.trace.emit("signals", f"market refresh failed: {type(e).__name__}", "warn")
            await asyncio.sleep(max(20.0, self.cfg.market_scan_seconds))

    async def gather_signals(self) -> None:
        if self._gathering:
            return
        self._gathering = True
        say = lambda text, level="info": self.trace.emit("signals", text, level)  # noqa: E731
        try:
            active = [p["name"] for p in self.signals.status() if p["enabled"]]
            self.trace.emit("signals", "refreshing: " + (", ".join(active) or "no providers enabled"))
            st = await asyncio.wait_for(self.signals.gather(say), timeout=180)
            self.signals_state = st
            self.crowding = st.get("crowding", {})
            self.funding = {s: float(r) for s, r in (st.get("funding") or {}).items() if r is not None}
            for sym in self.symbols:
                pa = st["per_asset"].get(sym)
                if pa:
                    self.features.set_exo(sym, pa["feat"])
            fg = st.get("fear_greed") or {}
            if fg.get("value") is not None:
                self.trace.emit("signals", f"Fear & Greed {fg['value']} ({fg['class']})")
            dv = st.get("dvol") or {}
            if dv:
                self.trace.emit("signals", "DVOL " + ", ".join(f"{k} {v:.0f}" for k, v in dv.items()))
            for sym in self.symbols:
                pa = st["per_asset"].get(sym, {})
                f = pa.get("feat", {})
                fr = pa.get("funding_rate")
                self.trace.emit("signals", f"{self.base[sym]} crowding {self.crowding.get(sym, 0):+.2f} | reddit {pa.get('mentions', 0)} "
                                f"mentions ({f.get('social_sent', 0):+.2f}) | funding {fr * 100:+.4f}%/8h " if fr is not None else
                                f"{self.base[sym]} crowding {self.crowding.get(sym, 0):+.2f} | reddit {pa.get('mentions', 0)} "
                                f"mentions ({f.get('social_sent', 0):+.2f}) | no perp ")
            self._publish({"type": "signals", "signals": self.signals_state, "signal_providers": self.signals.status()})
        except Exception as e:  # noqa: BLE001
            self.trace.emit("signals", f"signal refresh failed: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._gathering = False

    # News ------------------------------------------------------------------------------

    async def _news_loop(self) -> None:
        if not self.cfg.news_enabled:
            self.trace.emit("news", "news skimming disabled (STEEL_NEWS=0)")
            return
        await asyncio.sleep(3)
        while True:
            await self.skim_news()
            await asyncio.sleep(max(1.0, self.cfg.news_minutes) * 60)

    async def skim_news(self) -> None:
        if self._skimming:
            self.trace.emit("news", "skim already in progress", "warn")
            return
        self._skimming = True
        say = lambda text, level="info": self.trace.emit("news", text, level)  # noqa: E731
        try:
            active = [x["name"] for x in self.news_hub.status() if x["enabled"]]
            self.trace.emit("news", "skimming: " + (", ".join(active) or "no news sources enabled"))
            res = await asyncio.wait_for(self.news_hub.skim(say), timeout=300)
            for s in self.symbols:
                pa = res["per_asset"].get(s)
                base = (pa["sentiment"], pa["attention"]) if pa else (0.0, 0.0)
                self.news_base[s] = base
                self.features.set_news(s, *self._blend_news(s, *base))
            self.news = res
            fresh = [h for h in res["headlines"] if h["new"]]
            self.trace.emit("news", f"{len(res['headlines'])} relevant headlines via {res['method']}, {len(fresh)} new")
            for h in fresh[:25]:
                tags = ",".join(h["assets"]) or "market"
                self.trace.emit("news", f"[{tags}] {h['sentiment']:+.2f} {h['title']} ({h['source']})",
                                "act" if abs(h["sentiment"]) >= 0.5 else "info")
            for idea in res["ideas"]:
                self.trace.emit("news", "idea: " + idea, "act")
            self._publish({"type": "news", "news": self.news})
        except Exception as e:  # noqa: BLE001
            self.trace.emit("news", f"skim failed: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._skimming = False

    # Controls ---------------------------------------------------------------------------

    def _brief_slices(self) -> dict:
        """Assemble Steel's current state into compact per-topic data slices for the analysts."""
        sig = self.signals_state or {}
        m = sig.get("market", {}) or {}
        met = self.metrics
        b = self.base
        stance = ("trusted" + (" on held-out history" if met.trusted_by == "held-out" else "") if met.trusted
                  else f"warming up ({met.live_labels}/{self.cfg.min_labels} live labels"
                  + (f"; held-out {met.holdout_hit * 100:.0f}% on {met.holdout_calls} calls" if met.holdout_hit is not None else "") + ")")

        def line(v):
            q = v.get("q") or [0, 0, 0, 0, 0]
            return (f"{b.get(v['symbol'], v['symbol'])} {v.get('action')} score {v.get('score', 0):+.2f} "
                    f"q50 {q[2]:+.0f}bps P(up) {v.get('p_up', 0):.2f} band[{q[0]:+.0f},{q[4]:+.0f}]")
        calls = sorted((v for v in self.latest.values() if v.get("q")), key=lambda v: -abs(v.get("score", 0)))
        tape = f"model stance: {stance}; regime {m.get('regime', '?')}\n" + "\n".join(line(v) for v in calls[:10])

        secs = m.get("sectors") or []
        lead = ", ".join(f"{s['name']} {s['chg']:+.1f}%" for s in secs[:2])
        lag = ", ".join(f"{s['name']} {s['chg']:+.1f}%" for s in secs[-2:])
        mv = m.get("movers", {}) or {}
        g = ", ".join(f"{x.get('base') or x['symbol']} {x['chg']:+.1f}%" for x in (mv.get("gainers") or [])[:5])
        lo = ", ".join(f"{x.get('base') or x['symbol']} {x['chg']:+.1f}%" for x in (mv.get("losers") or [])[:5])
        fg = sig.get("fear_greed") or {}
        dv = sig.get("dvol") or {}
        flow = m.get("stable_flow_1d")
        macro = (f"breadth {round((m.get('breadth') or 0) * 100)}% of the top {m.get('breadth_n', '?')} names up on the day; "
                 f"BTC dominance {m.get('btc_dom') or '?'}%; total crypto cap {m.get('mcap_chg24') if m.get('mcap_chg24') is not None else '?'}% 24h; "
                 f"stablecoin supply {flow * 100:+.2f}% 1d; " if flow is not None else "") + (
                 f"DVOL BTC {dv.get('BTC', '?')} ETH {dv.get('ETH', '?')}; Fear & Greed {fg.get('value', '?')} ({fg.get('class', '?')}); "
                 f"regime {m.get('regime', '?')}\ncategory leaders: {lead}; laggards: {lag}\ntop gainers: {g}\ntop losers: {lo}")

        cr = sig.get("crowding", {}) or {}
        pa_all = sig.get("per_asset") or {}
        hot = ", ".join(f"{b.get(s, s)} {v:+.2f}" for s, v in sorted(cr.items(), key=lambda kv: -kv[1])[:3])
        fund = ", ".join(f"{b.get(s, s)} {pa['funding_rate'] * 100:+.3f}%" for s, pa in
                         sorted(((s, pa) for s, pa in pa_all.items() if pa.get("funding_rate") is not None),
                                key=lambda kv: -abs(kv[1]["funding_rate"]))[:3])
        social = ", ".join(f"{b.get(s, s)} {n} mentions" for n, s in
                           sorted(((pa.get("mentions", 0), s) for s, pa in pa_all.items()), reverse=True)[:3] if n)
        positioning = f"most crowded: {hot or 'n/a'}\nhighest funding: {fund or 'n/a'}\ntop retail attention: {social or 'n/a'}"

        stats = (f"{len(self.symbols)} crypto names tracked; model {stance}; regime {m.get('regime', '?')}; "
                 f"breadth {round((m.get('breadth') or 0) * 100)}%; BTC dominance {m.get('btc_dom') or '?'}%; "
                 f"Fear & Greed {fg.get('value', '?')}")
        return {"tape": tape, "macro": macro, "positioning": positioning, "stats": stats}

    async def generate_brief(self) -> None:
        if self.brief.get("generating"):
            return
        self.brief = {**self.brief, "generating": True, "error": None}
        self._publish({"type": "brief", "brief": self.brief})
        say = lambda t: self.trace.emit("system", "brief: " + t)  # noqa: E731
        try:
            from .brief import write_brief
            res = await write_brief(self.cfg, self._brief_slices(), say)
            self.brief = {"text": None, "takes": {}, "models": {}, "generating": False, **res}
            if res.get("error"):
                self.trace.emit("system", "brief unavailable: " + res["error"], "warn")
            else:
                n = sum(1 for t in res.get("takes", {}).values() if t)
                self.trace.emit("system", f"brief written by {res['models']['big']} from {n} local desk notes", "act")
        except Exception as ex:  # noqa: BLE001
            self.brief = {**self.brief, "generating": False, "error": f"{type(ex).__name__}: {str(ex)[:120]}"}
            self.trace.emit("system", "brief failed: " + str(ex)[:120], "error")
        self._publish({"type": "brief", "brief": self.brief})

    async def _identity_loop(self) -> None:
        """The identity book: CoinGecko's top 1,000 and Coinbase's names, at start and daily."""
        while True:
            if self.identity.stale:
                try:
                    await self.identity.refresh()
                    st = self.identity.status()
                    self.trace.emit("system", f"identity: {st['symbols']} symbols known, {st['ambiguous']} shared by several coins"
                                              + (f" ({st['error']})" if st["error"] else ""), "warn" if st["error"] else "info")
                    self._publish({"type": "identity", "identity": self.identity_view()})
                except Exception as e:  # noqa: BLE001
                    self.trace.emit("system", f"identity refresh: {type(e).__name__}", "warn")
            await asyncio.sleep(3600)

    def identity_view(self) -> dict:
        """Per modeled name: the coin it is, whether the ticker is shared, and which venues resolved it."""
        out = {}
        for s in self.symbols:
            lab = self.identity.label(s, self.sources.active.get(s))
            if lab["ambiguous"]:
                lab["resolved_on"] = [v for v in self.sources.sources if v != "sim" and self.identity.resolve(v, s)]
            out[s] = lab
        return out

    async def _dislocation_loop(self) -> None:
        """Every 15 s: close episodes whose quotes went stale and publish the monitor's stats."""
        while True:
            await asyncio.sleep(15)
            try:
                self.dislocations.sweep()
                self._publish({"type": "dislocations", "dislocations": self.dislocations.stats(30)})
            except Exception as e:  # noqa: BLE001
                self.trace.emit("feed", f"dislocations: {type(e).__name__}", "warn")

    def _record_account_history(self, pf: dict) -> None:
        """One row per poll, at most one every five minutes: the total and each venue's
        liquidation value and cash, so the Book tab can draw real equity over time."""
        now = time.time()
        if now - getattr(self, "_history_at", 0.0) < 300 or not (pf or {}).get("accounts"):
            return
        self._history_at = now
        row = {"t": round(now), "total": round(sum(float(a.get("liquidation") or 0.0) for a in pf["accounts"]), 2),
               "cash": round(sum(float(a.get("cash") or 0.0) for a in pf["accounts"]), 2),
               "venues": {a["id"]: {"equity": round(float(a.get("liquidation") or 0.0), 2), "cash": round(float(a.get("cash") or 0.0), 2)}
                          for a in pf["accounts"] if not a.get("error")}}
        try:
            with open(os.path.join(self.cfg.state_dir, HISTORY_FILE), "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def account_history(self, days: float = 30.0) -> dict:
        """The recorded rows over the last `days`, thinned to at most 2000 points."""
        path = os.path.join(self.cfg.state_dir, HISTORY_FILE)
        since = time.time() - days * 86400.0
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if float(r.get("t") or 0.0) >= since:
                        rows.append(r)
        except OSError:
            pass
        step = max(1, len(rows) // 2000)
        rows = rows[::step]
        venues = sorted({v for r in rows for v in (r.get("venues") or {})})
        return {"days": days, "rows": rows, "venues": venues, "first": rows[0]["t"] if rows else None, "last": rows[-1]["t"] if rows else None}

    def export_csv(self, what: str) -> tuple[str, str] | None:
        """(filename, csv text) for the paper trades, the paper transfers, the account history,
        the live order ledger or the arbitrage ledger; None for an unknown name."""
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        if what == "paper-trades":
            w.writerow(["time", "utc", "symbol", "side", "qty", "price", "notional_usd", "note"])
            for t in sorted(self.paper.trades, key=lambda x: x.get("t", 0)):
                w.writerow([t.get("t"), _iso(t.get("t")), t.get("sym"), t.get("side"), t.get("shares"), t.get("price"), t.get("notional"), t.get("note", "")])
        elif what == "paper-transfers":
            w.writerow(["time", "utc", "event", "from", "to", "venue", "amount_usd", "fee_usd", "asset", "rail", "lands_at"])
            for t in sorted(self.paper.transfers, key=lambda x: x.get("t", 0)):
                w.writerow([t.get("t"), _iso(t.get("t")), t.get("event"), t.get("from"), t.get("to"), t.get("venue"), t.get("amount"), t.get("fee"), t.get("asset"), t.get("rail"), t.get("lands_at")])
        elif what == "account-history":
            h = self.account_history(3650)
            w.writerow(["time", "utc", "total_usd", "cash_usd"] + [f"{v}_equity" for v in h["venues"]] + [f"{v}_cash" for v in h["venues"]])
            for r in h["rows"]:
                vs = r.get("venues") or {}
                w.writerow([r["t"], _iso(r["t"]), r.get("total"), r.get("cash")] + [vs.get(v, {}).get("equity") for v in h["venues"]] + [vs.get(v, {}).get("cash") for v in h["venues"]])
        elif what in ("orders", "arb"):
            path = os.path.join(self.cfg.state_dir, "orders.jsonl" if what == "orders" else "arb.jsonl")
            rows = []
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                pass
            keys: list[str] = []
            for r in rows:
                for k in _flat(r):
                    if k not in keys:
                        keys.append(k)
            w.writerow(["utc"] + keys)
            for r in rows:
                fr = _flat(r)
                w.writerow([_iso(r.get("t"))] + [fr.get(k, "") for k in keys])
        else:
            return None
        return f"steel-{what}-{time.strftime('%Y%m%d', time.gmtime())}.csv", buf.getvalue()

    async def _portfolio_loop(self) -> None:
        """Poll exchange account balances (read-only) for the Portfolio tab, and model every
        holding so the tab shows real calls. Never trades. Checks for keys on every pass, so a
        key saved in the Control panel starts it without a restart."""
        await asyncio.sleep(9)
        while True:
            configured = [a for a in self.accounts.values() if a.configured]
            if not configured:
                await asyncio.sleep(30)
                continue
            try:
                self.portfolio = await fetch_balances(configured, self.prices)
                self.arb.portfolio = self.portfolio
                self._record_account_history(self.portfolio)
                self._publish({"type": "portfolio", "portfolio": self.portfolio})
                if self.live.live or self.live.expected:
                    rec = self.live.reconcile(self.portfolio)
                    self._publish({"type": "live", "live": self.live_status()})
                    if rec.get("breach"):
                        self._publish({"type": "execution", "execution": self.execution_status()})
                held = {s for s in held_symbols(self.portfolio) if self.sources.tradable(s)}
                self._held = set(held)
                room = self.cfg.max_universe - len(self.all_symbols)
                new = [s for s in sorted(held) if s not in self.all_symbols][:max(0, room)]
                if new:                                    # model your holdings too, so the Portfolio tab shows real calls
                    self.all_symbols.extend(new)
                    self.trace.emit("system", f"holdings added to the universe: {', '.join(base_of(s) for s in new)}", "act")
                    asyncio.get_running_loop().create_task(self.reconfigure())
            except Exception as e:  # noqa: BLE001
                self.trace.emit("system", f"portfolio: {type(e).__name__}", "warn")
            await asyncio.sleep(max(20.0, self.cfg.portfolio_seconds))

    async def _brief_loop(self) -> None:
        while True:
            # only write when the model is live: during backfill/warmup the local LLM and the
            # DNN contend for the GPU and both crawl to a halt
            if self.brief_enabled and self.status == "live" and (self.signals_state or {}).get("market"):
                try:
                    await self.generate_brief()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(max(2.0, self.cfg.brief_minutes) * 60)
            else:
                await asyncio.sleep(15)

    def apply_control(self, payload: dict) -> dict:
        changed = {}
        for k, v in (payload.get("set") or {}).items():
            if k not in TUNABLE:
                continue
            try:
                val = TUNABLE[k](v)
            except (TypeError, ValueError):
                continue
            setattr(self.cfg, k, val)
            changed[k] = val
            if k == "lr":
                for g in self.learner.opt.param_groups:
                    g["lr"] = val
            if k in ("train_epochs", "memory_floor_gb") and getattr(self, "handoff", None) is not None:
                self.handoff.rebase()                        # the operator's new figure is the base the modes scale from
        if changed:
            self.trace.emit("system", "settings changed: " + ", ".join(f"{k}={v}" for k, v in changed.items()), "act")
            self.metrics.holdout_hit, self.metrics.holdout_calls, self.metrics.holdout_coverage = None, 0, None
            self.holdout_outcomes = {}
            self._update_trust()
        action = payload.get("action")
        if action == "pause":
            self.learning_enabled = False
            self.trace.emit("system", "learning paused; the model keeps forecasting with frozen weights", "act")
        elif action == "resume":
            self.learning_enabled = True
            self.trace.emit("system", "learning resumed", "act")
        elif action == "checkpoint":
            self._save()
            self.trace.emit("system", f"checkpoint saved to {self.cfg.state_dir}", "act")
        elif action == "reset":
            self.learner.reset(clear_replay=bool(payload.get("clear_replay")))
            self.metrics = Metrics(pnl_by_symbol={s: 0.0 for s in self.symbols})
            self.metrics.labels = self.learner.labels
            for v in self.history.values():
                v.clear()
            self._history_new = {k: [] for k in self.history}
            for v in self.outcomes.values():
                v.clear()
            self.trace.emit("system", "model weights and optimizer re-initialized" +
                            (", replay cleared" if payload.get("clear_replay") else ", replay kept"), "act")
        elif action == "refresh_universe":
            self._universe_refreshed = ""                 # the loop runs the next pass the pool allows
            return {"ok": True, "universe": "refresh queued"}
        elif action == "backtest":
            self._backtest_requested = True
            return {"ok": True, "backtest": "running" if self._backtesting else "queued"}
        elif action == "backtest_stop":
            self._backtest_stop = True
            return {"ok": True, "backtest": "stopping" if self._backtesting else "idle"}
        elif action == "skim":
            asyncio.get_running_loop().create_task(self.skim_news())
        elif action == "toggle_source":
            res = self.sources.toggle(payload.get("id", ""), bool(payload.get("on")))
            if res.get("error"):
                self.trace.emit("feed", f"could not toggle {payload.get('id')}: {res['error']}", "warn")
            self._publish({"type": "sources", "sources": self.sources.status(), "status": self.status_dict()})
        elif action == "toggle_news":
            self.news_hub.toggle(payload.get("id", ""), bool(payload.get("on")))
            self.trace.emit("news", f"{payload.get('id')} news turned {'on' if payload.get('on') else 'off'}", "act")
            self._publish({"type": "news_sources", "news_sources": self.news_hub.status()})
        elif action == "toggle_signal":
            self.signals.toggle(payload.get("id", ""), bool(payload.get("on")))
            self.trace.emit("signals", f"{payload.get('id')} signal turned {'on' if payload.get('on') else 'off'}", "act")
            asyncio.get_running_loop().create_task(self.gather_signals())
            self._publish({"type": "signal_providers", "signal_providers": self.signals.status()})
        elif action == "refresh_signals":
            asyncio.get_running_loop().create_task(self.gather_signals())
        elif action == "brief":
            asyncio.get_running_loop().create_task(self.generate_brief())
        elif action == "toggle_brief":
            self.brief_enabled = bool(payload.get("on"))
            self.trace.emit("system", f"local LLM brief {'enabled' if self.brief_enabled else 'disabled'}", "act")
            if self.brief_enabled and not self.brief.get("text"):
                asyncio.get_running_loop().create_task(self.generate_brief())
        elif action == "overlay":
            self.overlay_enabled = bool(payload.get("on"))
            self.trace.emit("policy", f"crowd overlay {'enabled' if self.overlay_enabled else 'disabled'}", "act")
        elif action == "mute":
            on = bool(payload.get("on"))  # on == muted
            for sym in payload.get("symbols", []):
                sym = canonical(str(sym))
                self.muted.add(sym) if on else self.muted.discard(sym)
            asyncio.get_running_loop().create_task(self.reconfigure())
        elif action == "mute_class":
            cls, on = payload.get("class"), bool(payload.get("on"))
            for sym in self.all_symbols:
                if asset_class(sym) == cls:
                    self.muted.add(sym) if on else self.muted.discard(sym)
            asyncio.get_running_loop().create_task(self.reconfigure())
        elif action == "add_symbols":
            added = []
            for sym in payload.get("symbols", []):
                sym = canonical(str(sym))
                if sym and "-" in sym and sym not in self.all_symbols and len(self.all_symbols) < self.cfg.max_universe:
                    self.all_symbols.append(sym); added.append(sym)
            if added:
                self.trace.emit("system", f"added to universe: {', '.join(added)}", "act")
                asyncio.get_running_loop().create_task(self.reconfigure())
        elif action == "remove_symbols":
            rem = {canonical(str(s)) for s in payload.get("symbols", [])}
            self.all_symbols = [s for s in self.all_symbols if s not in rem]
            self.muted -= rem
            if rem:
                self.trace.emit("system", f"removed from universe: {', '.join(sorted(rem))}", "act")
                asyncio.get_running_loop().create_task(self.reconfigure())
        elif action == "add_movers":
            n = int(payload.get("n", 20))
            radar = (self.signals_state.get("market", {}) or {}).get("radar", [])
            added = []
            for r in radar:
                sym = canonical(str(r.get("symbol", "")))
                if (sym and asset_class(sym) == "crypto" and self.sources.tradable(sym) and sym not in self.all_symbols
                        and len(self.all_symbols) < self.cfg.max_universe):
                    self.all_symbols.append(sym); added.append(sym)
                if len(added) >= n:
                    break
            if added:
                self.trace.emit("system", f"added top {len(added)} movers: {', '.join(added)}", "act")
                asyncio.get_running_loop().create_task(self.reconfigure())
        elif action == "inject":
            return {"type": "inject", **self.inject_note(payload.get("text", ""))}
        state = {"type": "controls", "controls": self.controls(), "metrics": self.metrics.as_dict(),
                 "sources": self.sources.status(), "news_sources": self.news_hub.status(),
                 "signal_providers": self.signals.status()}
        self._publish(state)
        return state
