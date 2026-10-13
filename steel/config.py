"""Runtime configuration. Every field can be overridden with a STEEL_<NAME> environment variable."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default, cast=None):
    raw = os.environ.get(f"STEEL_{name}")
    if raw is None:
        return default
    if cast is not None:
        return cast(raw)
    return type(default)(raw)


def _flag(name: str, default: str) -> bool:
    return str(_env(name, default)).lower() in ("1", "true", "yes", "on")


# The core universe: liquid Coinbase USD pairs across the majors, the L1/L2 chains, DeFi, infra and
# a few memes. Every name here is a live `<BASE>-USD` product on Coinbase (probed 2026-10-11).
CORE = ("BTC,ETH,SOL,XRP,DOGE,ADA,AVAX,LINK,DOT,LTC,BCH,ATOM,UNI,AAVE,NEAR,SUI,APT,ARB,OP,POL,"
        "XLM,HBAR,ICP,FIL,ETC,INJ,TIA,SEI,RENDER,FET,ONDO,TAO,ALGO,VET,CRO,STX,IMX,GRT,LDO,ENS,"
        "CRV,SAND,MANA,SHIB,PEPE,BONK,WIF,ZEC,HYPE,ENA,WLD,PENDLE,JTO,PYTH,QNT,STRK,TON,ZK,EIGEN,MORPHO")


def canonical(sym: str) -> str:
    """BASE-USD for a bare base ("btc"), a dashed pair ("BTC-USD"), a slashed one ("BTC/USD") or a
    venue pair against a dollar stable ("BTCUSDT")."""
    s = str(sym or "").strip().upper().replace("/", "-")
    if not s:
        return s
    if "-" in s:
        base, quote = s.split("-", 1)
        return f"{base}-USD" if quote in ("USD", "USDT", "USDC") else s
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}-USD"
    return f"{s}-USD"


def _symbols() -> list[str]:
    return [canonical(s) for s in _env("SYMBOLS", CORE).split(",") if s.strip()]


def _json_file(*paths: str) -> dict:
    """A key file, sealed by the vault (steel/vault.py) or plaintext; {} when absent."""
    for path in paths:
        try:
            raw = open(path).read()
        except OSError:
            continue
        if raw.startswith("steel-vault:"):
            from .vault import get  # noqa: PLC0415 -- the vault imports nothing of config
            d = get().read_json(path)
            if d:
                return d
            continue
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return {}


def _coinbase() -> tuple[str, str]:
    """Coinbase Advanced Trade (CDP) API key name and EC private key, from the environment or
    coinbase.json ({"name": "organizations/.../apiKeys/...", "privateKey": "-----BEGIN EC PRIVATE KEY-----..."}),
    the file the key portal downloads. Read-only use: account balances for the Portfolio tab."""
    name = _env("COINBASE_KEY_NAME", "")
    pem = _env("COINBASE_PRIVATE_KEY", "")
    if not (name and pem):
        d = _json_file("coinbase.json", ".coinbase.json")
        name = name or str(d.get("name") or d.get("key_name") or "")
        pem = pem or str(d.get("privateKey") or d.get("private_key") or "")
    return name, pem.replace("\\n", "\n")


def _kraken() -> tuple[str, str]:
    key = _env("KRAKEN_KEY", "")
    secret = _env("KRAKEN_SECRET", "")
    if not (key and secret):
        d = _json_file("kraken.json", ".kraken.json")
        key = key or str(d.get("key") or "")
        secret = secret or str(d.get("secret") or "")
    return key, secret


def _coingecko_key() -> str:
    key = _env("COINGECKO_KEY", "")
    return key or str(_json_file("coingecko.json", ".coingecko.json").get("key") or "")


def read_creds(service: str, fields: list[str]) -> dict[str, str]:
    """A venue's saved credentials: `<service>.json` in the working directory (what the Control
    panel writes) with a `STEEL_<SERVICE>_<FIELD>` environment variable overriding each field.
    Every field a string, empty when unset; a PEM field's escaped newlines are unescaped."""
    d = _json_file(f"{service}.json", f".{service}.json")
    out = {}
    for f in fields:
        v = _env(f"{service.upper()}_{f.upper()}", "") or str(d.get(f) or "")
        out[f] = v.replace("\\n", "\n") if f.lower().endswith("key") and "BEGIN" in v else v
    if service == "coinbase":                      # the first version's names for these two
        name, pem = _coinbase()
        out["name"] = out.get("name") or name
        out["privateKey"] = out.get("privateKey") or pem
    elif service == "kraken":
        key, secret = _kraken()
        out["key"] = out.get("key") or key
        out["secret"] = out.get("secret") or secret
    return out


@dataclass
class Config:
    # Market data
    symbols: list[str] = field(default_factory=_symbols)
    feed: str = _env("FEED", "auto")                 # auto | or a single source id to force
    sources_on: str = _env("SOURCES_ON", "")         # comma list to force EXACTLY these on (plus sim); "+bitget,+kucoin" adds to the defaults
    sources_off: str = _env("SOURCES_OFF", "")        # comma list of venues to keep off even if on by default (Coinbase, Kraken, Hyperliquid)
    coinbase_creds: tuple = field(default_factory=_coinbase)   # (key name, EC private key PEM): read-only account access
    kraken_creds: tuple = field(default_factory=_kraken)       # (key, secret): read-only account access
    coingecko_key: str = field(default_factory=_coingecko_key)  # optional demo key for a higher CoinGecko rate limit
    coinbase_ws: str = _env("COINBASE_WS", "wss://ws-feed.exchange.coinbase.com")
    coinbase_rest: str = _env("COINBASE_REST", "https://api.exchange.coinbase.com")
    kraken_seconds: float = _env("KRAKEN_SECONDS", 20.0)       # REST poll between websocket reconnects
    bybit_seconds: float = _env("BYBIT_SECONDS", 15.0)         # seconds between Bybit ticker polls
    okx_seconds: float = _env("OKX_SECONDS", 15.0)
    coingecko_seconds: float = _env("COINGECKO_SECONDS", 60.0)  # free tier: ~30 calls/min across everything
    bar_seconds: float = _env("BAR_SECONDS", 300.0)   # 5-minute bars (every venue serves them natively)
    backfill_days: float = _env("BACKFILL_DAYS", 30.0)    # history every name is paged before the first forecast (Coinbase serves 30 days for 60 names in about two minutes)
    backfill_seconds: float = _env("BACKFILL_SECONDS", 0.0)  # the same in seconds; 0 = backfill_days (set either; seconds wins when both are set)
    backfill_pages: int = _env("BACKFILL_PAGES", 0)   # max REST pages per symbol (0 = whatever backfill_days needs)
    train_epochs: float = _env("TRAIN_EPOCHS", 1.0)   # background training: up to this many passes over the replay per UTC day (0 = off); the market never closes, so this runs between bars
    overfit_patience: int = _env("OVERFIT_PATIENCE", 3)   # background training stops for the day when the held-out pinball (5% of windows never trained on) fails to improve over this many 40-step checks (0 = off)
    deep_backfill_days: float = _env("DEEP_BACKFILL_DAYS", 30.0)  # keep paging history back this far and train on it (0 = off); the cap on market-data use
    deep_chunk_days: float = _env("DEEP_CHUNK_DAYS", 5.0)        # days per deep-backfill page (5 days = 1,440 five-minute candles, 5 Coinbase requests per name)
    deep_backfill_seconds: float = _env("DEEP_BACKFILL_SECONDS", 150.0)  # pause between pages, so a 60-name burst stays well under Coinbase's public limit

    # Problem definition
    window: int = _env("WINDOW", 64)      # bars of context fed to the model
    horizon: int = _env("HORIZON", 12)    # bars ahead (12 x 5min = 1h forecast): the policy's horizon
    extra_horizons: str = _env("EXTRA_HORIZONS", "48,288")  # more horizons the net forecasts alongside (4 h and 24 h in bars), shown, never traded; "" = none
    quantiles: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
    # The target: "absolute" trains on each name's log return; "relative" on the return minus beta
    # times the index's (the market-relative abnormal return), with the index hedged in the book.
    # The replay stores absolute labels either way, so this is a run-time switch, not a reset.
    target: str = _env("TARGET", "absolute")
    index_symbols: str = _env("INDEX_SYMBOLS", "BTC-USD,ETH-USD")  # "the market" for the relative target: modeled index names, first = the prior; unmodeled ones are ignored
    hedge_index: bool = _flag("HEDGE_INDEX", "1")       # relative target: hold, per index, the weight that cancels the book's loading on it
    hedge_min: float = _env("HEDGE_MIN", 0.01)          # an index hedge smaller than this share of the book is not worth its cost
    beta_windows: int = _env("BETA_WINDOWS", 8640)      # label rows the loadings are fit on (about 30 days of 5-minute bars)
    beta_shrink: float = _env("BETA_SHRINK", 400.0)     # pseudo-observations behind the prior (1 on the first index, 0 on the rest)

    # Compute + auto-sizing (benchmarks the machine on first run and picks the biggest
    # model that still trains in real time; cached to <state_dir>/machine.json).
    device: str = _env("DEVICE", "auto")               # auto | cpu | cuda | mps
    compile: bool = _flag("COMPILE", "0")              # torch.compile the net (CUDA only); falls back to eager on failure
    auto_size: bool = _flag("AUTO_SIZE", "1")
    autotune_util: float = _env("AUTOTUNE_UTIL", 0.7)  # fraction of the bar interval training may use
    max_warmup_seconds: float = _env("MAX_WARMUP_SECONDS", 180.0)  # the autotune's go-live ceiling over warmup_steps: caps the auto model size
    max_warmup_minutes: float = _env("MAX_WARMUP_MINUTES", 30.0)   # the most time the go-live warmup may take over the backfilled history (0 = no cap)

    # Model (overridden by auto-sizing unless auto_size is off)
    d_model: int = _env("D_MODEL", 48)
    dilations: tuple[int, ...] = (1, 2, 4, 8, 16)
    n_experts: int = _env("N_EXPERTS", 3)
    n_heads: int = _env("N_HEADS", 4)
    dropout: float = _env("DROPOUT", 0.15)

    # Online learning
    lr: float = _env("LR", 5e-4)
    weight_decay: float = _env("WEIGHT_DECAY", 1e-2)
    label_smoothing: float = _env("LABEL_SMOOTHING", 0.1)   # softens the P(up)/P(down) targets so the net cannot claim ~99% certainty
    label_cap_bps: float = _env("LABEL_CAP_BPS", 3000.0)    # cap on label magnitude in bps (0 = off); crypto moves 30% in an hour only on a broken feed
    direction_threshold_bps: float = _env("DIRECTION_THRESHOLD_BPS", 10.0)  # deadband: P(up)=P(ret>+t), P(down)=P(ret<-t); the gap is P(flat)
    batch_size: int = _env("BATCH_SIZE", 16)
    steps_per_label: int = _env("STEPS_PER_LABEL", 2)
    replay_size: int = _env("REPLAY_SIZE", 8640)   # replay capacity in bars when no replay_gb is set (8,640 = 30 days of 5-minute bars)
    replay_gb: float = _env("REPLAY_GB", 0.0)      # replay budget in GB; when set, the bar count follows the universe and replay_size is ignored
    replay_days: float = _env("REPLAY_DAYS", 400.0)  # the most history the replay keeps, whatever the budget allows (0 = no cap)
    recent_frac: float = _env("RECENT_FRAC", 0.3)   # share of each batch drawn from the newest samples
    recent_n: int = _env("RECENT_N", 256)
    warmup_steps: int = _env("WARMUP_STEPS", 60)   # the least training on backfilled history before going live (the autotune's budget unit)
    trust_days: float = _env("TRUST_DAYS", 7.0)     # the held-out bars of the last N days score the model before live labels can: trusted on history when they clear min_labels calls at min_hit_rate
    warmup_epochs: float = _env("WARMUP_EPOCHS", 1.0)  # passes over the backfilled history before the first forecast, with the held-out guard; 0 = warmup_steps only
    input_noise: float = _env("INPUT_NOISE", 0.1)  # gaussian noise on standardized inputs during training
    min_labels: int = _env("MIN_LABELS", 48)        # live (out-of-sample) labels before suggestions are counted
    band_gamma: float = _env("BAND_GAMMA", 0.05)     # adaptive conformal step for the band scale
    temper_lr: float = _env("TEMPER_LR", 0.01)       # online temperature step for P(up)
    torch_threads: int = _env("TORCH_THREADS", 2)   # overridden by autotune (up to half the cores on a GPU box)

    # Suggestion policy
    score_threshold: float = _env("SCORE_THRESHOLD", 0.35)  # |q50| / IQR needed to act
    prob_margin: float = _env("PROB_MARGIN", 0.06)          # |P(up) - 0.5| needed to act
    cost_bps: float = _env("COST_BPS", 0.0)                 # a floor on the spread the policy charges (the live spread replaces it when wider); fees are separate, by venue
    trade_venue_spot: str = _env("TRADE_VENUE_SPOT", "coinbase")   # the venue a spot position would trade on: its fee profile (sources/fees.py) is what the policy and the book charge
    trade_venue_perp: str = _env("TRADE_VENUE_PERP", "hyperliquid")  # the venue a perp (long or short) would trade on
    long_mode: str = _env("LONG_MODE", "perp")              # perp: a bullish view is a 1x perp on trade_venue_perp (a stake-capped loss, perp fees and funding) | spot: it is spot on trade_venue_spot
    long_leverage: float = _env("LONG_LEVERAGE", 1.0)       # isolated margin per perp long = notional / leverage; 1x has no liquidation price and the stake is the worst case
    spot_fee_bps: float = _env("SPOT_FEE_BPS", 0.0)         # taker fee per spot leg; 0 = the spot venue's profile
    perp_fee_bps: float = _env("PERP_FEE_BPS", 0.0)         # taker fee per perp leg; 0 = the perp venue's profile
    max_size: float = _env("MAX_SIZE", 1.0)
    move_floor_bps: float = _env("MOVE_FLOOR_BPS", 8.0)     # min |expected move| (bps) to take a side
    # Risk gates (policy only; the model is untouched). All live controls.
    skill_min_n: int = _env("SKILL_MIN_N", 8)                # matured direction calls a name needs before it may trade
    confirm_bars: int = _env("CONFIRM_BARS", 2)              # the same side must persist this many bars before entry
    min_hold_bars: int = _env("MIN_HOLD_BARS", 6)            # a spot position is kept at least this long unless the signal reverses
    short_min_hold_bars: int = _env("SHORT_MIN_HOLD_BARS", 3)  # hold a perp short at least this many bars before closing (anti-churn)
    # The cost edge beyond the quoted spread (steel/policy.py): square-root price impact of the order the
    # signal would place, the funding a short pays over the hold, and an illiquidity gate on Amihud's
    # measure (the price move the order would buy).
    impact_eta: float = _env("IMPACT_ETA", 0.1)              # square-root impact coefficient: cost = eta * sigma_daily * sqrt(Q / ADV)
    max_impact_bps: float = _env("MAX_IMPACT_BPS", 10.0)     # a name whose Amihud illiquidity says the order would move it more than this does not trade
    funding_in_edge: bool = _flag("FUNDING_IN_EDGE", "1")    # charge the funding a short would pay (negative rates) against a bearish signal's edge
    funding_interval_hours: float = _env("FUNDING_INTERVAL_HOURS", 0.0)  # the perp venue's funding interval; 0 = its profile (8 h on Bybit/Binance, 1 h on Hyperliquid). The rates Steel reads are Bybit's 8-hour unit, scaled to the interval
    # Exits on positions (0 = off) and a daily loss limit that stops new entries for the UTC day.
    stop_loss_bps: float = _env("STOP_LOSS_BPS", 300.0)      # close a position down this much from entry, min hold or not (crypto: wider than equities)
    take_profit_bps: float = _env("TAKE_PROFIT_BPS", 0.0)    # close a position up this much from entry (0: the forecast decides)
    max_hold_bars: int = _env("MAX_HOLD_BARS", 0)            # close a position held this many bars (0: the forecast decides)
    exit_cooldown_bars: int = _env("EXIT_COOLDOWN_BARS", 12)  # after an exit the name waits this many bars before re-entering
    horizon_agree: bool = _env("HORIZON_AGREE", True)    # the next horizon's median (4 h) must not point the other way before a 1 h call trades
    vol_target: float = _env("VOL_TARGET", 0.05)          # daily volatility a full-size position is sized for: size scales by vol_target / the name's realized daily vol (floor 0.25x; 0 = off)
    max_cluster_weight: float = _env("MAX_CLUSTER_WEIGHT", 0.4)  # gross weight cap per sector (the market scan's categories); 0 = off
    max_beta_exposure: float = _env("MAX_BETA_EXPOSURE", 1.0)    # cap on the book's net BTC beta (sum of weight x loading on BTC-USD); 0 = off
    daily_loss_limit: float = _env("DAILY_LOSS_LIMIT", 0.03)  # fraction of the UTC day's opening equity lost before new entries stop for the day (0 = off)
    max_spread_bps: float = _env("MAX_SPREAD_BPS", 20.0)     # names quoting wider than this do not trade; the live spread also replaces cost_bps when larger
    min_dollar_volume: float = _env("MIN_DOLLAR_VOLUME", 2e6)  # names trading less than this a day (from the bars held) do not trade: thin books, fake fills
    size_by_coverage: bool = _flag("SIZE_BY_COVERAGE", "1")  # scale size by band coverage / 0.8, so an overconfident model trades small
    short_leverage: float = _env("SHORT_LEVERAGE", 2.0)      # isolated margin per perp short = notional / leverage; the loss is capped at that margin (liquidation at +1/leverage)
    short_max_frac: float = _env("SHORT_MAX_FRAC", 0.05)     # margin per short, as a fraction of equity: the most one bearish view can lose
    min_hit_rate: float = _env("MIN_HIT_RATE", 0.5)         # only trade once directional skill (hit_ema) beats a coin flip
    paper_min_trade_frac: float = _env("PAPER_MIN_TRADE_FRAC", 0.015)  # skip paper rebalances smaller than this share of equity (anti-churn)
    kelly_fraction: float = _env("KELLY_FRACTION", 0.15)   # fractional-Kelly sizing: weight = this * |q50|/IQR (risk-adjusted edge)

    # Signals + the crowd overlay
    muted_symbols: str = _env("MUTED", "")           # symbols to watch-but-not-suggest (comma list)
    signals_off: str = _env("SIGNALS_OFF", "")          # signal providers off by default: social, feargreed, derivatives, vol, market
    signals_minutes: float = _env("SIGNALS_MINUTES", 5.0)   # how often to refresh exogenous signals
    market_scan_seconds: float = _env("MARKET_SCAN_SECONDS", 90.0)  # light loop for the whole-market panel (CoinGecko's free tier is ~30 calls/min)
    portfolio_seconds: float = _env("PORTFOLIO_SECONDS", 60.0)      # how often to poll exchange account balances (read-only)
    operator_half_life: float = _env("OPERATOR_HALF_LIFE", 1800.0)  # decay of an injected human note (seconds)
    max_universe: int = _env("MAX_UNIVERSE", 512)            # hard cap on modeled symbols (Coinbase lists about 400 USD pairs; attention is cheap, the websockets are the limit)
    universe_mode: str = _env("UNIVERSE", "liquid")          # liquid: every dollar pair the enabled spot venues list above model_dollar_volume (plus the core and holdings) | core: the configured core, holdings and the most interesting names (Flint's rule)
    model_dollar_volume: float = _env("MODEL_DOLLAR_VOLUME", 1e6)  # the modeling floor in liquid mode: names trading less a day are not worth a row (the trading floor, min_dollar_volume, is separate)
    dislocation_min_bps: float = _env("DISLOCATION_MIN_BPS", 0.0)   # an episode needs the cross-venue gap, net of both taker fees, above this
    dislocation_latency_bps: float = _env("DISLOCATION_LATENCY_BPS", 0.0)  # a margin for the price moving while the orders travel
    dislocation_max_gap_bps: float = _env("DISLOCATION_MAX_GAP_BPS", 500.0)  # a gross gap beyond this is a wrong quote (another asset or unit, a broken book), never an episode
    dislocation_stale_s: float = _env("DISLOCATION_STALE_S", 10.0)   # a venue's quote older than this is not a leg
    # The GPU handoff with Flint (steel/handoff.py)
    handoff: bool = _env("HANDOFF", True)                     # watch for the peer and switch between shared and solo modes
    handoff_peer: str = _env("HANDOFF_PEER", "flint-run")     # the peer's systemd user unit
    handoff_peer_process: str = _env("HANDOFF_PEER_PROCESS", "python -m flint")
    handoff_peer_port: int = _env("HANDOFF_PEER_PORT", 8000)
    handoff_schedule: str = _env("HANDOFF_SCHEDULE", "Mon..Fri 19:45")  # when the peer's timer starts it (local time), for the lead-up release; blank = detection only
    handoff_lead_minutes: float = _env("HANDOFF_LEAD_MINUTES", 15.0)    # release the GPU this long before the peer starts
    solo_train_multiplier: float = _env("SOLO_TRAIN_MULTIPLIER", 3.0)   # train_epochs x this while the peer is off
    solo_memory_floor_gb: float = _env("SOLO_MEMORY_FLOOR_GB", 2.0)     # memory_floor_gb while the peer is off
    # Order books (steel/book.py)
    books: bool = _env("BOOKS", True)                         # subscribe to each live venue's depth (Binance, Bybit, OKX, Kraken, Hyperliquid)
    book_mb: float = _env("BOOK_MB", 64.0)                    # memory for the decimated level history (the heatmap), split across the universe
    book_history_s: float = _env("BOOK_HISTORY_S", 15.0)      # how often a row of levels is kept per venue
    book_levels: int = _env("BOOK_LEVELS", 40)                # levels per side per venue kept in that row
    # Derivatives feeds (steel/derivs.py, steel/sources/derivs.py)
    derivs: bool = _env("DERIVS", True)                       # liquidations, OI, funding and mark from the perp venues' public streams
    derivs_venues: str = _env("DERIVS_VENUES", "binance,bybit,okx,hyperliquid")  # which feeds run (ids binance-perp, bybit-perp, okx-perp; a bare venue name works)
    liq_mb: float = _env("LIQ_MB", 16.0)                      # memory for liquidation events, split across the universe
    liq_alert_usd: float = _env("LIQ_ALERT_USD", 250000.0)    # alert on a single liquidation at or above this notional (0 = off)
    # The trade tape (steel/tape.py)
    tape_mb: float = _env("TAPE_MB", 256.0)                   # memory for every live venue's prints, split across the universe (about 8M prints)
    tape_hours: float = _env("TAPE_HOURS", 24.0)              # how far back the tape answers
    tape_alert_usd: float = _env("TAPE_ALERT_USD", 0.0)       # alert on a single print at or above this notional (0 = off)
    bubble_usd: float = _env("BUBBLE_USD", 100000.0)          # a print at or above this notional is a bubble on the chart
    predictions_minutes: float = _env("PREDICTIONS_MINUTES", 10.0)  # how often Polymarket's crypto markets are read (0 = off)
    options_minutes: float = _env("OPTIONS_MINUTES", 10.0)   # how often Deribit's BTC and ETH chains are refreshed for the GEX, smile and term structure (0 = off)
    absorption_sensitivity: float = _env("ABSORPTION_SENSITIVITY", 50.0)  # 10 (few flags) to 90 (many): how large new positioning must be, relative to recent bars, with price not moving
    # The operator token (steel/auth.py)
    auth: str = _env("AUTH", "mutate")                        # mutate: every POST needs the token (the default) | all: reads and the websocket too | off: nothing does
    token: str = _env("TOKEN", "")                            # the operator token; empty = generated at first start, sealed in the vault, printed by `steel token`
    api_weight_per_minute: int = _env("API_WEIGHT_PER_MINUTE", 1200)   # the /api/v1 weight budget per token per minute (steel/apiv1.py)
    backup_dir: str = _env("BACKUP_DIR", "")                  # when set, the engine writes a `steel backup` archive there once a day
    backup_keep: int = _env("BACKUP_KEEP", 14)                # how many daily archives to keep there
    backup_replay: bool = _env("BACKUP_REPLAY", False)        # include the replay buffer in the daily archive (large)
    flatten_on_stop: bool = _env("FLATTEN_ON_STOP", False)    # a stop closes every live position (reduce-only market orders); open orders are always cancelled
    live_deadman_minutes: float = _env("LIVE_DEADMAN_MINUTES", 30.0)  # a live book not heard from for this long at a start is demoted (and flattened when execution is on); 0 = off
    clock_max_offset_s: float = _env("CLOCK_MAX_OFFSET_S", 1.0)  # box clock vs venue server time past which execution is refused
    # Execution (off by default; see steel/execution). Every cap at zero refuses every order.
    execution: bool = _flag("EXECUTION", "0")                 # the master switch: nothing is ever sent while it is off
    execution_venues: str = _env("EXECUTION_VENUES", "")      # comma list of armed venue ids; each needs its own <venue>-trade.json key
    execution_testnet: bool = _flag("EXECUTION_TESTNET", "0")  # use the venues' testnets where they have one
    execution_dry_run: bool = _flag("EXECUTION_DRY_RUN", "1")  # build, sign and log every order without sending it
    exec_max_order_usd: float = _env("EXEC_MAX_ORDER_USD", 0.0)
    exec_max_name_usd: float = _env("EXEC_MAX_NAME_USD", 0.0)     # per name per UTC day
    exec_max_venue_usd: float = _env("EXEC_MAX_VENUE_USD", 0.0)   # per venue per UTC day
    exec_max_day_usd: float = _env("EXEC_MAX_DAY_USD", 0.0)       # across everything per UTC day
    exec_max_exposure_frac: float = _env("EXEC_MAX_EXPOSURE_FRAC", 0.5)  # open notional over live equity
    exec_max_leverage: float = _env("EXEC_MAX_LEVERAGE", 2.0)
    exec_reject_cooldown_s: float = _env("EXEC_REJECT_COOLDOWN_S", 300.0)
    # The live book (steel/live.py): the paper book mirrored to the armed venues once promoted
    live_scale: float = _env("LIVE_SCALE", 0.1)              # live notional per paper notional (the paper book is $100k)
    live_min_backtest_days: int = _env("LIVE_MIN_BACKTEST_DAYS", 10)
    live_min_backtest_net_bps: float = _env("LIVE_MIN_BACKTEST_NET_BPS", 0.0)   # net bps per scored day
    live_min_hit: float = _env("LIVE_MIN_HIT", 0.52)
    live_min_paper_days: float = _env("LIVE_MIN_PAPER_DAYS", 7.0)
    live_min_paper_sharpe: float = _env("LIVE_MIN_PAPER_SHARPE", 0.05)    # per-bar Sharpe of the paper curve
    live_max_drawdown: float = _env("LIVE_MAX_DRAWDOWN", 0.05)            # paper drawdown to be eligible; live drawdown from peak that demotes
    live_drift_tol: float = _env("LIVE_DRIFT_TOL", 0.25)                  # sent vs held, as a share of the expected size, that demotes
    # The arbitrage executor (steel/arb.py): acts on the dislocation monitor's episodes, through the gate
    arb: bool = _flag("ARB", "0")
    arb_min_edge_bps: float = _env("ARB_MIN_EDGE_BPS", 5.0)       # net of both taker fees and the latency margin
    arb_max_order_usd: float = _env("ARB_MAX_ORDER_USD", 200.0)   # per leg, or the top of book when smaller
    arb_max_open: int = _env("ARB_MAX_OPEN", 3)                   # perp pairs held at once
    arb_cooldown_s: float = _env("ARB_COOLDOWN_S", 60.0)          # per name between attempts
    universe_refresh: bool = _flag("UNIVERSE_REFRESH", "1")  # once a day at the UTC boundary: the universe is rebuilt by its mode (a live control)
    interesting_count: int = _env("INTERESTING_COUNT", 40)        # how many "interesting" slots the universe keeps beyond core and holdings (a live control)
    interesting_min_dollar_volume: float = _env("INTERESTING_MIN_DOLLAR_VOLUME", 2e7)  # a radar name needs this much daily dollar volume to be a candidate
    interesting_grace_days: int = _env("INTERESTING_GRACE_DAYS", 14)   # a promoted name is demoted after this many days without ranking in the top 1.5 x count
    interesting_min_tenure_days: int = _env("INTERESTING_MIN_TENURE_DAYS", 5)  # ... and never before this many days in the universe
    radar_top: int = _env("RADAR_TOP", 250)                  # movers watchlist size (CoinGecko serves 250 per page)
    overlay_enabled: bool = _flag("OVERLAY", "1")            # the crowd overlay: margin of safety and fade-the-crowd on top of the raw call
    overlay_aggr: float = _env("OVERLAY_AGGR", 0.7)          # 0..1: how hard the overlay fades crowded trades
    overlay_fade_at: float = _env("OVERLAY_FADE_AT", 0.45)   # crowding magnitude above which aligned trades get faded
    overlay_safety: float = _env("OVERLAY_SAFETY", 0.5)      # margin-of-safety penalty on asymmetric downside

    # News skimmer
    news_enabled: bool = _flag("NEWS", "1")
    news_minutes: float = _env("NEWS_MINUTES", 3.0)
    news_scorer: str = _env("NEWS_SCORER", "finbert")     # headline tone: finbert (ProsusAI/finbert on the CPU, graded) or lexicon (word lists)
    finbert_model: str = _env("FINBERT_MODEL", "ProsusAI/finbert")
    news_browser: bool = _flag("NEWS_BROWSER", "1")
    news_off: str = _env("NEWS_OFF", "")           # news source ids off by default: browser
    news_sources: str = _env("NEWS_SOURCES", "")   # "Name|https://url,Name2|https://url2" to override the browser defaults

    # Narrative brief (a LOCAL model only; never a cloud API)
    ollama_host: str = _env("OLLAMA_HOST", "http://localhost:11434")
    brief_model: str = _env("BRIEF_MODEL", "qwen3.5:latest")
    brief_small_model: str = _env("BRIEF_SMALL_MODEL", "qwen3.5:latest")
    brief_minutes: float = _env("BRIEF_MINUTES", 15.0)
    brief_timeout: float = _env("BRIEF_TIMEOUT", 1800.0)
    brief_enabled: bool = _flag("BRIEF_ENABLED", "0")
    brief_backend: str = _env("BRIEF_BACKEND", "ollama")     # "ollama" or "openai" (llama-server / vLLM, local only)
    brief_openai_base: str = _env("BRIEF_OPENAI_BASE", "http://localhost:8001/v1")

    # Server / UI
    host: str = _env("HOST", "127.0.0.1")
    port: int = _env("PORT", 8100)                 # 8100: Flint owns 8000 on a box that runs both
    tick_hz: float = _env("TICK_HZ", 2.0)
    chart_bars: int = _env("CHART_BARS", 160)   # ~13h at 5-min bars
    paper_start: float = _env("PAPER_START", 100000.0)  # starting cash for the paper-trading book
    paper_perp_cash_frac: float = _env("PAPER_PERP_CASH_FRAC", 0.5)  # the share of the paper book that starts on the perp venue as collateral (the rest on the spot venue)
    transfer_asset: str = _env("TRANSFER_ASSET", "USDC")  # the stablecoin cash moves between venues as (fees.RAILS)
    min_transfer_usd: float = _env("MIN_TRANSFER_USD", 250.0)  # the smallest transfer the paper book makes between its venues
    log_size: int = 60

    # Persistence
    state_dir: str = _env("STATE_DIR", "state")
    checkpoint_minutes: float = _env("CHECKPOINT_MINUTES", 5.0)
    replay_save_minutes: float = _env("REPLAY_SAVE_MINUTES", 30.0)  # the replay buffer (tens of GB when full) is written at most this often, and at shutdown
    backtest_weekly: bool = _flag("BACKTEST_WEEKLY", "1")       # walk-forward backtest over the replay once a week (a live control; action "backtest" runs one any time)
    backtest_weekday: int = _env("BACKTEST_WEEKDAY", 5)         # 0 = Monday ... 6 = Sunday (UTC); Saturday is the quietest crypto day
    backtest_hour: int = _env("BACKTEST_HOUR", 0)               # UTC hour the weekly run may start
    backtest_hours: float = _env("BACKTEST_HOURS", 12.0)        # time budget for a run; the per-day training budget shrinks to fit it (0 = uncapped); live training waits meanwhile
    backtest_night_epochs: float = _env("BACKTEST_NIGHT_EPOCHS", 1.0)  # passes over the history seen so far per simulated day, as the background trainer does per day
    backtest_days: int = _env("BACKTEST_DAYS", 0)             # score only the last N days (0 = all timestamped history)
    backtest_cost_bps: float = _env("BACKTEST_COST_BPS", 8.0)  # round-trip cost per unit of position in the backtest (the live book charges the quoted spread)
    backtest_warm_days: int = _env("BACKTEST_WARM_DAYS", 7)   # training-only days before scoring starts, like warmup
    backtest_target: str = _env("BACKTEST_TARGET", "both")    # absolute | relative | both (two runs, each on half the budget, reported side by side)
    memory_floor_gb: float = _env("MEMORY_FLOOR_GB", 10.0)   # GB that must stay free: the backtest sizes its net to fit above it and stops below it; deep paging pauses near it (a live control)

    def __post_init__(self):
        if not self.sources_on and self.feed and self.feed != "auto":
            self.sources_on = self.feed
        if not self.backfill_seconds or self.backfill_seconds <= 0:
            self.backfill_seconds = float(self.backfill_days) * 86400.0

    def replay_bytes_per_bar(self, n_features: int) -> int:
        """What one bar costs in the replay: the feature row plus the label, mask and flags per name."""
        return max(1, self.n_assets) * (int(n_features) * 4 + 10) + 40

    def replay_bars(self, n_features: int) -> int:
        """The replay capacity in bars: replay_gb spread over the current universe when set (so
        growing the universe shrinks the bar count instead of the free memory), else
        replay_size; never more than replay_days of bars."""
        gb = float(getattr(self, "replay_gb", 0.0) or 0.0)
        cap = max(256, int(gb * 1e9 // self.replay_bytes_per_bar(n_features))) if gb > 0 else int(self.replay_size)
        days = float(getattr(self, "replay_days", 0.0) or 0.0)
        if days > 0:
            cap = min(cap, int(days * self.bars_per_day))
        return max(1, cap)

    def replay_windows(self, n_features: int) -> int:
        """Older name for replay_bars: the replay holds bars now, and a window is built per sample."""
        return self.replay_bars(n_features)

    @property
    def horizons(self) -> tuple[int, ...]:
        """Every horizon the net forecasts, the policy's first."""
        extra = [int(float(h)) for h in str(getattr(self, "extra_horizons", "") or "").split(",") if h.strip()]
        return (int(self.horizon),) + tuple(h for h in extra if h > 0 and h != int(self.horizon))

    @property
    def bars_per_day(self) -> int:
        return max(1, int(round(86400.0 / float(self.bar_seconds))))

    @property
    def n_assets(self) -> int:
        return len(self.symbols)

    @property
    def n_quantiles(self) -> int:
        return len(self.quantiles)
