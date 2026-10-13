# Steel

Steel is a self-hosted, continuously-learning crypto market model: Flint's architecture (the
sibling project in `../flint`) pointed at a market that never closes. A PyTorch net (SteelNet)
forecasts each name's return distribution over the next hour on 5-minute bars, trains online as
its own forecasts resolve, and serves everything to a no-build browser dashboard over FastAPI and
a websocket. Everything runs on the local machine. It is a research toy, not advice.

## Running and restarting

`uv run steel` starts the server on 127.0.0.1:8100 (Flint owns 8000 on a box that runs both). It
refuses to start if that port is already in use — a second instance would run the GPU autotune
(thrashing the first) before failing to bind.

To restart during development, kill the old process first:

```
pkill -9 -f "python -m steel"
```

Wait for :8100 to free, then relaunch. One-off benchmark scripts run as `.venv/bin/python -`,
which that pattern won't match — kill those by PID. Run only one GPU benchmark at a time, and
never next to Flint's own autotune: two ladders on one GPU thrash memory into swap.

Every restart re-runs the startup backfill (`backfill_days`, 30 by default: a couple of minutes
from Coinbase for 60 names) and the go-live warmup (`warmup_epochs` passes over that history with
the held-out guard, under `max_warmup_minutes`), so a restart on the Spark is ten to thirty
minutes without forecasts. "Trusted" comes from the held-out scorecard first (`rescore_holdout`:
the 5% of bars never trained on over the last `trust_days`, scored the way live labels are; at
least `min_labels` judged calls at `min_hit_rate`), then from live labels once `min_labels` of
them mature, so the paper book trades minutes after a restart when the history says the net has
skill. Avoid restarting unless a change actually requires it.

On the Spark (hostname `omega`, 192.168.1.128), Steel is an always-on systemd user service,
`deploy/spark/steel.service`, that runs `deploy/spark/steel-start --foreground` (which `install.sh`
copies to `~/.local/bin/steel-start`; the Spark's `STEEL_*` environment lives in that file).
`steel handoff` shows which GPU mode the running Steel is in (solo while Flint is off: a triple
training budget and a low memory floor; shared while Flint runs or is about to). `journalctl --user -u steel -f` follows the log and `systemctl --user stop steel` stops it; a stop
returns in seconds and saves the checkpoint on the way out. Started by hand, `steel-start` launches
the transient unit `steel-run` instead. Flint shares the box: it runs weekdays as `flint-run` with
about 50 GB on the GPU, so `steel-start` budgets a 12 GB replay, a 12 GB memory floor and 120 names,
and `steel check` names `flint-run.service` (and any `llama-server@*`) when a start would not fit.

## Layout

- `steel/engine.py` — orchestrator: bar clock, forecasting, online training, the periodic loops
  (signals, market scan, news, checkpoints, deep backfill, background training, the weekly
  backtest, the universe refresh, the account poll), paper rebalancing, snapshot/publish, and the
  `KEY_SERVICES` registry.
- `steel/model.py` — SteelNet (dilated causal convs, cross-asset attention, regime-gated experts,
  non-crossing quantiles) and the loss. FlintNet plus a horizon axis: `n_horizons` quantile sets
  and tail pairs from the same trunk (`cfg.horizons`: the policy's 1-hour horizon first, then
  `extra_horizons`, 4 h and 24 h by default); the loss weighs the extras `HORIZON_WEIGHT` each.
  The extra horizons' labels come from the closes the bar store keeps (`BarStore.labels_at`),
  so they need no pending window of their own; `Prediction.horizons` carries their forecasts
  and the dashboard shows them under the policy's. Only the first horizon is ever traded.
- `steel/learner.py` — the replay (a `BarStore`: one row per bar with its label, mask, timestamp
  and links to the bars before and after it; windows are gathered by index at batch time and
  are valid only over `window` time-contiguous bars) and the online optimizer; atomic,
  corruption-resilient checkpoints. Consecutive windows share all but one bar, so this holds
  64x the history the window ring did: a year of 400 names is under 4 GB. A saved window ring
  (`x` four-dimensional) is converted on load (`as_bars`); `read_replay` says which `format`
  it found. `replay_gb` sizes the store in bars (`Config.replay_bars`), capped at `replay_days`.
- `steel/backtest.py`, `steel/sweep.py` — walk-forward backtest over the timestamped replay (days
  are UTC days, Sharpe annualizes over 365) and the policy sweep over its stored forecasts.
- `steel/autotune.py` — picks the model preset by benchmarking against a warmup-time budget;
  caches the choice in `state/machine.json`; refuses a start the box cannot hold.
- `steel/features.py`, `steel/bars.py` — per-bar features with running normalization, and tick to
  5-minute-bar aggregation. 9 microstructure features, 13 exogenous ones (news tone and
  attention, Reddit tone and attention, Fear & Greed, funding, open-interest change, long/short
  ratio, basis, DVOL, BTC dominance, breadth, stablecoin flow) and 7 calendar ones
  (`clock.CALENDAR`: hour and day-of-week angles, the funding clock, the CME session, the
  Friday options expiry; a function of the bar's timestamp): 29 in all. A feature-set change
  resets the net but keeps the replay: the feature names travel with it (`replay/meta.json`),
  columns are mapped by name, new ones are zero and calendar ones recomputed from each bar's
  timestamp (`OnlineLearner._copy_replay`).
- `steel/sources/` — one module per venue (a `Source` subclass, base.py), the priority-ordered
  `VENUES` list (registry.py) and `SourceManager` (manager.py: failover per symbol, the merged
  startup backfill, toggles persisted to `state/sources.json`). Coinbase (websocket trades +
  ticker, candles by date range, the deep backfill), Kraken (websocket, 720 candles) and
  Hyperliquid (the one perp venue on by default) are on out of the box; every other venue is
  present but off until toggled (`default_on` on the class; `STEEL_SOURCES_ON=+bitget,+kucoin`
  adds venues at start, a bare list forces exactly those). Spot venues: Coinbase, Kraken, Binance,
  Bybit, OKX, Bitget, KuCoin, Gate.io, MEXC, HTX, Crypto.com, Bitfinex, Gemini, Bitstamp. Perp
  venues (`venue = "perp"`, ranked below every spot venue because a perp is a derivative):
  Hyperliquid, BloFin, Kraken Futures, dYdX, ApeX, Deribit, Aster. CoinGecko is the quote-only
  aggregate. `sources/bitmex.py` exists but is not listed: BitMEX closed its exchange on
  2026-09-22 (every perp settled), so only its balance reader is registered. No venue needs a key
  for market data. `steel venues` lists them all. The startup backfill runs in two waves: every
  spot venue concurrently, then the perp venues and the simulator only for the names still empty
  (Hyperliquid's rate budget serializes its candle requests: two minutes for 60 names).
- `steel/market.py`, `steel/signals.py` — the whole-market scan (CoinGecko's top 250: radar,
  breadth, category rotation, dominance; DefiLlama stablecoin supply) and the exogenous signals
  (ApeWisdom Reddit chatter, alternative.me Fear & Greed, Bybit + Hyperliquid perp positioning,
  Deribit DVOL).
- `steel/paper.py` — the paper-trading book: spot longs, shorts as isolated-margin perps (bounded
  loss), funding accrued, persisted in the checkpoint.
- `steel/policy.py` — the policy's cost and exit arithmetic as pure functions: impact, Amihud,
  funding carry, the liquidation price, the exits.
- `steel/accounts/` — read-only balance readers for the Portfolio tab, one module per venue (an
  `Account` subclass, base.py) behind one HTTP gate: `Account._request` is the only call site in
  the package and refuses any path the reader did not declare in `endpoints`. Spot and perp
  positions (`kind: "perp"`, signed size, entry, mark, leverage) from every venue that has a key
  or, for Hyperliquid and dYdX, just a wallet address. Balance reads only, never orders.
- `steel/vault.py` — the key vault: a master key Steel generates on first start
  (`state/vault.key`, owner-only) seals every `<service>.json` and `<venue>-trade.json` at rest
  (Fernet); plaintext key files found at start are sealed in place; `config._json_file` reads
  through it. The recovery copy is shown once, in the dashboard, until the operator acknowledges
  saving it; after that nothing prints it (`steel vault status|rotate|restore <key>`;
  `STEEL_VAULT_KEY` for a restore on a new box). The API never returns a key in full.
- `steel/auth.py` — the operator token: `STEEL_TOKEN`, else generated at first start and sealed
  in `state/token.json`; `steel token` prints it (`steel token rotate` replaces it). `STEEL_AUTH`
  is `mutate` by default (every `POST /api/*` needs `Authorization: Bearer` or `?token=`), `all`
  closes the reads and the websocket too, `off` opens everything. The dashboard asks for the token
  once on a 401 and keeps it in the browser (`localStorage` `steel.token`). The page itself is
  always served.
- `steel/alerts.py` — Telegram, Discord and plain-webhook alerts: the "Alerts" KEY_SERVICES entry
  (sealed `alerts.json`); every error-level trace line through `Trace.on_emit`, plus explicit
  sends for stalls, trusted BUY/SELL flips, paper fills, live fills and refusals, trust changes,
  arm/disarm and a pending vault key. Queued, drained every 3 s by `_alerts_loop` into one
  message per destination, throttled per kind and key (`THROTTLE`; the error kinds never wait).
- `steel/clockcheck.py` — the box clock against Binance, Bybit and Coinbase server time (the
  median), hourly from `_clock_check_loop`; `ExecutionGate.check` refuses every order while the
  offset passes `clock_max_offset_s` (1 s). It lives outside `steel/execution/` because that
  package may not talk to the network except through the gate.
- Fee tiers, monitoring, tickets, exports (engine.py): `_fees_loop` reads every configured
  reader's `fees()` once a day and `apply_fee_tier` puts the trade venues' real rates into
  `self.fees` and the paper book (an explicit `spot_fee_bps`/`perp_fee_bps` control still wins;
  `state/fees.json` remembers a week). `monitor()` serves Research's calibration-by-name, feature
  drift (the last day of standardized replay rows) and the daily scorecard
  (`state/scorecard.jsonl`, `_record_scorecard` at the checkpoint). `ticket(sym)` is a human's
  order ticket for the current call (`/api/ticket/{sym}`, the Ticket button in a name's detail).
  `_record_account_history` keeps `state/account_history.jsonl` (one row per poll, five-minute
  spacing) for Book's real-equity chart, and `export_csv` serves
  `/api/export/{paper-trades,paper-transfers,account-history,orders,arb}.csv`.
- `steel/handoff.py` — the GPU handoff with Flint, without swapping the model: `_handoff_loop`
  checks every minute whether `flint-run` is up (unit, process, port 8000) or starts within
  `handoff_lead_minutes` of its timer (`handoff_schedule`, local time) and switches between
  `shared` (the base `train_epochs` and `memory_floor_gb`, the backtest deferred, and in the
  lead-up the background trainer idle and the CUDA cache released) and `solo` (`train_epochs` x
  `solo_train_multiplier`, `solo_memory_floor_gb`). A control change of either knob rebases the
  modes. `steel handoff [auto|shared|solo] [--wait]` talks to the running box with the operator
  token; `deploy/spark/steel-handoff` is the same for Flint's start script; the Ops Execution
  panel shows the GPU line with a pin button.
- `steel/arbreplay.py` — the arbitrage rules replayed over `state/dislocations.jsonl` by latency
  rung (50 ms to 3 s): an episode counts when it outlasted the round trip, at its mean net edge,
  at the smaller of the book's size and `arb_max_order_usd`, after the per-name cooldown, and
  every `2 x arb_max_order_usd` of one-way flow on a pair charges an on-chain inventory rebalance
  (two `transfer_cost` legs). Episodes wider than `max_edge_bps` (100) are data problems, counted
  per venue, never captured. `steel arb-replay`, `/api/arb/replay` (cached 10 min), the Research
  panel.
- `steel/probe.py` — `steel probe`: every venue against the contract its code expects (product
  list, an hour of BTC-USD history, a live tick; every reader's and order client's declared path
  unauthenticated, GET only for order clients, so nothing is ever sent). Nightly in
  `.github/workflows/probe.yml`; exit 1 on a default-on source or a trade venue the book uses.
- `steel/backup.py` — `steel backup [dir] [--replay]` and `steel restore <archive>`: one tar.gz of
  the checkpoint, vault key, token, sealed key files, the live book, the order and arbitrage
  ledgers, universe, source toggles, identity book and machine cache. `_backup_loop` writes one a
  day into `STEEL_BACKUP_DIR` when set, keeping `backup_keep`.
- `steel/live.py` — the live book and the promotion rule. `Promotion.check` reads the weekend
  backtest's totals and the paper book's record (trusted, scored days, net bps per day, hit
  rate, days running, per-bar Sharpe, drawdown, return) into a checklist; the operator promotes
  from the Ops tab (a failing checklist needs `force`), never the code. While live, every paper
  fill becomes an order of `live_scale` times its notional through the execution client of the
  venue that side trades on, through the gate like any order; the book keeps what it expects to
  hold, reconciles it against the readers' positions on every portfolio poll, reports slippage,
  and demotes itself at once on the kill switch, a live drawdown past `live_max_drawdown`, or a
  position drift past `live_drift_tol`. `state/live.json`.
- `steel/arb.py` — the arbitrage executor, off unless `arb` is on (and execution is). The
  dislocation monitor's `on_open` hands it every opening; when the net edge clears
  `arb_min_edge_bps`, both venues are armed with order clients, both legs are the same kind
  (a spot-against-perp gap is a basis trade and is refused) and the readers' last balances can
  fund the legs, it sends a buy on the low-ask venue and a sell on the high-bid venue at once
  (`arb_max_order_usd` or the top of book), through the gate. A leg that filled while the other
  failed is flattened at once with a reduce-only order and counted as `unhedged`; a perp pair is
  unwound with two reduce-only orders when the episode closes (`on_close`); a spot pair moved
  inventory and stays. `state/arb.jsonl`; the Ops tab has the switch.
- `steel/identity.py` — asset identity: CoinGecko's top 1,000 by symbol (a symbol with several
  ranked coins is `ambiguous`) and the names venues publish (Coinbase today) to resolve a listing
  to one coin. An ambiguous name pairs in the dislocation monitor only between venues that
  resolved to the same coin, is served by the manager only from a venue that resolved it
  (`identity_ok`), and the Names detail says which coin it is. `state/identity.json`, daily.
- `steel/dislocation.py` — the cross-venue dislocation monitor: latest bid/ask per venue per
  name, episodes with their execution window, the jsonl log and the stats the dashboard shows.
- `steel/sources/fees.py` — the fee profile per venue (taker/maker bps, funding interval,
  maintenance margin) the policy and the paper book charge.
- `steel/clock.py` — UTC-day arithmetic and the weekly backtest window. There is no session.
- `steel/server.py` — FastAPI routes and the `/ws` websocket.
- `steel/config.py` — every knob, each reading a `STEEL_*` env var or a json file, with a default;
  `canonical()` turns `btc`, `ETH/USD`, `SOLUSDT` into `BASE-USD`.
- `web/` — the dashboard: plain HTML/CSS/JS, no build step, served static. Six views, by what the
  operator looks at (`VIEW_OF` in app.js maps each panel to its view; `vis(part)` gates a
  renderer): Market (whole market, dislocations, watch list, radar, news, the brief), Names (the
  cards, the detail, the universe), Book (paper equity, positions, trades, exchange balances),
  Venues (sources, signals, keys), Research (backtest, sweep, the model's tiles and log), Ops
  (controls, brief control, execution and its kill switch, the consoles, the terminal). Old
  hashes and saved views (`#dashboard`, `#consoles`, ...) map through `LEGACY_VIEW`.

## Invariants worth keeping

- Symbols are canonical `BASE-USD` everywhere inside the app (`config.canonical`); each source maps
  to its venue's spelling (`BTC/USD` on Kraken's websocket, `XBTUSD` on its REST, `BTCUSDT` on
  Binance, Bybit, Bitget and MEXC, `BTC-USDT` on OKX and KuCoin, `BTC_USDT` on Gate.io, `tBTCUSD`
  on Bitfinex, `PF_XBTUSD` on Kraken Futures, `XBTUSDT` on BitMEX, the bare `BTC` on Hyperliquid;
  USDT- and USDC-quoted pairs count as USD). Small-price contracts (`1000PEPE`, `kPEPE`) go through
  `sources.unscale`: the price is divided and the size multiplied before a tick is emitted. A new
  name is only promoted or added when an enabled live venue lists it (`SourceManager.tradable`):
  every venue's instrument list is fetched once an hour.
- The market never closes, so nothing waits for a session. The start pages `backfill_days` of
  history for every name from the spot venues (perp venues and the simulator only for names left
  empty), then `_warmup` runs `warmup_epochs` passes over the labeled bars (never fewer than
  `warmup_steps`) with a held-out check every 40 steps that ends it when the held-out pinball
  stops improving (`overfit_patience`), or at `max_warmup_minutes`; `status.warmup` carries the
  progress to the loading screen. Only then does it forecast. The deep backfill pages Coinbase
  history in `deep_chunk_days` chunks every `deep_backfill_seconds` (5 days, 150 s: a 60-name
  burst of 300 requests at 6 in flight stays under Coinbase's public 10/s) and trains 8 steps per
  chunk off the event loop; `_train_loop` (Flint's idle trainer) trains up to `train_epochs` passes
  over the replay per UTC day, in 4-step chunks so a forecast waits one step at most, with the
  held-out overfit guard; the weekly backtest starts inside a window (`backtest_weekday` at
  `backtest_hour` UTC, Saturday 00:00 by default, the thinnest tape) and owns the GPU while it
  runs, as in Flint. The daily loss limit, the universe refresh and the training budget all turn
  over at 00:00 UTC (`clock.utc_day`).
- A stall (no bar for three intervals while prices tick) is always an alarm: there is no closed
  market to excuse it.
- `steel/dislocation.py` watches every enabled venue's top of book (the manager's `on_book` hook
  fires for every live venue's quote, active or standby) and opens an episode when the best bid
  across venues stands above the best ask across venues net of both venues' taker fees and
  `dislocation_latency_bps`, within the same dollar (USD, USDT, USDC) and the same kind (spot
  against spot, perp against perp); it closes when the gap is gone, and the episode's window is
  what an executor would have had to act in. Closed episodes go to `state/dislocations.jsonl`,
  `/api/dislocations` and the dashboard's dislocations panel (windows, peaks, per venue pair).
  It measures; it never executes. Cross-stablecoin and spot-perp gaps are deliberately not
  episodes (a basis to carry, not a round trip).
- Every venue declares its `quote` (USD, USDT or USDC). A standby venue may refresh a name's
  bid/ask only when its quote matches the active venue's (`SourceManager._route`): on the first
  all-venue smoke run a USDT book put BTC's spread 10 bps above Coinbase's last trade, which the
  policy would have read as the cost of a fill.
- Candles build only from real trades (`Tick.quote is False`). Ticker, book-ticker and CoinGecko
  ticks update price and bid/ask but never build bars. Coinbase's `match.side` is the maker's side,
  so `side == "sell"` is a taker buy; Kraken, Bybit and OKX report the taker's side directly; a
  Binance trade with `m` true had the buyer as maker, so it is a taker sell.
- Every simulated action pays what the real one would. A paper fill pays the venue's taker fee
  (`fees.fee_profile` for `trade_venue_spot` / `trade_venue_perp`), the half-spread from the live
  book, and funding pro rata on open perps. Money between venues is an on-chain transfer: the
  paper book keeps spot cash on the spot venue and collateral on the perp venue
  (`paper_perp_cash_frac` splits the start), and when a position needs more than its venue holds
  it sends the shortfall as `transfer_asset` (USDC) on the cheapest rail in `fees.RAILS`
  (`transfer_cost`), pays that rail's fee, and waits for the wait (`minutes`) before the position
  can open: the signal has to still be there when the money lands (`PaperBook._need`, `_land`,
  `in_flight`). One venue for both classes means one pool and no transfers. The arbitrage
  executor never pre-funds a leg: a missing balance is refused with the transfer it would take,
  its cost in bps of the trade and its minutes (`ArbExecutor._funded`). STEEL_FEES `transfers`
  overrides an asset's rails (Lightning for BTC, say). `tests/test_transfers.py`.
- No position may carry unlimited risk, ever. A bearish view is an isolated-margin perp short at
  `short_leverage` (2x): the margin posted, `notional / leverage`, capped at `short_max_frac` of
  equity, is the most it can lose, because the venue liquidates it at `entry × (1 + (1 -
  maintenance) / leverage)` (`policy.liquidation_price`) and the book settles that at exactly the
  margin (`PaperBook._settle_liquidations`). A bullish view is, in `long_mode=perp` (the
  default), a perp long at `long_leverage` (1x: no liquidation price, the stake is the worst
  case, the same as spot) on the perp venue, or spot on the spot venue in `long_mode=spot`.
  There is no naked borrow and no leverage on the book. The book's store is `perps` (`side`
  +1/-1); `shorts` and `longs` are views; an old checkpoint's `shorts` loads. Funding accrues on
  every open perp at the venue's current rate, pro rata per bar: a positive rate pays the shorts
  and charges the longs (`_accrue_funding`); the policy charges the carry a perp would pay
  (`funding_in_edge`) and never credits one it would earn. Paper only trades once the model is
  `trusted`.
- Fees are the venue's, not a flat number: `steel/sources/fees.py` holds a retail base-tier
  profile per venue (taker and maker bps for spot and perps, the funding interval, the
  maintenance margin; `STEEL_FEES` names a json override file), and `trade_venue_spot`
  (Coinbase) / `trade_venue_perp` (Hyperliquid) say where a position would trade. The policy
  charges two taker legs of the venue a side trades on plus the funding it would pay, on top of
  the spread, impact and Amihud; the book charges the same on its fills (`spot_fee_bps` /
  `perp_fee_bps` override the profiles; `funding_interval_hours` 0 takes the venue's). A retail
  spot tier (40 bps a side on Coinbase) therefore never looks tradable at a one-hour horizon,
  which is the point: longs go to the perp venue. `tests/test_fees.py` pins it.
- Risk gates in the policy (all live controls; the model is untouched): a name needs `skill_min_n`
  matured direction calls at or above `min_hit_rate` (the held-out scorecard's calls on that name
  stand in until live calls fill the record, then age out of it); the same side must persist `confirm_bars`; a
  position is held `min_hold_bars` (`short_min_hold_bars` for a short) unless the signal reverses;
  the live spread replaces `cost_bps` when larger and `max_spread_bps` blocks wide books; a name
  trading under `min_dollar_volume` a day (from the bars held) does not trade (Flint's `min_price`
  made no sense here: DOGE at $0.19 is perfectly liquid); the square-root impact of the order and
  Amihud's illiquidity (`max_impact_bps`) are charged and gated as in Flint; stops at
  `stop_loss_bps` (300 by default, wider than equities), targets and time limits apply to shorts
  too (`exit_decisions` takes the book's shorts and flips the sign); a cooldown follows an exit;
  `daily_loss_limit` (3%) stops new entries for the UTC day. All of it is `steel/policy.py` and
  `Engine._suggest`, with `tests/test_policy.py` and `tests/test_gates.py`.
- Sizing and clusters (all live controls): `vol_target` scales a position by the daily vol it is
  sized for over the name's realized daily vol (`policy.sigma_daily`, floor 0.25x; 0 = off), so a
  meme coin and BTC carry about the same risk per slot; `horizon_agree` holds a 1 h call whose
  4 h median points the other way; `policy.cluster_caps` then cuts the targets of any sector
  (`_sector_of`: the market scan's categories, else `market.SECTOR_MAP`) over
  `max_cluster_weight` gross and the long-beta names when the book's net BTC beta
  (`_btc_beta_of`, the relative target's loading) passes `max_beta_exposure`; the index hedges
  are exempt. Each cut is written into the name's "why".
- The crowd overlay (`_crowd_overlay`, Flint's council overlay without the 13F council): a margin
  of safety on asymmetric downside, and fade-the-crowd on `crowding`, which is Fear & Greed, Reddit
  attention and tone, funding, the long/short ratio, open-interest change and the basis leaning one
  way. A thin edge into extreme crowding is flipped. `overlay_*` are live controls; the action
  `overlay` toggles it.
- The relative target's "market" is BTC-USD and ETH-USD (`index_symbols`); loadings refit every 288
  labels (a UTC day). The hedge per index is an index short under a long basket and index spot over
  a bearish one, `uncapped` in the book as in Flint.
- Stop and panic: `Engine.shutdown` cancels every open order on each armed venue before saving
  (`LiveBook.cancel_open`), and `STEEL_FLATTEN_ON_STOP=1` closes every live position with
  reduce-only market orders first (`LiveBook.flatten`, also the "Flatten everything" button on
  Ops and `/api/live {action: flatten}`; it demotes to paper when done). The dead-man rule: a live
  book whose `heartbeat` (written at every save, every checkpoint) is older than
  `live_deadman_minutes` at a start comes back demoted, flattened when execution is on. A paper
  box (execution off or nothing armed) skips all of it.
- Account access is strictly read-only; the readers never place, change or cancel an order on
  any venue, and never move funds. Orders exist only in `steel/execution/`, behind
  `ExecutionGate`: off unless `execution` is on, a venue trades only when armed
  (`execution_venues`) with a trade key of its own, the kill switch (a flag, the Ops button, or
  the file `state/KILL`) refuses everything, every cap (`exec_max_*`) must be set, a reject
  starts a cooldown, every request and answer goes to `state/orders.jsonl`, and clients default
  to `execution_dry_run` (signed, logged, never sent). `tests/test_invariants.py` exempts only
  that package from the order-endpoint grep and pins the gate's single HTTP call site. Every reader declares its `endpoints` and goes through
  `Account._request`, which refuses an undeclared path and anything but GET and POST before a
  connection is made. `tests/test_invariants.py` greps every path-like string in the package for
  order, withdrawal and transfer words, checks that every declared endpoint reads like a balance
  or position read, and pins the gate as the only HTTP call site in `steel/accounts/`. Make the
  API keys read-only ("View" on Coinbase, "Query Funds" on Kraken, "Read" everywhere else) and
  nothing here can do more.
- Sources live in `VENUES` (sources/registry.py), readers in `ACCOUNTS` (accounts/registry.py)
  and order clients in `EXECUTORS` (execution/registry.py); `KEY_SERVICES` (engine.py) is built
  from every reader's `describe()`, the CoinGecko key and every client's trade key
  (`<venue>-trade`, its own file, never the read-only key), all optional. A key is `<id>.json`
  in the working directory (gitignored; `tests/test_keys.py` checks every file is), sealed by the
  vault, or `STEEL_<ID>_<FIELD>` in the environment (`config.read_creds`).
  `set_key` writes the file and `_apply_key` pushes the values into the reader (`configure`) or
  the market scanner, so a key saved in the panel works without a restart; `_key_values` has an
  entry per service. The Coinbase field is a PEM private key; a one-line field carrying `\n`
  escapes is unescaped. `tests/test_keys.py` walks KEY_SERVICES for the save, the panel status,
  the restart read-back through `read_creds` and the live apply. Adding a venue is one module in
  each package plus a line in each registry: the panel, the Portfolio tab, `steel venues` and the
  tests pick it up from the class attributes.
- The universe, in `liquid` mode (the default), is the configured core (the 60-name `CORE`:
  always modeled), every holding (the account poll adds a new holding within a minute) and
  every dollar pair the enabled spot venues list whose daily dollar volume clears
  `model_dollar_volume` ($1M; about 120 Coinbase names today), by volume, under `max_universe`
  (512: a hard cap, not a target). `SourceManager.volumes` asks each enabled spot venue for its
  listed pairs' 24-hour dollar volume (Coinbase: `/products/volume-summary` in base units times
  a known price, with one ticker read per unpriced name; Kraken: `/0/public/Ticker`, volume
  times VWAP) and keeps the most any venue reports; `liquid_universe` hands the names above the
  floor to `pick_universe`, whose grace and tenure rules mean a thin day never churns the
  universe (every demotion forfeits a learned row); stables and fiat pairs (Coinbase lists
  EUR-USD and GBP-USD as USD products; `sources.asset_class`) are never modeled and leave at
  the next pass if an older rule let them in. The pass runs once a day and within minutes
  of a fresh start. `core` mode is Flint's rule: core, holdings and up to `interesting_count`
  promoted names scored by `universe_candidates` (radar movers by move x dollar volume, and
  names in headlines that are on the radar), after 01:00 UTC once the radar and the news skim
  have reported. The trading floor (`min_dollar_volume`, $2M) is separate from the modeling
  floor. Promotion and demotion, `state/universe.json`, the checkpoint and replay migration on
  a universe change, and the deep re-page for new names are Flint's.
- Everything about the model, the checkpoint migration, the memory guard, the backtest owning
  the GPU, the CUDA cache discipline and the autotune cache key is inherited from Flint and
  documented in `../flint/CLAUDE.md`; it applies here unchanged. A change of window or feature
  set resets the model once (backed up to `model.pt.bak`); a symbol-list change migrates it.
  The replay differs from Flint's: it is a bar store (above). A window's first `add` stores its
  lead-in bars unlabeled; a restart's re-walk of the same days adds nothing (dedupe on the
  labeled end bar); a universe change leaves a new name's columns empty (`has` False) until the
  deep backfill's re-walk `fill`s them; every 20th bar by timestamp is held out of training;
  an untimed window (tests only) lives on virtual negative timestamps. The backtest and
  `steel backtest` read the store (`BarStore.from_replay`, or a window array through `WindowSet`).
- The news skimmer renders the crypto desks (CoinDesk, Cointelegraph, The Block, Decrypt, Google
  News crypto) in headless Chromium; headline tone is FinBERT on the CPU, as in Flint. Names match
  through `ASSET_TERMS` aliases (bitcoin, ether, solana, …) or as upper-case words of three or
  more letters outside `TICKER_STOPLIST`; two-letter names (OP, ZK) and stoplisted words only match
  with a dollar sign (`$OP`). `headline_tickers` in engine.py applies the same rule for the universe.
- The narrative brief runs on a **local** LLM backend (Ollama, or llama-server/vLLM through
  `brief_backend=openai`). Never route it to a cloud API.
- A venue stream's quirks live in its module docstring and `tests/test_sources_*.py`: taker side
  semantics (Coinbase's `side` is the maker's; Binance's and Aster's `m` mean the buyer was the
  maker; most venues report the taker; Bitfinex signs the amount; Bitstamp's `type` is 0 for a
  buy; Hyperliquid's `B` is a taker buy; MEXC's `tradeType` 1 is a taker buy), candle row order
  (KuCoin is open-close-high-low, Gate.io quote-volume first, Bitfinex open-close-high-low, BitMEX
  stamps the bucket end), compressed or binary frames (HTX gzips every message; MEXC's stream is
  protobuf, decoded by hand in `sources/mexc.py`), server heartbeats that must be answered (HTX,
  Crypto.com, Deribit, ApeX), per-connection tokens (KuCoin), per-connection subscription caps
  (MEXC 30, Bitfinex ~56, Aster 200: those sources shard names over several sockets), replayed
  history on subscribe that must not be counted (Bitget, HTX, Crypto.com, Bitfinex, Gemini, ApeX,
  Kraken Futures, dYdX skip their snapshots), contract sizing (BloFin and BitMEX trade in
  contracts: `contractValue` / `homeNotional` give base units; Deribit's BTC and ETH perps are
  inverse, amount in USD). A venue that only serves "the last N candles" (Kraken, HTX, Gemini)
  has no `backfill_range` and never serves the deep backfill.
- Account-reader quirks worth knowing: Gemini's nonce is epoch seconds (a key driven with
  millisecond nonces by another client rejects seconds); Bybit's FUND wallet is not read (its only
  endpoint is under `/asset/transfer/`, a path the invariant forbids by word); Deribit signs per
  request (`deri-hmac-sha256`) rather than through `public/auth`; BloFin and OKX back a position's
  base size out of the venue's own numbers because the instrument list is not a balance read;
  Binance on `api.binance.us` reads spot only; the ApeX account field names come from the docs
  and SDK, not a keyed call. Every reader was exercised live with dummy credentials up to the
  venue's "invalid key" answer, which proves the request shape, not the response parsing.
- The simulator never speaks for a name a live venue lists (`SourceManager.fiction`): its
  ticks are dropped and it is never the provider for such a name, so when every live venue is
  stale the provider is nobody and the stall alarm fires. On the first GPU run (2026-10-12) it
  built every name's first live bar from its seeds (BTC 60,000, SOL 150) before the venues'
  sockets delivered, and in a CPU run it served INJ, UNI and LINK for hours after a socket
  dropped; the labels, the live calibration and the paper book all saw prices no market
  printed. A pure simulator run (`--feed sim`, nothing else enabled) is unaffected.
- The simulator (`SimSource`, priority 200) is a fallback, never a peer: its startup backfill is six
  hours at a coarse step, in a worker thread, because five days at the live step for a whole
  universe took minutes of CPU inside the event loop and held the server off its port (the first
  smoke run, 2026-10-11). The manager keeps the richest history per name, so sim ticks only seed a
  name no venue served.
- `backtest_preset_for` must never fall through to the top rung: with auto-sizing off the live
  d_model is no preset, and Flint's version then built a 5XL net for the backtest (a CPU box spent
  its first minute on it). It takes the live net's own shape as `fallback`.
- User-facing text capitalizes "Steel" as a proper noun.

## Working on it

- Inspect live state over the JSON API: `curl -s localhost:8100/api/state` (phase, metrics, bars,
  paper), plus `/api/sources`, `/api/signals`, `/api/keys`, `/api/backtest`.
- On the Spark judge memory from `free -g` system used, never from the process RSS: the CUDA
  allocations live outside it. Flint's day and Steel's night share the box; `steel check` before a
  start.
- Frontend edits are static — hard-reload the tab, no restart. Backend edits need a restart.
- A CPU smoke run against the real feeds, with no autotune and a few names, is the way to see the
  pipeline work without touching the GPU:
  `STEEL_DEVICE=cpu STEEL_AUTO_SIZE=0 STEEL_SYMBOLS=BTC,ETH,SOL STEEL_STATE_DIR=/tmp/steel-smoke STEEL_NEWS=0 STEEL_DEEP_BACKFILL_DAYS=0 uv run steel`.
- Tests: `STEEL_DEVICE=cpu uv run pytest -q tests`. They cover the memory guard, the paper book's
  bounded-risk rules (a fuzz, a liquidation, funding), the policy gates in `_suggest`, checkpoint
  mismatch handling, bar building, features, every source's message parsing and the manager's
  failover, the signal providers and the market scan from canned data, the account signing (the
  Kraken vector from its docs, a Coinbase JWT round trip), key saving and live application, the
  weekly schedule, the 24/7 stall rule, and the websocket handler returning on disconnect, all
  without loading a model or opening a feed. Add a test when you touch one of those.
- Python ≥3.11, managed with uv. No logins to run: market data needs no key, and the optional
  account keys are pasted in on the Venues tab.
