"""The GPU handoff between Flint and Steel on one box.

Flint runs weekdays (its timer starts it before the US open; it stops itself after the close)
and holds about 50 GB of the GPU while it does. Steel never closes. One model, two modes:

- `shared`: Flint is running, or starts within `handoff_lead_minutes`. Steel keeps its memory
  floor, trains at its base budget, skips the weekly backtest, and in the lead-up releases
  what it can (background training paused, the CUDA cache emptied) so Flint's autotune finds
  the room it measured.
- `solo`: Flint is off (nights, weekends, holidays). The memory floor drops to
  `solo_memory_floor_gb`, the training budget is `solo_train_multiplier` times the base, the
  deep backfill trains at full tilt, the backtest may run.

The model is not swapped: a bigger preset would be a different net, trained from scratch
every night, and continuity is the whole point of online learning. The mode changes what the
same net does with the hours. Flint is detected by its user unit, its process or its port; the
schedule (`handoff_schedule`, "Mon..Fri 19:45" like the timer) only adds the lead time. The
operator can pin a mode (`steel handoff shared|solo|auto`, the Ops panel) and `deploy/spark/
steel-handoff` lets Flint's own start ask Steel to release first."""
from __future__ import annotations

import re
import socket
import subprocess
import time
from datetime import datetime, timedelta

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_schedule(text: str) -> tuple[set[int], int, int] | None:
    """"Mon..Fri 19:45" -> ({0..4}, 19, 45); "Sat,Sun 03:00"; "daily 02:00"; None when blank or unreadable."""
    t = str(text or "").strip()
    if not t:
        return None
    m = re.match(r"^(\S+)\s+(\d{1,2}):(\d{2})$", t)
    if not m:
        return None
    days_txt, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    days: set[int] = set()
    if days_txt.lower() in ("daily", "*"):
        days = set(range(7))
    else:
        for part in days_txt.split(","):
            if ".." in part:
                a, b = part.split("..", 1)
                if a not in DAYS or b not in DAYS:
                    return None
                i, j = DAYS.index(a), DAYS.index(b)
                days |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
            elif part in DAYS:
                days.add(DAYS.index(part))
            else:
                return None
    return days, hh, mm


def next_start(schedule: str, now: datetime | None = None) -> datetime | None:
    """The next scheduled peer start at or after `now` (local time, like a systemd timer)."""
    sch = parse_schedule(schedule)
    if not sch:
        return None
    days, hh, mm = sch
    now = now or datetime.now()
    for d in range(8):
        day = (now + timedelta(days=d)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day.weekday() in days and day >= now:
            return day
    return None


def peer_running(unit: str = "flint-run", process: str = "python -m flint", port: int = 8000) -> tuple[bool, str]:
    """(running, how): the peer's user unit, its process, or its port."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", f"{unit}.service"], capture_output=True, text=True, timeout=3)
        if r.stdout.strip() == "active":
            return True, f"{unit}.service is active"
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run(["pgrep", "-f", process], capture_output=True, text=True, timeout=3)
        pids = [p for p in r.stdout.split() if p.strip()]
        if pids:
            return True, f"process '{process}' ({pids[0]})"
    except Exception:  # noqa: BLE001
        pass
    if port:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True, f"port {port} is listening"
        finally:
            s.close()
    return False, ""


def decide(peer_up: bool, nxt: datetime | None, lead_minutes: float, pinned: str = "auto", now: datetime | None = None) -> tuple[str, str]:
    """(mode, why)."""
    if pinned in ("shared", "solo"):
        return pinned, f"pinned by the operator"
    if peer_up:
        return "shared", "the peer is running"
    now = now or datetime.now()
    if nxt is not None and 0 <= (nxt - now).total_seconds() <= lead_minutes * 60:
        return "shared", f"the peer starts at {nxt.strftime('%a %H:%M')} (releasing the GPU)"
    return "solo", (f"the peer is off until {nxt.strftime('%a %H:%M')}" if nxt else "no peer")


class Handoff:
    """Holds the mode and applies it to a config: the training budget and the memory floor."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base_epochs = float(getattr(cfg, "train_epochs", 0.0) or 0.0)
        self.base_floor = float(getattr(cfg, "memory_floor_gb", 0.0) or 0.0)
        self.mode = ""
        self.why = ""
        self.pinned = "auto"
        self.peer = ""
        self.changed_at = 0.0
        self.released = False
        self.next_start: datetime | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "handoff", False))

    def rebase(self) -> None:
        """A control change of train_epochs or memory_floor_gb while in a mode: take it as the new base."""
        mult = float(getattr(self.cfg, "solo_train_multiplier", 1.0) or 1.0)
        if self.mode == "solo":
            self.base_epochs = float(getattr(self.cfg, "train_epochs", 0.0) or 0.0) / max(mult, 1e-9)
        else:
            self.base_epochs = float(getattr(self.cfg, "train_epochs", 0.0) or 0.0)
            self.base_floor = float(getattr(self.cfg, "memory_floor_gb", 0.0) or 0.0)

    def evaluate(self, now: datetime | None = None) -> tuple[str, str, bool]:
        """Decide the mode; returns (mode, why, changed) and applies it to the config."""
        cfg = self.cfg
        up, how = peer_running(str(getattr(cfg, "handoff_peer", "flint-run")), str(getattr(cfg, "handoff_peer_process", "python -m flint")),
                               int(getattr(cfg, "handoff_peer_port", 8000) or 0))
        self.peer = how
        self.next_start = next_start(str(getattr(cfg, "handoff_schedule", "") or ""), now)
        mode, why = decide(up, self.next_start, float(getattr(cfg, "handoff_lead_minutes", 15.0) or 0.0), self.pinned, now)
        self.released = mode == "shared" and not up and self.pinned == "auto"     # the lead-up: give the GPU back before the peer measures it
        changed = mode != self.mode
        if changed:
            self.apply(mode)
            self.mode, self.changed_at = mode, time.time()
        self.why = why
        return mode, why, changed

    def apply(self, mode: str) -> None:
        cfg = self.cfg
        mult = float(getattr(cfg, "solo_train_multiplier", 1.0) or 1.0)
        if mode == "solo":
            cfg.train_epochs = self.base_epochs * mult
            cfg.memory_floor_gb = float(getattr(cfg, "solo_memory_floor_gb", self.base_floor))
        else:
            cfg.train_epochs = self.base_epochs
            cfg.memory_floor_gb = self.base_floor

    def status(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode or "off", "why": self.why, "pinned": self.pinned, "peer": self.peer,
                "released": self.released, "next_start": self.next_start.isoformat(timespec="minutes") if self.next_start else None,
                "train_epochs": float(getattr(self.cfg, "train_epochs", 0.0) or 0.0), "memory_floor_gb": float(getattr(self.cfg, "memory_floor_gb", 0.0) or 0.0),
                "changed_at": self.changed_at}
