# Steel roadmap

Steel is Flint's architecture pointed at crypto. Below is the list of proposals for what to tackle
next, for the partners to prioritize (2026-10-11). Each is a hypothesis for the walk-forward
backtest where it touches the model: it ships behind a switch where it can, lands in one commit
with tests, and is judged by a backtest before and after. Items that change the feature set reset
the model once, so they are batched. Effort is a rough size: S (a day), M (a few days), L (a week+).

## G. Crypto-native core (agreed 2026-10-11, being worked in this order)

| # | Item | Status | Notes |
|---|------|--------|-------|
| G1 | Replay bar store | shipped 2026-10-11 | one row per bar (n_assets x features) with the label, mask and timestamp; windows built by index at batch time, valid only over 64 time-contiguous bars. 64x less memory than the window ring: a year of all 403 Coinbase names is 3.7 GB. Migrates an old windows replay by de-overlapping; the backtest reads the store |
| G2 | Deep warmup | shipped 2026-10-11 | backfill `backfill_days` (30) before the first forecast, then `warmup_epochs` passes with the held-out guard instead of 60 steps; go live trained. Autotune sizes the net to the bar cadence, not a 3-second go-live budget |
| G3 | Trust from held-out history | shipped 2026-10-11 | trusted when the held-out last week scores (hit rate, pinball), per name too; live labels keep updating both. A restart or reset no longer blinds the paper book for four hours |
| G4 | Liquidity-floor universe | shipped 2026-10-11 | every USD product on the enabled spot venues above `model_dollar_volume`, refreshed daily; the trading floor stays separate; `max_universe` is a hard cap only. Replaces promote-by-headlines (118 Coinbase names clear $2M/day today) |
| G5 | Venue fee profiles and 1x perp longs | shipped 2026-10-11 | maker/taker bps, funding interval and liquidation formula per venue; the book and the policy charge the venue a position trades on; longs as 1x perps with the shorts' bounded-risk rules; retail spot tiers never look tradable at one hour |
| G6 | Cross-venue dislocation monitor | shipped 2026-10-11 | latest bid/ask and size per venue per name; best bid across venues vs best ask across venues net of both taker fees; every positive episode logged with its duration (the execution window) and shown on the dashboard. Read-only. Cross pairs for triangular steps later |
| G7 | Calendar features | shipped 2026-10-11 | hour of day, day of week, minutes to the next funding stamp (00/08/16 UTC), CME open/close, Deribit expiry Fridays. One reset with G8 |
| G8 | Multi-horizon heads | shipped 2026-10-11 | 4h and 24h quantile heads next to the 1h one; the policy trades 1h, the dashboard shows the rest |

## H. Execution layer (agreed 2026-10-11; after G6 shows what there is to capture)

| # | Item | Status | Notes |
|---|------|--------|-------|
| H1 | Order clients per venue behind a switch | shipped 2026-10-11 (12 clients, dry-run by default, none exercised against a live matching engine; testnets need the partners' demo keys) | `steel/execution/`: place, amend, cancel, open orders, fills per venue on separate trade-permission keys, behind `STEEL_EXECUTION=1` and a per-venue arm. Readers and market data stay read-only; the invariant test is loosened on purpose so only `steel/execution/` may hold an order endpoint. API-key venues first (Binance, Bybit, OKX, Bitget, KuCoin, Gate.io, Coinbase, Kraken, Kraken Futures, BloFin, Deribit) |
| H2 | Kill switch, caps, limits, audit log | shipped 2026-10-11 | a global kill switch (button, API, a file to touch), hard caps per order, name, venue and UTC day, open-exposure and leverage caps, a cooldown after rejects, `state/orders.jsonl` append-only. Nothing executes while a cap is unset |
| H3 | Paper-to-live promotion and reconciliation | shipped 2026-10-11 (checklist, operator-only promotion, mirrored fills, reconciliation, auto-demotion) | live only once the weekend backtest and the paper book clear thresholds over a window; the live book reconciles against real positions and fills every minute, reports slippage against the paper fill, demotes itself on a breach |
| H4 | Wallet-signed venues | shipped 2026-10-11 (Hyperliquid: SDK byte-exact vectors; Aster V3: the docs' EIP-191 recipe, unverified live; dYdX v4: hand-encoded Cosmos txs, pinned vector, never broadcast) | Hyperliquid (EIP-712 with an agent wallet), dYdX v4 (Cosmos transactions), Aster V3 (EVM signer); private keys in their own gitignored files |
| H5 | Cross-venue arbitrage executor | shipped 2026-10-11 (same-kind, same-dollar legs through the gate; inventory from the readers; unhedged legs flattened; perp pairs unwound on close) | acts on G6's episodes from inventory pre-positioned on both venues, only when the net edge clears both fees plus a latency margin measured from the window distribution; rebalances off-cycle; triangular steps once cross pairs stream |

## I. Dashboard

| # | Item | Status | Notes |
|---|------|--------|-------|
| I1 | Navigation by what the operator looks at | shipped 2026-10-11 | Market (whole market, dislocations, watch), Names (cards, detail), Book (paper and live positions, trades, equity, exchange balances), Venues (sources, fees, keys, accounts, dislocation stats, execution arming), Research (backtest, sweep, held-out scorecard), Ops (console, controls, kill switch). Approved 2026-10-11; every panel moves, none is rewritten |

## J. Keys

| # | Item | Status | Notes |
|---|------|--------|-------|
| J1 | Key vault | shipped 2026-10-11 | every key file sealed at rest under a master key Steel keeps (`state/vault.key`, 0600); plaintext files sealed at start; the recovery key shown once in the dashboard until acknowledged; `steel vault status/rotate/restore`; `STEEL_VAULT_KEY` for a new box |

## K. Before execution goes live (agreed 2026-10-11, in this order)

| # | Item | Status | Notes |
|---|------|--------|-------|
| K1 | Dashboard auth | shipped 2026-10-11 | `steel/auth.py`: the operator token (STEEL_TOKEN or generated, sealed in `state/token.json`, `steel token` prints it); `STEEL_AUTH=mutate` (default: every POST), `all` (reads and the websocket too), `off`; the dashboard asks once and keeps it in the browser |
| K2 | Alerts | shipped 2026-10-11 | `steel/alerts.py`: Telegram, Discord, webhook (the "Alerts" entry on Venues, sealed); error-level trace lines, stalls, trusted calls, paper fills, live fills and refusals, trust flips, the vault pending; batched every 3 s, throttled per kind; `/api/alerts` |
| K3 | Shutdown and panic | shipped 2026-10-11 | a stop cancels open orders on every armed venue (`STEEL_FLATTEN_ON_STOP=1` closes positions too); "Flatten everything" on Ops (`/api/live {action: flatten}`); dead-man: a live book not saved for `live_deadman_minutes` (30) comes back demoted, flattened when execution can |
| K4 | Clock drift check | shipped 2026-10-11 | `steel/clockcheck.py`: hourly median offset against Binance, Bybit and Coinbase; the gate refuses orders past `clock_max_offset_s` (1 s); "box clock" on Ops |
| K5 | `steel backup` / `restore` | shipped 2026-10-11 | `steel/backup.py`: tar.gz of the checkpoint (`--replay` adds the replay), vault key, token, sealed key files, ledgers, universe, sources, identity, machine cache; daily into `STEEL_BACKUP_DIR` keeping `backup_keep` (14); `steel restore <archive>` |
| K6 | Arbitrage replay | shipped 2026-10-11 | `steel/arbreplay.py`, `steel arb-replay [--days N] [--latency-ms L]`, `/api/arb/replay`, the Research panel: the executor's rules over `dislocations.jsonl` by latency rung, at the mean net edge, size-capped, cooled down, with on-chain inventory rebalancing charged every `2 x arb_max_order_usd` of one-way flow; episodes wider than 100 bps are counted as data problems (HTX on nearly all of them on the first run) |
| K7 | Nightly venue contract check | shipped 2026-10-11 | `steel/probe.py`, `steel probe [--json] [--only V] [--no-stream]`: every source's product list, an hour of BTC history and a live tick; every reader's and order client's declared paths unauthenticated (401/400/405 = still there, 404 = moved, 451 = geo-blocked); `.github/workflows/probe.yml` nightly at 03:17 UTC, failing on a default-on source or a trade venue |
| K8 | Real fee tiers | shipped 2026-10-11 | `Account.fees()` on 15 readers (each venue's fee endpoint, read-only, signed with the reader's own key); `_fees_loop` once a day applies the trade venues' real rates to the policy's profiles and the paper book (`apply_fee_tier`, `state/fees.json`, a week's memory across restarts); "fee tiers" on Book says table or live tier |
| K9 | Vol-targeted sizing and cluster limits | shipped 2026-10-11 | `vol_target` (5% daily: size x min(1, target / realized daily vol), floor 0.25x, shown in the call's why), `max_cluster_weight` (40% gross per sector from the market scan's categories) and `max_beta_exposure` (net BTC beta 1.0) in `policy.cluster_caps`, applied to the targets before the index hedge; all live controls |
| K10 | Model monitoring | shipped 2026-10-11 | `Engine.monitor` → `/api/monitor` and the Research panel: calibration per name (hit, in-band, error, pnl over the resolved calls), feature drift over the last day of standardized replay rows (shifted / wide / dead flags), and the daily scorecard (`state/scorecard.jsonl`, written at the first checkpoint of each UTC day) |
| K11 | Longer horizons in the policy | shipped 2026-10-11 | `horizon_agree` (on): the shortest extra horizon's median (4 h) must not point against the 1 h call ("the 4-hour view disagrees (-25 bps)") |
| K12 | Multi-venue deep backfill | shipped 2026-10-11 | `SourceManager.backfill_range` pages each name from the first enabled ranged venue that lists it (Coinbase, then Kraken, Bybit, ... ten venues page by date range); `deep_venue` records which |
| K13 | Order ticket hand-off | shipped 2026-10-11 | `Engine.ticket(sym)` → `/api/ticket/{sym}` and the Ticket button in a name's detail: venue and its spelling (the order client's mapper), side, qty, paper and live-scaled notional, limit at the touch, stop, target, time limit, liquidation; copy as text or JSON; nothing is sent |
| K14 | Real account history and CSV export | shipped 2026-10-11 | `state/account_history.jsonl` (one row per poll, 5-minute spacing: total, cash, per-venue equity), `/api/portfolio/history`, the equity-over-time chart on Book; `/api/export/{paper-trades,paper-transfers,account-history,orders,arb}.csv` with Export links |
| K15 | GPU handoff Flint/Steel | shipped 2026-10-11 | `steel/handoff.py`: one model, two modes. Flint detected by unit, process or port, its timer schedule (`handoff_schedule` "Mon..Fri 19:45") for a 15-minute lead-up release; `solo` = train_epochs x3 and a 2 GB floor, backtest allowed; `shared` = the base budget and the 12 GB floor, backtest deferred, background training paused and the CUDA cache emptied in the lead-up. `steel handoff [auto|shared|solo] [--wait]`, `/api/handoff`, `deploy/spark/steel-handoff` for Flint's start script, the GPU line on Ops |
| K16 | Transfer costs and delays in the simulation | shipped 2026-10-11 | `fees.RAILS` + `transfer_cost(asset, qty, px)`: the cheapest rail's fee and wait per asset (STEEL_FEES `transfers` overrides); the paper book keeps spot cash on the spot venue and collateral on the perp venue, moves the shortfall as `transfer_asset` (USDC) when a position needs it, pays the fee, and the position waits until it lands (Book tab: collateral, in flight, transfer fees); the arbitrage executor's unfunded reason names the transfer, its bps and its minutes |
| K17 | HTX quote mapping | queued | HTX is on 186k of the 202k over-wide dislocation episodes in a week: its bid/ask for some names is a different asset or unit; check its symbol map and ticker parsing before trusting any HTX book |
| K18 | `steel backtest --preset` | queued | the CLI takes the cached preset (5XL on the Spark) even on a CPU box, where it is unusable; a `--preset S` flag for offline runs |
| K19 | Paper trades on the GPU run | queued | the model was trusted on both runs but the book never traded: the live calibration was poisoned by the simulator's bars (fixed 2026-10-12, `SourceManager.fiction`); re-check band_scale, p_scale and the move floor against the 5XL net's forecasts on the next GPU run |

## A. Get the baseline

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| A1 | First full run on the Spark + the first weekend backtest | S | Nothing below can be judged without a "before" | 60-name core, the autotuned preset, 180 days of Coinbase history. `steel check` next to a live flint-run; the deploy budgets are a first guess |
| A2 | Real exogenous history in the replay | M | Paged history carries zeros for all 13 exogenous features, so the model learns them from live bars only (weeks to matter) | Reconstructible: Bybit funding history, alternative.me Fear & Greed (full history), Deribit DVOL by range, CoinGecko daily dominance, Hyperliquid funding history. Live-only ones stay in the policy |

## B. Signals and features (model changes, batched into one reset where possible)

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| B1 | Cross-venue price dislocation features | M | With 20 venues streaming, the Coinbase premium (vs Binance/Bybit), the Hyperliquid mark-vs-spot gap and the perp basis across venues are classic crypto flow signals, now free | Needs the manager to keep every venue's last price per name, not only the active one; a `premium` feature per venue class |
| B2 | Consolidated multi-venue tape | M | Today one venue serves each name and the rest are failover. A volume-weighted consolidated bar (trades from every enabled venue, deduplicated by venue) is a better price and a much better volume | Changes bar building; the microstructure features would see all venues' order flow |
| B3 | Liquidation and open-interest flow | M | Forced flow moves crypto: Hyperliquid and Bybit publish liquidations in real time; OI change by venue, and the funding term structure (1h Hyperliquid vs 8h Bybit) | A feature (liquidation volume per bar, signed) and a dashboard event ("$12M of longs liquidated in 5 min") |
| B4 | Order-book features | S | Every venue already streams best bid/ask; the microprice, the quoted spread's dynamics and (on Coinbase/Hyperliquid, with L2) depth imbalance are cheap microstructure features | No new venues; a second websocket channel on two of them |
| B5 | Options surface from Deribit | M | DVOL is one number; the 25-delta skew and the term structure (1w vs 1m IV) say which way the market is hedged | Public endpoints, BTC and ETH only; applies to every name through the index loading |
| B6 | On-chain and flow signals | L | Stablecoin mint/burn (DefiLlama has it), exchange net flows, ETF creations, mempool fees and hashrate | The free-source question for exchange flows is open |
| B7 | Pricing-model feature batch | M | Yang-Zhang / Garman-Klass realized vol, HAR, Amihud and Roll as features, hour-of-week seasonality (crypto has it; equities hide it behind the session) | One reset, batched with B1-B5 |

## C. Policy and the paper book

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| C1 | Policy pass | M | A per-name stop from realized vol rather than a flat 300 bps; a funding-aware hold (a short that earns funding may hold past the forecast); the liquidation distance as a hard floor on short leverage | Live controls; judged by the sweep |
| C2 | Venue-aware paper book | S | Fees, funding intervals and the liquidation formula differ per venue (Hyperliquid 1h funding and its own maintenance margin; Bybit 8h) | The book takes a venue profile; the policy's cost edge follows |
| C3 | Sweep grid sized for crypto | S | `move_floor_bps` and the stop grid were Flint's; crypto's hourly moves are 3-5x wider | Already runs after every backtest; just the grid |

## D. Product: the operator's loop (Steel watches, a human trades in Insilico)

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| D1 | Alerts to Telegram / Discord / Slack / webhook | S | Steel is read-only by design: someone has to act. A trusted BUY/SELL, a stop hit on a paper position, a portfolio position that runs counter to the model, a stall, a liquidation cascade | A local-only notifier module; the message carries the why, the quantiles and the size |
| D2 | Order ticket hand-off | S | Turn a suggestion into a ticket an operator can paste or deep-link into Insilico Terminal: venue, pair, side, size from the policy's weight and the account's equity, the stop and target. Steel still never sends an order | A button on the suggestion and in the alert |
| D3 | Unified portfolio view | M | With ~20 venues readable: one book across venues, net exposure per name, perp vs spot, funding paid, and Steel's view on every holding ("you are long ETH on three venues and the model is bearish") | Builds on the new readers; needs a per-venue PnL since the last poll |
| D4 | Wallet-address holdings | S | Cold storage and DeFi positions through a public address: Etherscan/Solscan balances, Hyperliquid and dYdX already work this way | Read-only by construction |
| D5 | Multi-operator keys and dashboard auth | M | Partners' keys on one box: a login, per-operator key files, a read-only API token for a second dashboard | Today the dashboard is LAN-only with no auth |
| D6 | Mobile shell / PWA | S | Flint's Tauri wrapper was left out; the dashboard works in a phone browser as is; a PWA manifest and the alerts (D1) cover most of the want | |

## E. Data and venues

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| E1 | Long-tail venues | S each | Backpack, Paradex, Lighter (API geo-blocked here), WOO X, Phemex, Bitunix, Poloniex, BingX, CoinEx, XT, Upbit and Bithumb (KRW: the Kimchi premium, B1), Bitvavo (EUR); Aster's wallet-signed V3 account API; Bybit's FUND wallet and OKX's contract sizes through a read the gate accepts | One module each; the venue contract is written and the registries pick a module up |
| E6 | Asset identity across venues | shipped 2026-10-11 (CoinGecko ranks + Coinbase names; other venues publish no name, so an ambiguous ticker only resolves on Coinbase) | tickers are not unique across venues (NIGHT, PONS, US, CHIP mean different tokens on different exchanges), and the dislocation monitor compares by ticker: the first live hour showed 300-bps "dislocations" on such names. Match names by asset identity (CoinGecko id, contract address) before a cross-venue pair counts, and show the identity on the Names detail |
| E5 | Real-key verification of the 22 readers | S | Every reader was exercised live only up to the venue's "invalid key" answer; the balance and position parsing rests on the docs. A read-only key per venue the partners use, and one captured response each as a test fixture | Needs the partners' keys, pasted into the panel on the Spark |
| E2 | Perp data from Binance futures | S | A third opinion on funding and OI, and the taker long/short ratio the retail ratio lacks | fapi.binance.com is reachable from the Spark |
| E3 | Non-dollar quotes | M | EUR/KRW/JPY pairs through an FX rate: more venues per name, and the premiums in B1 | `canonical` leaves other quotes alone today |
| E4 | Venue health and maintenance windows | S | A venue's scheduled maintenance looks like a stall; the venues publish status pages | The stall rule stays strict; the panel says why |

## F. Operations

| # | Proposal | Effort | Why | Notes |
|---|----------|--------|-----|-------|
| F1 | Observability | S | Prometheus metrics (bars, ticks per venue, loss, memory), a health endpoint for the Spark's monitoring, log rotation | |
| F2 | Backtest at scale | M | The weekend run on multi-venue history; a ladder sweep over presets; a report page with the before/after of every item above | |
| F3 | Replay from venue history, not only Coinbase | S | The deep backfill pages one venue; a name not on Coinbase gets its history from the first venue that has it | The manager's `backfill_range` already falls through venues; the deep loop should too |

Baseline: not run yet. The first full walk-forward backtest runs the first Saturday after Steel has
paged its history on the Spark.
