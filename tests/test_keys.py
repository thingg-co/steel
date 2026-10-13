"""API keys: every KEY_SERVICES entry round-trips through set_key, the panel status, a fresh
Config() (what a restart reads) and the live apply."""
import json
import types
from pathlib import Path

import pytest

from steel.accounts import ACCOUNTS
from steel.config import Config, read_creds
from steel.alerts import Alerts
from steel.engine import KEY_SERVICES, Engine
from steel.execution import EXECUTORS, ExecutionGate

PEM = "-----BEGIN EC PRIVATE KEY-----\\nabc\\n-----END EC PRIVATE KEY-----\\n"


def sample(svc: dict) -> dict:
    """A value per field: the Coinbase key json as the portal downloads it, a sample for every
    other field (an address for wallet fields, a token otherwise)."""
    out = {}
    for n, f in enumerate(svc["fields"]):
        fid = f["id"]
        if fid == "privateKey":
            out[fid] = PEM
        elif "address" in fid.lower() or fid in ("user", "signer", "vault"):
            out[fid] = "dydx1" + "q" * 38 if svc["id"].startswith("dydx") else "0x" + "ab12" * 10
        elif fid.endswith("_key") or fid == "mnemonic":
            out[fid] = "0x" + f"{1234 + n:064x}"                                                   # a wallet-style hex private key
        else:
            out[fid] = f"{svc['id']}-{fid}-{1234 + n}"
    return out


def config_read(svc: dict) -> dict:
    """What a restart reads: the generic reader for an account service, the field for CoinGecko."""
    if svc["id"] == "coingecko":
        return {"key": Config().coingecko_key}
    return read_creds(svc["id"], [f["id"] for f in svc["fields"]])


def fake_engine(tmp_path):
    cfg = Config()
    gate = ExecutionGate(cfg, tmp_path)
    e = types.SimpleNamespace(cfg=cfg, accounts={cls.id: cls() for cls in ACCOUNTS}, executors={cls.id: cls(gate) for cls in EXECUTORS},
                              signals=types.SimpleNamespace(market=types.SimpleNamespace(key="")),
                              sources=types.SimpleNamespace(sources={}),
                              trace=types.SimpleNamespace(emit=lambda *a, **k: None), _publish=lambda m: None,
                              alerts=Alerts({}))
    e._key_values = lambda: Engine._key_values(e)
    e.keys_status = lambda: Engine.keys_status(e)
    e._apply_key = lambda s, v: Engine._apply_key(e, s, v)
    e.set_key = lambda s, v: Engine.set_key(e, s, v)
    return e


def test_every_service_is_covered():
    assert {s["id"] for s in KEY_SERVICES} == {c.id for c in ACCOUNTS} | {"coingecko", "alerts"} | {f"{c.id}-trade" for c in EXECUTORS}
    assert len({s["id"] for s in KEY_SERVICES}) == len(KEY_SERVICES)
    for svc in KEY_SERVICES:
        assert (svc["url"].startswith("https://") or svc["id"].endswith("-trade")) and svc["note"] and svc["fields"] and svc["file"] == f"{svc['id']}.json"
        if svc["id"].endswith("-trade"):
            assert "trade" in svc["note"].lower() and svc["file"].endswith("-trade.json")   # a trade key is its own file, never the read-only one
        else:
            assert "read-only" in svc["note"].lower() or svc["id"] in ("coingecko", "alerts"), f"{svc['id']}: the note must say the key is read-only"
        assert len({f["id"] for f in svc["fields"]}) == len(svc["fields"])


def test_every_key_file_is_gitignored():
    import fnmatch
    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().split()
    for svc in KEY_SERVICES:
        assert any(fnmatch.fnmatch(svc["file"], pat) for pat in ignored), f"{svc['file']} must be in .gitignore"


@pytest.mark.parametrize("svc", KEY_SERVICES, ids=lambda s: s["id"])
def test_save_status_restart_and_live_apply(svc, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in list(__import__("os").environ):
        if k.startswith("STEEL_"):
            monkeypatch.delenv(k, raising=False)
    e = fake_engine(tmp_path)
    before = next(x for x in e.keys_status() if x["id"] == svc["id"])
    assert before["present"] is False
    vals = sample(svc)
    res = e.set_key(svc["id"], vals)
    assert res.get("ok"), res
    assert (tmp_path / svc["file"]).exists()
    after = next(x for x in res["keys"] if x["id"] == svc["id"])
    assert after["present"] is True
    for f in after["fields"]:
        assert f["present"] and "***" in f["masked"] and vals[f["id"]] not in f["masked"]   # never shown in full
    fresh = config_read(svc)                                                            # what a restart reads
    expect = {k: v.replace("\\n", "\n") for k, v in vals.items()}
    assert fresh == expect, f"{svc['id']} does not survive a restart"
    if svc["id"] == "coingecko":
        assert e.signals.market.key == vals["key"] and e.cfg.coingecko_key == vals["key"]
    elif svc["id"].endswith("-trade"):
        ex = e.executors[svc["id"].removesuffix("-trade")]
        assert ex.configured and all(getattr(ex, f["id"]) == expect[f["id"]] for f in ex.fields)
    elif svc["id"] == "alerts":
        assert e.alerts.configured and e.alerts.values() == expect and len(e.alerts.queue) == 1   # the hello
    else:
        acct = e.accounts[svc["id"]]
        assert acct.configured, f"{svc['id']} reader not configured after the live apply"
        assert acct.values() == expect
        assert e.accounts[svc["id"]].__class__(**fresh).configured                       # and a restart's reader would be too


def test_unknown_service_and_empty_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    e = fake_engine(tmp_path)
    assert e.set_key("schwab", {"key": "x"}) == {"error": "unknown service"}
    assert e.set_key("kraken", {"key": "", "secret": ""}) == {"error": "no value provided"}
