"""The key vault: sealing at rest, transparent reads, the recovery key shown once, rotation,
restore, and the files locked to the owner."""
import json
import os
import stat

import pytest

from steel import vault as V
from steel.config import read_creds


def test_seal_read_and_lockdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    v = V.Vault(tmp_path / "state")
    assert v.available and v.fresh and v.backup_pending and v.backup()
    assert stat.S_IMODE(os.stat(v.key_path).st_mode) == 0o600
    (tmp_path / "kraken.json").write_text(json.dumps({"key": "k1", "secret": "s1"}))
    assert v.seal_all(["kraken.json", "missing.json"]) == ["kraken.json"]
    raw = (tmp_path / "kraken.json").read_text()
    assert raw.startswith("steel-vault:v1:") and "k1" not in raw and stat.S_IMODE(os.stat(tmp_path / "kraken.json").st_mode) == 0o600
    assert v.read_json("kraken.json") == {"key": "k1", "secret": "s1"}
    V.reset(); monkeypatch.setenv("STEEL_STATE_DIR", str(tmp_path / "state"))
    assert read_creds("kraken", ["key", "secret"]) == {"key": "k1", "secret": "s1"}     # the config reader decrypts through the process vault
    assert v.seal_file("kraken.json") is False                                           # already sealed: untouched
    other = V.Vault(tmp_path / "elsewhere")                                              # a different key cannot read it
    assert other.read_json("kraken.json") == {}
    st = v.status(["kraken.json", "coinbase.json"])
    assert st["sealed"] == ["kraken.json"] and st["missing"] == ["coinbase.json"] and st["backup_pending"]
    V.reset()


def test_backup_is_shown_once_then_rotation_shows_a_new_one(tmp_path):
    v = V.Vault(tmp_path / "state")
    first = v.backup()
    assert first and v.backup() == first                                                 # until acknowledged it stays available
    v.acknowledge()
    assert v.backup() is None and not v.backup_pending
    again = V.Vault(tmp_path / "state")                                                  # reopened from the file: not fresh, nothing to show
    assert not again.fresh and again.backup() is None and again.available
    (tmp_path / "okx.json").write_text(json.dumps({"key": "a", "secret": "b", "passphrase": "c"}))
    again.seal_file(tmp_path / "okx.json")
    new = again.rotate([tmp_path / "okx.json"])
    assert new != first and again.backup() == new and again.read_json(tmp_path / "okx.json") == {"key": "a", "secret": "b", "passphrase": "c"}
    assert V.Vault(tmp_path / "state").read_json(tmp_path / "okx.json")["key"] == "a"    # the file holds the new key


def test_env_key_restores_on_a_new_box(tmp_path, monkeypatch):
    a = V.Vault(tmp_path / "a")
    (tmp_path / "bybit.json").write_text(json.dumps({"key": "x", "secret": "y"}))
    a.seal_file(tmp_path / "bybit.json")
    monkeypatch.setenv("STEEL_VAULT_KEY", a.backup())
    b = V.Vault(tmp_path / "b", create=False)
    assert b.available and not b.fresh and b.read_json(tmp_path / "bybit.json") == {"key": "x", "secret": "y"}
    monkeypatch.delenv("STEEL_VAULT_KEY")
    c = V.Vault(tmp_path / "c", create=False)
    assert not c.available and c.read_json(tmp_path / "bybit.json") == {}
    assert c.encrypt("plain") == "plain" and c.decrypt("plain") == "plain"
    with pytest.raises(Exception):
        c.decrypt("steel-vault:v1:nope")
