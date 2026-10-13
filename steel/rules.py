"""Rules: the operator's own alerts on any series Steel stores (M19).

A rule names a pair (or `*` for every pair), a series the screener row carries (price, the
hour's change, the tape's delta, open interest and its change, funding, liquidations by side,
the book's imbalance, dislocations, the model's median call and P(up)), a comparison and a
value. The engine evaluates every enabled rule against the screener's rows every ten seconds;
a hit goes to the alert destinations (Telegram, Discord, a webhook) as kind `rule`, to the
console, and over the websocket. Each rule keeps its own cooldown per pair so a series sitting
on the line does not fire every ten seconds. Crosses compare with the pair's previous value.
`state/rules.json` persists the rules and their hit counts. The rules never place orders.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from pathlib import Path

from .config import canonical

SERIES = {
    "price": ("price", "$"), "chg_1h_bps": ("1-hour change", "bps"), "delta_1h": ("tape delta, 1 h", "$"), "prints_1h": ("prints, 1 h", ""),
    "vol_24h": ("dollar volume, 24 h", "$"), "oi_usd": ("open interest", "$"), "oi_chg_1h": ("OI change, 1 h", "frac"), "funding": ("funding rate", "frac"),
    "liq_long_1h": ("longs liquidated, 1 h", "$"), "liq_short_1h": ("shorts liquidated, 1 h", "$"), "liq_1h": ("liquidations, 1 h", "$"),
    "book_imb_05": ("book imbalance within 0.5%", "frac"), "dislocations": ("dislocation episodes", ""), "q50": ("the model's median call", "bps"),
    "p_up": ("the model's P(up)", "frac"), "size": ("the policy's size", "frac"),
}
OPS = {">": "above", "<": "below", ">=": "at or above", "<=": "at or below", "abs>": "beyond ±", "crosses_above": "crosses above", "crosses_below": "crosses below"}
MIN_COOLDOWN = 60.0


def value_of(row: dict, series: str) -> float | None:
    if series == "liq_1h":
        return float(row.get("liq_long_1h") or 0.0) + float(row.get("liq_short_1h") or 0.0)
    v = row.get(series)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _holds(op: str, v: float, prev: float | None, x: float) -> bool:
    if op == ">":
        return v > x
    if op == "<":
        return v < x
    if op == ">=":
        return v >= x
    if op == "<=":
        return v <= x
    if op == "abs>":
        return abs(v) > abs(x)
    if op == "crosses_above":
        return prev is not None and prev <= x < v
    if op == "crosses_below":
        return prev is not None and prev >= x > v
    return False


def describe(rule: dict) -> str:
    name, unit = SERIES.get(rule["series"], (rule["series"], ""))
    pair = "any pair" if rule["pair"] == "*" else rule["pair"]
    return f"{pair}: {name} {OPS.get(rule['op'], rule['op'])} {rule['value']:g}{(' ' + unit) if unit and unit != '$' else ''}"


class RuleBook:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.rules: list[dict] = []
        self.prev: dict[tuple[str, str], float] = {}        # (rule id, pair) -> last value seen
        self.last: dict[tuple[str, str], float] = {}        # (rule id, pair) -> last hit time
        self.fired: deque[dict] = deque(maxlen=200)
        self.load()

    # Persistence ----------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
            self.rules = [r for r in d.get("rules", []) if isinstance(r, dict) and r.get("series") in SERIES and r.get("op") in OPS]
            self.fired.extend(d.get("fired", [])[-200:])
        except (OSError, ValueError):
            self.rules = []

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rules": self.rules, "fired": list(self.fired)[-50:]}, indent=1))
            os.replace(tmp, self.path)
        except OSError:
            pass

    # Editing --------------------------------------------------------------------------
    def add(self, spec: dict) -> dict:
        series = str(spec.get("series") or "")
        if series not in SERIES:
            raise ValueError(f"unknown series {series!r}")
        op = str(spec.get("op") or ">")
        if op not in OPS:
            raise ValueError(f"unknown comparison {op!r}")
        try:
            value = float(spec.get("value"))
        except (TypeError, ValueError):
            raise ValueError("value must be a number") from None
        pair = str(spec.get("pair") or "*").strip()
        pair = "*" if pair in ("*", "", "any") else canonical(pair)
        rule = {"id": uuid.uuid4().hex[:8], "pair": pair, "series": series, "op": op, "value": value,
                "cooldown_s": max(MIN_COOLDOWN, float(spec.get("cooldown_s") or 900.0)), "note": str(spec.get("note") or "")[:120],
                "enabled": True, "created": time.time(), "hits": 0, "last_hit": None, "last_value": None}
        self.rules.append(rule)
        self.save()
        return rule

    def remove(self, rule_id: str) -> bool:
        n = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        if len(self.rules) != n:
            self.save()
        return len(self.rules) != n

    def toggle(self, rule_id: str, enabled: bool | None = None) -> dict | None:
        for r in self.rules:
            if r["id"] == rule_id:
                r["enabled"] = (not r["enabled"]) if enabled is None else bool(enabled)
                self.save()
                return r
        return None

    def get(self, rule_id: str) -> dict | None:
        return next((r for r in self.rules if r["id"] == rule_id), None)

    # Evaluation -----------------------------------------------------------------------
    def evaluate(self, rows: list[dict], now: float | None = None) -> list[dict]:
        """Every (rule, pair) that holds now and is past its cooldown. Updates the previous
        values for the crossing rules whether or not anything fired."""
        now = time.time() if now is None else now
        out = []
        if not self.rules:
            return out
        by_pair = {r.get("pair"): r for r in rows}
        for rule in self.rules:
            pairs = list(by_pair) if rule["pair"] == "*" else [rule["pair"]]
            for pair in pairs:
                row = by_pair.get(pair)
                if not row:
                    continue
                v = value_of(row, rule["series"])
                if v is None:
                    continue
                k = (rule["id"], pair)
                prev = self.prev.get(k)
                self.prev[k] = v
                if not rule.get("enabled", True) or not _holds(rule["op"], v, prev, float(rule["value"])):
                    continue
                if now - self.last.get(k, -1e12) < float(rule.get("cooldown_s") or MIN_COOLDOWN):
                    continue
                self.last[k] = now
                rule["hits"] = int(rule.get("hits") or 0) + 1
                rule["last_hit"] = now
                rule["last_value"] = v
                name, unit = SERIES[rule["series"]]
                text = f"{pair}: {name} is {v:,.4g}{(' ' + unit) if unit and unit != '$' else ''} ({OPS[rule['op']]} {rule['value']:g})" + (f" — {rule['note']}" if rule.get("note") else "")
                ev = {"id": rule["id"], "pair": pair, "series": rule["series"], "value": v, "t": now, "text": text}
                self.fired.append(ev)
                out.append(ev)
        if out:
            self.save()
        return out

    def status(self) -> dict:
        return {"rules": [dict(r, text=describe(r)) for r in self.rules], "n": len(self.rules), "enabled": sum(1 for r in self.rules if r.get("enabled", True)),
                "fired": list(self.fired)[-30:], "series": {k: {"name": n, "unit": u} for k, (n, u) in SERIES.items()}, "ops": OPS}
