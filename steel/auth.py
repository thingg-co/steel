"""The operator token: what lets a browser change anything.

The dashboard is LAN-only and its reads stay open by default, but every mutating route (the
controls, the keys, arming, the kill switch, promotion, the arbitrage switch, the vault) needs
`Authorization: Bearer <token>` (or `?token=`), and `auth=all` closes the reads and the
websocket too. The token is `STEEL_TOKEN` when set; otherwise it is generated once at first
start, sealed in the vault (`state/token.json`) and printed by `steel token` on the box, which
is the one place it is ever shown. `steel token rotate` replaces it.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from . import vault as _vault

FILE = "token.json"


def load(state_dir: str | Path, env_token: str = "", *, create: bool = True) -> tuple[str, bool]:
    """(token, fresh): the environment's, else the sealed file's, else a new one (sealed)."""
    if env_token:
        return env_token.strip(), False
    v = _vault.get(state_dir)
    path = Path(state_dir) / FILE
    d = v.read_json(path)
    if d.get("token"):
        return str(d["token"]), False
    if not create:
        return "", False
    tok = secrets.token_urlsafe(24)
    v.write_json(path, {"token": tok})
    return tok, True


def rotate(state_dir: str | Path) -> str:
    tok = secrets.token_urlsafe(24)
    _vault.get(state_dir).write_json(Path(state_dir) / FILE, {"token": tok})
    return tok


def presented(headers, query: dict) -> str:
    """The token a request carries: the bearer header, else `?token=`."""
    h = str(headers.get("authorization") or headers.get("Authorization") or "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return str(query.get("token") or "")


def ok(token: str, given: str) -> bool:
    return bool(token) and bool(given) and secrets.compare_digest(token, given)
