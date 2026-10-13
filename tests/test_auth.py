"""The operator token: generated once and sealed, from the environment when set, required on
every mutating route (and on reads with auth=all), never on the dashboard itself."""
import types

import pytest

from steel import auth
from steel.config import Config
from steel.server import create_app


def test_token_is_generated_sealed_and_rotated(tmp_path, monkeypatch):
    monkeypatch.setenv("STEEL_STATE_DIR", str(tmp_path))
    from steel import vault as V
    V.reset()
    tok, fresh = auth.load(tmp_path)
    assert fresh and len(tok) >= 30 and (tmp_path / "token.json").read_text().startswith("steel-vault:")
    again, fresh2 = auth.load(tmp_path)
    assert again == tok and not fresh2
    assert auth.load(tmp_path, "env-token")[0] == "env-token"
    new = auth.rotate(tmp_path)
    assert new != tok and auth.load(tmp_path)[0] == new
    assert auth.ok(tok, tok) and not auth.ok(tok, tok + "x") and not auth.ok("", "")
    assert auth.presented({"authorization": "Bearer abc"}, {}) == "abc" and auth.presented({}, {"token": "q"}) == "q"
    V.reset()


def stub(mode="mutate"):
    from tests.test_server import make_engine
    e = make_engine()
    e.cfg = Config(); e.cfg.auth = mode
    e.token = "secret-token"
    e.execution_status = lambda: {"enabled": False}
    e.execution_action = lambda p: {"ok": True}
    return e


@pytest.mark.parametrize("mode", ["mutate", "all", "off"])
def test_routes_need_the_token(mode):
    from fastapi.testclient import TestClient
    with TestClient(create_app(stub(mode))) as c:
        assert c.get("/").status_code == 200                                                   # the dashboard always loads
        r = c.get("/api/state")
        assert r.status_code == (401 if mode == "all" else 200)
        assert c.get("/api/state", headers={"Authorization": "Bearer secret-token"}).status_code == 200
        assert c.get("/api/state", params={"token": "secret-token"}).status_code == 200
        p = c.post("/api/execution", json={"action": "kill"})
        assert p.status_code == (200 if mode == "off" else 401)
        if mode != "off":
            assert p.json()["error"] == "operator token required"
        assert c.post("/api/execution", json={"action": "kill"}, headers={"Authorization": "Bearer secret-token"}).status_code == 200
        assert c.post("/api/execution", json={"action": "kill"}, headers={"Authorization": "Bearer wrong"}).status_code == (200 if mode == "off" else 401)
