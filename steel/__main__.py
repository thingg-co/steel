"""Entry point: python -m steel [--feed sim|coinbase|auto] [--symbols BTC-USD,ETH-USD] [--port 8100]

`steel venues` lists every market-data source and read-only account reader with its id, default
state and priority."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def _replay() -> None:
    """Print a short report about the saved training state without starting the server."""
    import sys
    import os
    import json
    from datetime import datetime
    from pathlib import Path

    import numpy as np
    import torch

    from .config import Config
    from .features import N_FEATURES

    # Read state_dir from environment first, then default to "state"
    state_dir_str = os.environ.get("STEEL_STATE_DIR", "state")
    state_dir = Path(state_dir_str)

    cfg = Config()
    model_path = state_dir / "model.pt"
    replay_path = state_dir / "replay.npz"

    # Check if model.pt exists
    if not model_path.exists():
        print(f"model.pt: {model_path} does not exist")
        print("No checkpoint found")
        sys.exit(1)

    # Try to load the checkpoint
    try:
        ck = torch.load(model_path, map_location="cpu", weights_only=False)
    except Exception as e:
        print(f"model.pt: {model_path} exists but is unreadable ({type(e).__name__}: {e})")
        print("Corrupt checkpoint")
        sys.exit(2)

    # Load replay.npz
    replay_info = None
    from .learner import read_replay
    if read_replay(state_dir) is not None:
        try:
            z = read_replay(state_dir)
            replay_info = {
                "x_shape": z["x"].shape,
                "y_shape": z["y"].shape,
                "mask_shape": z["mask"].shape if "mask" in z else None,
                "ptr": int(z.get("ptr", 0)),
            }
        except Exception as e:
            replay_info = {"error": str(e)}

    # Extract saved info
    saved_shape = tuple(ck.get("shape", ()))
    saved_symbols = ck.get("symbols", [])
    saved_steps = ck.get("steps", 0)
    saved_labels = ck.get("labels", 0)

    # Get the current window: from machine.json cache if it exists, else cfg.window
    machine_path = state_dir / "machine.json"
    if machine_path.exists():
        try:
            machine = json.loads(machine_path.read_text())
            current_window = machine.get("choice", {}).get("window", cfg.window)
        except (OSError, ValueError, KeyError):
            current_window = cfg.window
    else:
        current_window = cfg.window

    # Get the current symbol list: merge universe.json into config symbols like Engine does
    try:
        from .config import canonical
        saved_universe = [canonical(s) for s in json.loads((state_dir / "universe.json").read_text()).get("symbols", [])]
        extra_syms = [s for s in saved_universe if s not in set(cfg.symbols)]
        room = cfg.max_universe - len(cfg.symbols)
        current_symbols = cfg.symbols + extra_syms[:max(0, room)]
    except (OSError, ValueError):
        current_symbols = cfg.symbols

    current_shape = (len(current_symbols), current_window, N_FEATURES)

    # Check if checkpoint would load or backup
    shape_matches = saved_shape == current_shape
    symbols_match = saved_symbols == current_symbols
    would_load = shape_matches and symbols_match

    print(f"state_dir: {state_dir}")
    print(f"model.pt: {model_path} exists")

    # File age in hours
    try:
        mtime = model_path.stat().st_mtime
        age_hours = (datetime.now().timestamp() - mtime) / 3600
        print(f"checkpoint age: {age_hours:.1f} hours")
    except Exception:
        print("checkpoint age: unknown")

    print(f"saved pairs: {len(saved_symbols)}")
    print(f"saved shape: {saved_shape}")
    print(f"current pairs: {len(current_symbols)}")
    print(f"current shape: {current_shape}")

    if would_load:
        print("config match: yes — would load checkpoint")
    else:
        print("config match: no — would back up to model.pt.bak")

    print(f"steps: {saved_steps}")
    print(f"labels: {saved_labels}")

    if replay_info:
        if "error" in replay_info:
            print(f"replay.npz: unreadable ({replay_info['error']})")
            print("replay windows: 0")
            print("zero labels: N/A")
            print("label mean (bps): N/A")
            print("label std (bps): N/A")
        else:
            x_shape = replay_info["x_shape"]
            windows_stored = x_shape[0]
            print(f"replay: {state_dir / 'replay'} holds {windows_stored} {'bars' if len(x_shape) == 3 else 'windows (old ring)'}")
            print(f"replay windows: {windows_stored}")

            # Calculate zero label fraction and stats
            try:
                y = z["y"][:x_shape[0]]
                total_labels = y.size
                zero_count = np.sum(y == 0.0)
                zero_frac = zero_count / total_labels if total_labels > 0 else 0

                # Convert to bps for mean/std
                y_bps = y[y != 0.0]  # exclude fake zeros for stats
                if len(y_bps) > 0:
                    # labels are stored in bps already; the old x100 made the std read as 75,000
                    label_mean = np.mean(y_bps)
                    label_std = np.std(y_bps)
                else:
                    label_mean = 0.0
                    label_std = 0.0

                print(f"zero labels: {zero_count:,} / {total_labels:,} ({zero_frac * 100:.1f}%)")
                print(f"label mean (bps): {label_mean:.2f}")
                print(f"label std (bps): {label_std:.2f}")
            except Exception as e:
                print(f"label stats: could not compute ({e})")
    else:
        print("replay.npz: none")
        print("replay windows: 0")
        print("zero labels: N/A")
        print("label mean (bps): N/A")
        print("label std (bps): N/A")

    if would_load:
        sys.exit(0)
    else:
        # Even if config mismatch, we could read the checkpoint
        sys.exit(0)


def _sweep() -> None:
    """Sweep the policy's gates over the forecasts the last backtest stored: `steel sweep
    [path.npz] [--cost BPS]`. Seconds on the CPU; writes state/sweep.json and prints the
    proposal, which is never applied by itself."""
    import json
    import sys
    from pathlib import Path

    from .config import Config
    from .engine import clean
    from .sweep import load_forecasts, run_sweep
    cfg = Config()
    state = Path(cfg.state_dir)
    argv = sys.argv[2:]
    cost = float(cfg.backtest_cost_bps)
    path = None
    i = 0
    while i < len(argv):
        if argv[i] == "--cost" and i + 1 < len(argv):
            cost = float(argv[i + 1]); i += 2; continue
        path = argv[i]; i += 1
    if not path:
        cands = sorted(state.glob("backtest_forecasts_*.npz"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print(f"no stored forecasts under {state}; run a backtest first")
            raise SystemExit(1)
        path = str(cands[-1])
    fc = load_forecasts(path)
    print(f"sweeping the gates over {len(fc['day_of'])} forecasts, {int(fc['day_of'].max()) + 1} days ({fc['target']} target), cost {cost:g} bps")
    res = run_sweep(fc, cfg, cost, progress=lambda n: print(f"\r  {n} settings tried   ", end="", flush=True))
    print()
    if res.get("error"):
        print(res["error"]); raise SystemExit(1)
    cur, prop = res["current"], res["proposal"]
    f = lambda r: f"{r['net_bps_per_day']:+.1f} bps/day over {r['days']} days, {r['signals']} signals, Sharpe {r['sharpe'] if r['sharpe'] is not None else '·'}"  # noqa: E731
    print(f"current gates: tune {f(cur['tune'])}; confirm {f(cur['confirm'])}")
    if prop:
        print("proposal: " + ", ".join(f"{k} {cur['knobs'][k]} -> {v}" for k, v in prop["knobs"].items()))
        print(f"          tune {f(prop['tune'])}; confirm {f(prop['confirm'])}")
    print(res["why"])
    out = state / "sweep.json"
    out.write_text(json.dumps(clean(res)))
    print(f"report saved to {out}")


def _backtest() -> None:
    """Walk-forward backtest over the saved replay without starting the server, on the cached
    preset: `steel backtest [--days N] [--hours H] [--cost BPS] [--warm N] [--night EPOCHS]
    [--target absolute|relative|both] [--preset S|M|L|XL|XXL|XXXL|4XL|5XL]`. The cached preset
    (state/machine.json) is the default; `--preset` overrides it for an offline run on a box that
    cannot hold the live size (a CPU box with the Spark's 5XL cache). The live server runs its
    own once a week; do not run this next to one, it loads the replay and a second model."""
    import json
    import sys
    import time

    import numpy as np
    import torch

    from .autotune import configure_backend, pick_device
    from .backtest import combine_reports, run_backtest
    from .config import Config

    cfg = Config()
    state = Path(cfg.state_dir)
    preset = ""
    try:
        choice = json.loads((state / "machine.json").read_text())["choice"]
        for k in ("d_model", "n_experts", "n_heads", "window"):
            setattr(cfg, k, choice[k])
        cfg.dilations = tuple(choice["dilations"])
        preset = str(choice.get("preset", ""))
    except (OSError, ValueError, KeyError):
        print("no cached tuning in state/machine.json; using the config's model size")
    if "--preset" in sys.argv:
        from .autotune import PRESETS
        want = sys.argv[sys.argv.index("--preset") + 1].upper() if sys.argv.index("--preset") + 1 < len(sys.argv) else ""
        row = next((r for r in PRESETS if r[0] == want), None)
        if row is None:
            print(f"unknown preset {want!r}; one of {', '.join(r[0] for r in PRESETS)}")
            raise SystemExit(2)
        preset, cfg.d_model, cfg.dilations, cfg.n_heads, cfg.n_experts, cfg.window = row[0], row[1], tuple(row[2]), row[3], row[4], row[5]
        print(f"preset {preset} by request: d_model {cfg.d_model}, window {cfg.window}")
    from .learner import BarStore, as_bars, read_replay
    z = read_replay(state)
    if z is None:
        print(f"no replay under {state}")
        raise SystemExit(1)
    if not (np.asarray(z["t"]) > 0).any():
        print("the replay has no timestamps (saved before 0.5.10); nothing to walk")
        raise SystemExit(1)
    z = as_bars(z, float(cfg.bar_seconds))
    bars = BarStore.from_replay(z, int(cfg.window), float(cfg.bar_seconds))
    symbols = list(z.get("symbols") or [])
    if not symbols:
        try:
            symbols = list(torch.load(state / "model.pt", map_location="cpu", weights_only=False).get("symbols") or [])
        except Exception:  # noqa: BLE001
            pass
    opts = {"days": int(cfg.backtest_days), "hours": float(cfg.backtest_hours), "cost": float(cfg.backtest_cost_bps),
            "warm": int(cfg.backtest_warm_days), "night": float(cfg.backtest_night_epochs), "target": str(cfg.backtest_target)}
    argv = sys.argv[2:]
    for i, a in enumerate(argv):
        if a.startswith("--") and a[2:] in opts and i + 1 < len(argv):
            opts[a[2:]] = type(opts[a[2:]])(argv[i + 1])
    device = pick_device(cfg.device)
    configure_backend(device)
    mask = np.asarray(z["mask"])
    variants = {"absolute": ["absolute"], "relative": ["relative"], "both": ["absolute", "relative"]}.get(opts["target"].lower(), ["absolute"])
    print(f"walking {int((np.asarray(z['t']) > 0).sum())} timestamped windows on {device} ({preset or 'config'} preset), "
          f"cost {opts['cost']:g} bps, warm {opts['warm']} days, budget {opts['hours']:g} h, target{'s' if len(variants) > 1 else ''} "
          f"{' then '.join(variants)}")

    def prog(day, total, t, info=None):
        info = info or {}
        if info.get("phase") == "warming":
            print(f"\r  warming: training-only day {info.get('history_day')}/{info.get('warm_days')}   ", end="", flush=True)
            return
        print(f"\r  day {day}/{total}  net {t['net_bps']:+.0f} bps  per day {t['net_bps_per_day']:+.1f}  "
              f"hit {((t['hit_rate'] or 0) * 100):.0f}%  signals {t['signals']}   ", end="", flush=True)

    reports = []
    for variant in variants:
        if len(variants) > 1:
            print(f"-- {variant} target")
        reports.append(run_backtest(cfg, bars, device=device, symbols=symbols,
                                    progress=prog, hours=opts["hours"] / len(variants), days=opts["days"], warm_days=opts["warm"],
                                    cost_bps=opts["cost"], preset=preset, night_epochs=opts["night"], target=variant,
                                    index_symbols=str(cfg.index_symbols), beta_windows=int(cfg.beta_windows),
                                    beta_shrink=float(cfg.beta_shrink), store=str(state / "backtest_forecasts")))
        print()
        r = reports[-1]
        if r.get("error"):
            print(r["error"])
            raise SystemExit(1)
        t = r["totals"]
        print(f"{t['days']} days scored in {r['elapsed_s'] / 60:.0f} min [{r['config'].get('target')} target]: net {t['net_bps']:+.0f} bps "
              f"({t['net_bps_per_day']:+.1f}/day, Sharpe {t['sharpe'] if t['sharpe'] is not None else '·'}, "
              f"max drawdown {t['max_drawdown_bps']:.0f} bps), hit {((t['hit_rate'] or 0) * 100):.0f}%, "
              f"coverage {((t['coverage'] or 0) * 100):.0f}%, {t['signals']} signals; always-long {t['long_bps']:+.0f} bps"
              + (f"; unhedged {t['unhedged_bps']:+.0f} bps" if "unhedged_bps" in t and r['config'].get('target') == 'relative' else ""))
    res = combine_reports(reports)
    out = state / "backtest.json"
    from .engine import clean
    out.write_text(json.dumps(clean(res)))
    print(f"report saved to {out}")


def _version() -> None:
    """Print the installed package version and exit."""
    try:
        from importlib.metadata import version
        print(version("steel"))
    except Exception:
        print("unknown")


def _check() -> None:
    """Print what a start would do without loading a model or opening the port."""
    import json
    import sys

    from .autotune import (
        NotEnoughMemory,
        _check_start_memory,
        free_memory_gb,
        other_gpu_tenants,
        pick_device,
    )
    from .config import Config
    from .features import N_FEATURES

    cfg = Config()
    device = pick_device(cfg.device)

    # Device
    print(f"device: {device}")

    # Free memory
    free_gb = free_memory_gb(device)
    print(f"free memory: {free_gb:.1f} GB" if free_gb is not None else "free memory: unknown")

    # Other GPU tenants (llama-server units, Steel)
    tenants = other_gpu_tenants()
    if tenants:
        print(f"other GPU tenants: {', '.join(tenants)}")
    else:
        print("other GPU tenants: none")

    # Check for cached machine.json
    cache_path = Path(cfg.state_dir) / "machine.json"
    if cache_path.exists():
        try:
            c = json.loads(cache_path.read_text())
            choice = c.get("choice", {})
            preset = choice.get("preset", "unknown")
            params = choice.get("params", 0)
            ms_per_step = choice.get("ms_per_step", 0)
            peak_gb = choice.get("peak_gb", 0)

            print(f"cache exists: {cache_path}")
            print(f"  preset: {preset}")
            print(f"  params: {params / 1e6:.1f}M")
            print(f"  ms_per_step: {ms_per_step}")
            print(f"  peak_gb: {peak_gb:.1f}")

            # Check memory and print verdict
            try:
                _check_start_memory(cfg, N_FEATURES, choice, say=print)
                print("verdict: would start")
            except NotEnoughMemory as e:
                print(f"verdict: {e}")
                sys.exit(2)
        except (OSError, ValueError, KeyError):
            print(f"cache exists: {cache_path} (read error)")
            print("next start: will run autotune")
            return
    else:
        print(f"cache: {cache_path} does not exist")
        print("next start: will run autotune ladder")
        print(f"free memory for autotune: {free_gb:.1f} GB" if free_gb is not None else "free memory for autotune: unknown")


def _venues() -> None:
    """Every market-data source and read-only account reader, with ids for STEEL_SOURCES_ON and
    the Control panel."""
    from .accounts import ACCOUNTS
    from .sources import REGISTRY, SimSource
    print(f"{'id':<14} {'venue':<10} {'kind':<10} {'P':>4}  default  name")
    for cls in REGISTRY + [SimSource]:
        print(f"{cls.id:<14} {cls.venue:<10} {cls.mechanism:<10} {cls.priority:>4}  {'on ' if cls.default_on else 'off'}      {cls.name}")
    print(f"\n{len(REGISTRY)} market-data venues (+ the simulator); STEEL_SOURCES_ON=+<id> turns one on at start, the panel any time.\n")
    print(f"{'id':<14} {'holds':<10} fields                       read-only key")
    for cls in ACCOUNTS:
        print(f"{cls.id:<14} {cls.venue:<10} {', '.join(f['id'] for f in cls.fields):<28} {cls.URL}")
    print(f"\n{len(ACCOUNTS)} account readers (balances only, never an order); keys go in <id>.json or the Venues tab, sealed by the vault.")
    from .execution import EXECUTORS
    print(f"\n{'id':<14} {'trades':<8} fields                       testnet")
    for cls in EXECUTORS:
        print(f"{cls.id:<14} {cls.venue:<8} {', '.join(f['id'] for f in cls.fields):<28} {cls.TESTNET or '-'}")
    print(f"\n{len(EXECUTORS)} order clients (off unless STEEL_EXECUTION=1, a venue armed with its own <id>-trade.json key, every cap set; dry-run by default).")


def _vault() -> None:
    """steel vault status | rotate | restore <key>: the key vault that seals every key file."""
    import sys
    from . import vault
    from .config import Config
    from .engine import KEY_SERVICES
    cfg = Config()
    files = [svc["file"] for svc in KEY_SERVICES]
    sub = sys.argv[2] if len(sys.argv) > 2 else "status"
    if sub == "restore":
        if len(sys.argv) < 4:
            print("usage: steel vault restore <recovery key>")
            raise SystemExit(2)
        p = Path(cfg.state_dir) / vault.KEY_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sys.argv[3].strip() + "\n")
        p.chmod(0o600)
        print(f"vault key restored to {p}")
        return
    v = vault.Vault(cfg.state_dir)
    if sub == "rotate":
        new = v.rotate(files)
        print("new recovery key (shown once; store it now):\n  " + new)
        v.acknowledge()
        return
    st = v.status(files)
    print(f"vault: {'ok' if st['available'] else 'unavailable'} ({st['key_file']}); recovery key {'pending: open the dashboard to save it' if st['backup_pending'] else 'shown'}")
    print(f"sealed: {', '.join(st['sealed']) or 'none'}")
    if st["plaintext"]:
        print(f"plaintext (sealed at the next start): {', '.join(st['plaintext'])}")
    if st["error"]:
        print("error:", st["error"])


def _token() -> None:
    """steel token [rotate]: the operator token the dashboard needs for any change."""
    import sys
    from . import auth
    from .config import Config
    cfg = Config()
    if len(sys.argv) > 2 and sys.argv[2] == "rotate":
        print("new operator token: " + auth.rotate(cfg.state_dir))
        return
    tok, fresh = auth.load(cfg.state_dir, cfg.token)
    print(("STEEL_TOKEN (from the environment): " if cfg.token else "operator token" + (" (new)" if fresh else "") + ": ") + tok)


def _backup() -> None:
    """steel backup [dir] [--replay]: one archive of the state that matters; steel restore <archive> [--state DIR]."""
    import sys
    from . import backup
    from .config import Config
    cfg = Config()
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if sys.argv[1] == "restore":
        if not args:
            print("usage: steel restore <archive.tar.gz>")
            raise SystemExit(2)
        state = cfg.state_dir
        if "--state" in sys.argv:
            state = sys.argv[sys.argv.index("--state") + 1]
        print("archive holds: " + ", ".join(backup.contents(args[0])))
        for p in backup.restore(args[0], state):
            print("restored", p)
        return
    out = args[0] if args else (cfg.backup_dir or "backups")
    p = backup.make(cfg.state_dir, out, replay="--replay" in sys.argv, keep=int(cfg.backup_keep or 0) if not args else 0)
    print(f"{p} ({p.stat().st_size / 1e6:.1f} MB): " + ", ".join(backup.contents(p)))


def _arb_replay() -> None:
    """steel arb-replay [--days N] [--latency-ms L] [--min-edge BPS] [--order-usd N]: the arbitrage rules over the dislocation log."""
    import sys
    from . import arbreplay
    from .config import Config
    cfg = Config()
    def opt(name, default):
        return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
    ladder = (opt("--latency-ms", 0.0),) if "--latency-ms" in sys.argv else arbreplay.LADDER
    s = arbreplay.summary(Path(cfg.state_dir) / "dislocations.jsonl", days=opt("--days", 7.0), ladder=ladder,
                          min_edge_bps=opt("--min-edge", float(cfg.arb_min_edge_bps)), max_order_usd=opt("--order-usd", float(cfg.arb_max_order_usd)),
                          cooldown_s=float(cfg.arb_cooldown_s), stable=cfg.transfer_asset)
    if "--json" in sys.argv:
        import json
        print(json.dumps(s, indent=1))
        return
    print(arbreplay.fmt(s))


def _probe() -> None:
    """steel probe [--json] [--only VENUE] [--no-stream]: every venue against the contract its code expects (exit 1 on a critical failure)."""
    import asyncio
    import json
    import sys
    from . import probe
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
    stream_s = 0.1 if "--no-stream" in sys.argv else float(sys.argv[sys.argv.index("--stream-s") + 1]) if "--stream-s" in sys.argv else 20.0
    rep = asyncio.run(probe.run(only=only, stream_s=stream_s))
    print(json.dumps(rep, indent=1) if "--json" in sys.argv else probe.fmt(rep))
    raise SystemExit(1 if rep["critical"] else 0)


def _handoff() -> None:
    """steel handoff [auto|shared|solo] [--wait]: the GPU mode of the running Steel (through its API, with the operator token).
    `shared --wait` returns once Steel reports the GPU released, for a peer's start script."""
    import json
    import sys
    import time
    import urllib.request
    from . import auth
    from .config import Config
    cfg = Config()
    mode = next((a for a in sys.argv[2:] if a in ("auto", "shared", "solo")), "")
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "") else cfg.host
    base = f"http://{host}:{cfg.port}/api/handoff"
    tok, _ = auth.load(cfg.state_dir, cfg.token, create=False)
    hdrs = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"} if tok else {"Content-Type": "application/json"}
    try:
        if mode:
            req = urllib.request.Request(base, data=json.dumps({"mode": mode}).encode(), headers=hdrs, method="POST")
            st = json.load(urllib.request.urlopen(req, timeout=10)).get("handoff") or {}
        else:
            st = json.load(urllib.request.urlopen(urllib.request.Request(base, headers=hdrs), timeout=10))
    except Exception as e:  # noqa: BLE001
        print(f"Steel is not answering on {base}: {type(e).__name__}: {e}")
        raise SystemExit(1)
    print(f"GPU {st.get('mode')}: {st.get('why')} | pinned {st.get('pinned')} | train_epochs {st.get('train_epochs')} | floor {st.get('memory_floor_gb')} GB"
          + (f" | peer: {st['peer']}" if st.get("peer") else "") + (f" | next start {st['next_start']}" if st.get("next_start") else ""))
    if mode == "shared" and "--wait" in sys.argv:
        for _ in range(30):
            if st.get("mode") == "shared":
                print("released")
                return
            time.sleep(2)
            st = json.load(urllib.request.urlopen(urllib.request.Request(base, headers=hdrs), timeout=10))
        print("still not shared after 60 s")
        raise SystemExit(1)


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "handoff":
        _handoff()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        _probe()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "arb-replay":
        _arb_replay()
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("backup", "restore"):
        _backup()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "token":
        _token()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "appendix":
        from . import docsgen
        raise SystemExit(docsgen.main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        from . import mcp
        mcp.main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "vault":
        _vault()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        _check()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "venues":
        _venues()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        _replay()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        _sweep()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        _backtest()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "version":
        _version()
        return
    ap = argparse.ArgumentParser(prog="steel", description="continuously learning crypto market model with a live dashboard")
    ap.add_argument("--feed", metavar="VENUE", help="force one market-data source: auto (default), sim, or a venue id from `steel venues`")
    ap.add_argument("--symbols", help="comma separated pairs, e.g. BTC-USD,ETH-USD (a bare BTC works too)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-news", action="store_true", help="disable the news skimmer")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    if args.feed:
        os.environ["STEEL_FEED"] = args.feed
    if args.symbols:
        os.environ["STEEL_SYMBOLS"] = args.symbols
    if args.host:
        os.environ["STEEL_HOST"] = args.host
    if args.port:
        os.environ["STEEL_PORT"] = str(args.port)
    if args.no_news:
        os.environ["STEEL_NEWS"] = "0"
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    import uvicorn

    from .autotune import NotEnoughMemory
    from .config import Config
    from .engine import Engine
    from .server import create_app

    cfg = Config()
    import socket
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _sock.settimeout(0.4)
    if _sock.connect_ex(("127.0.0.1" if cfg.host in ("0.0.0.0", "") else cfg.host, cfg.port)) == 0:
        _sock.close()
        print(f"Steel is already running on {cfg.host}:{cfg.port} -- refusing to start a second instance "
              f"(it would thrash the GPU during tuning). Stop it first:  pkill -9 -f 'python -m steel'")
        raise SystemExit(1)
    _sock.close()
    try:
        engine = Engine(cfg)
    except NotEnoughMemory as e:
        print(f"\n{e}")
        raise SystemExit(2)
    app = create_app(engine)
    print(f"Steel listening on http://{cfg.host}:{cfg.port}  (feed={cfg.feed}, {len(cfg.symbols)} pairs: {', '.join(s.split('-')[0] for s in cfg.symbols[:12])}{'…' if len(cfg.symbols) > 12 else ''})")
    # Graceful shutdown waits for in-flight requests and websocket handlers to return; cap it
    # so a stuck handler can never hold SIGTERM past systemd's stop timeout (the save in
    # the lifespan shutdown still runs after the cap).
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
