"""`steel backup`: one archive of everything that would hurt to lose, restorable with
`steel restore`. The checkpoint (the model, and the replay with --replay), the vault key, every
sealed key file, the operator token, the live book, the order audit log, the arbitrage log, the
universe, the source toggles, the identity book and the machine cache. The engine runs one a
day into STEEL_BACKUP_DIR when it is set, keeping the newest `backup_keep`."""
from __future__ import annotations

import os
import tarfile
import time
from pathlib import Path

STATE_FILES = ["model.pt", "vault.key", "token.json", "live.json", "orders.jsonl", "arb.jsonl", "universe.json",
               "sources.json", "identity.json", "machine.json", "backtest.json", "KILL"]
REPLAY_FILES = ["replay.npz"]


def key_files() -> list[str]:
    from .engine import KEY_SERVICES
    return [svc["file"] for svc in KEY_SERVICES]


def make(state_dir: str | Path, out_dir: str | Path, *, replay: bool = False, keep: int = 0, now: float | None = None) -> Path:
    """Write <out_dir>/steel-<utc stamp>.tar.gz and return its path; prune to the newest `keep` when set."""
    state_dir, out_dir = Path(state_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    path = out_dir / f"steel-{stamp}.tar.gz"
    tmp = path.with_suffix(".tmp")
    names = STATE_FILES + (REPLAY_FILES if replay else [])
    with tarfile.open(tmp, "w:gz") as tar:
        for n in names:
            p = state_dir / n
            if p.is_file():
                tar.add(p, arcname=f"state/{n}")
        for n in key_files():
            p = Path(n)
            if p.is_file():
                tar.add(p, arcname=f"keys/{n}")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if keep > 0:
        olds = sorted(out_dir.glob("steel-*.tar.gz"))
        for old in olds[:-keep]:
            old.unlink(missing_ok=True)
    return path


def contents(archive: str | Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return [m.name for m in tar.getmembers() if m.isfile()]


def restore(archive: str | Path, state_dir: str | Path, keys_dir: str | Path = ".") -> list[str]:
    """Unpack into state_dir (state/*) and keys_dir (keys/*), refusing any path outside them.
    Returns the files written. Existing files are replaced."""
    state_dir, keys_dir = Path(state_dir), Path(keys_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            parts = Path(m.name).parts
            if len(parts) != 2 or parts[0] not in ("state", "keys") or parts[1] in ("..", "."):
                raise ValueError(f"refusing archive member {m.name!r}")
            dest = (state_dir if parts[0] == "state" else keys_dir) / parts[1]
            f = tar.extractfile(m)
            if f is None:
                continue
            tmp = dest.with_name(dest.name + ".restore")
            tmp.write_bytes(f.read())
            os.replace(tmp, dest)
            if parts[1] in ("vault.key", "token.json") or parts[0] == "keys":
                try:
                    dest.chmod(0o600)
                except OSError:
                    pass
            written.append(str(dest))
    return written
