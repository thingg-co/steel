"""Steel's key vault: every key file is encrypted at rest under a master key Steel manages.

The master key is generated on first start and kept in `<state_dir>/vault.key` (owner-only).
Every `<service>.json` and `<venue>-trade.json` is sealed with it (Fernet: AES-128-CBC with
an HMAC, a fresh token per write); a plaintext key file found at start is sealed in place.
The recovery copy of the master key is handed out exactly once: the dashboard shows it until
the operator acknowledges saving it, and after that neither the API nor the CLI will print it
again (`steel vault rotate` makes a new one, re-seals every file and shows the new one once).
`STEEL_VAULT_KEY` supplies the key on a box without the file, which is how a restore works.
Keys are decrypted only in memory, when a reader or a client needs them.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "steel-vault:v1:"
KEY_FILE = "vault.key"
SHOWN_FILE = "vault.shown"
_default: "Vault | None" = None


def is_sealed(text: str) -> bool:
    return str(text or "").startswith(PREFIX)


class Vault:
    def __init__(self, state_dir: str | Path, key: str | None = None, *, create: bool = True):
        self.state_dir = Path(state_dir)
        self.key_path = self.state_dir / KEY_FILE
        self.shown_path = self.state_dir / SHOWN_FILE
        self.fresh = False                                   # created on this open: the recovery copy has not been shown
        self.error = ""
        key = (key or os.environ.get("STEEL_VAULT_KEY") or "").strip()
        if not key:
            try:
                key = self.key_path.read_text().strip()
            except OSError:
                key = ""
        if not key and create:
            key = Fernet.generate_key().decode()
            self.fresh = True
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                self.key_path.write_text(key + "\n")
                self.key_path.chmod(0o600)
                with open(self.shown_path, "w") as f:
                    f.write("pending\n")
                self.shown_path.chmod(0o600)
            except OSError as e:
                self.error = f"could not write {self.key_path}: {type(e).__name__}"
        self._key = key
        self._f = Fernet(key.encode()) if key else None

    @property
    def available(self) -> bool:
        return self._f is not None

    @property
    def backup_pending(self) -> bool:
        """Whether the recovery copy still has to be shown: the key was created here and the
        operator has not yet acknowledged saving it."""
        try:
            return self.shown_path.read_text().strip() == "pending"
        except OSError:
            return False

    def backup(self) -> str | None:
        """The recovery copy, only while it is pending; None once it has been acknowledged."""
        return self._key if self.available and self.backup_pending else None

    def acknowledge(self) -> None:
        try:
            with open(self.shown_path, "w") as f:
                f.write(f"shown {time.time():.0f}\n")
            self.shown_path.chmod(0o600)
        except OSError:
            pass

    def encrypt(self, text: str) -> str:
        if not self.available:
            return text
        return PREFIX + self._f.encrypt(text.encode()).decode()

    def decrypt(self, text: str) -> str:
        text = str(text or "")
        if not is_sealed(text):
            return text
        if not self.available:
            raise InvalidToken("no vault key")
        return self._f.decrypt(text[len(PREFIX):].encode()).decode()

    def read_json(self, path: str | Path) -> dict:
        """A key file's content, sealed or not; {} when missing or unreadable with this key."""
        try:
            raw = Path(path).read_text()
        except OSError:
            return {}
        try:
            return json.loads(self.decrypt(raw))
        except (InvalidToken, ValueError):
            return {}

    def write_json(self, path: str | Path, data: dict) -> None:
        p = Path(path)
        p.write_text(self.encrypt(json.dumps(data)) + ("\n" if self.available else ""))
        try:
            p.chmod(0o600)
        except OSError:
            pass

    def seal_file(self, path: str | Path) -> bool:
        """Seal a plaintext key file in place. True when it was sealed by this call."""
        p = Path(path)
        try:
            raw = p.read_text()
        except OSError:
            return False
        if is_sealed(raw) or not self.available:
            try:
                p.chmod(0o600)
            except OSError:
                pass
            return False
        try:
            data = json.loads(raw)
        except ValueError:
            return False
        self.write_json(p, data)
        return True

    def seal_all(self, files: list[str | Path]) -> list[str]:
        return [str(f) for f in files if self.seal_file(f)]

    def rotate(self, files: list[str | Path]) -> str:
        """A new master key: every sealed file is re-sealed under it, the recovery copy is
        pending again. Returns the new key (the one time it is shown)."""
        new = Fernet.generate_key().decode()
        nf = Fernet(new.encode())
        for f in files:
            p = Path(f)
            try:
                raw = p.read_text()
            except OSError:
                continue
            try:
                plain = self.decrypt(raw)
            except InvalidToken:
                continue
            p.write_text(PREFIX + nf.encrypt(plain.encode()).decode() + "\n")
            try:
                p.chmod(0o600)
            except OSError:
                pass
        self._key, self._f = new, nf
        self.key_path.write_text(new + "\n")
        self.key_path.chmod(0o600)
        with open(self.shown_path, "w") as fh:
            fh.write("pending\n")
        return new

    def status(self, files: list[str | Path] | None = None) -> dict:
        sealed, plain, missing = [], [], []
        for f in files or []:
            p = Path(f)
            if not p.exists():
                missing.append(p.name)
            else:
                try:
                    (sealed if is_sealed(p.read_text()) else plain).append(p.name)
                except OSError:
                    missing.append(p.name)
        return {"available": self.available, "key_file": str(self.key_path), "backup_pending": self.backup_pending,
                "sealed": sealed, "plaintext": plain, "missing": missing, "error": self.error}


def get(state_dir: str | Path | None = None, *, create: bool = True) -> Vault:
    """The process's vault, opened once (STEEL_VAULT_KEY, else <state_dir>/vault.key, else a new key)."""
    global _default
    if _default is None:
        _default = Vault(state_dir or os.environ.get("STEEL_STATE_DIR", "state"), create=create)
    return _default


def reset() -> None:
    global _default
    _default = None
