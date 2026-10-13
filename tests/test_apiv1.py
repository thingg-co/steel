"""The data API v1 (steel/apiv1.py): the token gate, the weight budget, the pair check, the
index, the stream's fan-out and the engine's push helper, on a stub engine."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from steel import apiv1


def make_engine(auth="off", token="secret"):
    eng = SimpleNamespace(cfg=SimpleNamespace(auth=auth, api_weight_per_minute=20, bar_seconds=300.0, horizon=12), token=token, symbols=["BTC-USD", "ETH-USD"],
                          screener=lambda: {"rows": [{"pair": "BTC-USD"}], "n": 1}, markets_v1=lambda: {"markets": [{"pair": "BTC-USD"}], "n": 1},
                          candles=lambda pair, tf, n, venues, to: {"pair": pair, "tf": tf, "n": n, "venues": sorted(venues) if venues else None, "to": to},
                          tape_view=lambda pair, n, min_usd, bars: {"pair": pair, "n": n}, forecast_v1=lambda pair: {"pair": pair, "action": "HOLD"},
                          calendar_view=lambda: {"upcoming": []}, predictions_view=lambda: {"markets": []}, options_view=lambda a: {"asset": a},
                          tape=SimpleNamespace(cvd=lambda pair, bar_s, bars, min_usd=0.0, max_usd=None: {"pair": pair, "bars": bars, "min_usd": min_usd, "max_usd": max_usd}))
    return eng


def make_app(eng):
    app = FastAPI()
    app.include_router(apiv1.make_router(eng))
    return app


def test_index_and_markets_open():
    c = TestClient(make_app(make_engine()))
    r = c.get("/api/v1")
    assert r.status_code == 200 and r.json()["version"] == apiv1.VERSION and any("/markets" in x["route"] for x in r.json()["routes"])
    r = c.get("/api/v1/markets")
    assert r.status_code == 200 and r.json()["n"] == 1 and r.headers["X-Weight-Used"] == "1" and r.headers["X-API-Version"] == apiv1.VERSION


def test_token_required_when_auth_on():
    c = TestClient(make_app(make_engine(auth="mutate")))
    assert c.get("/api/v1/markets").status_code == 401                     # a GET, but v1 always wants the token
    assert c.get("/api/v1/markets", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert c.get("/api/v1/markets?token=secret").status_code == 200
    assert c.get("/api/v1/markets?token=wrong").status_code == 401


def test_pair_check_and_params():
    c = TestClient(make_app(make_engine()))
    assert c.get("/api/v1/candles/DOGE").status_code == 404
    r = c.get("/api/v1/candles/btc?tf=60&n=99999&venues=binance,coinbase")
    assert r.status_code == 200 and r.json() == {"pair": "BTC-USD", "tf": 60.0, "n": 5000, "venues": ["binance", "coinbase"], "to": None}
    r = c.get("/api/v1/delta/ETH-USD?bars=10&band=1")
    assert r.json() == {"pair": "ETH-USD", "bars": 10, "min_usd": 1000.0, "max_usd": 5000.0}
    r = c.get("/api/v1/delta/ETH-USD?band=25000,")
    assert r.json()["min_usd"] == 25000.0 and r.json()["max_usd"] is None
    assert c.get("/api/v1/delta/ETH-USD?band=x").status_code == 400
    assert c.get("/api/v1/forecast/eth").json()["action"] == "HOLD"


def test_weight_budget_429_and_usage():
    c = TestClient(make_app(make_engine()))                                  # a budget of 20 a minute
    for _ in range(4):
        assert c.get("/api/v1/screener").status_code == 200                 # 5 each
    r = c.get("/api/v1/screener")
    assert r.status_code == 429 and "Retry-After" in r.headers and r.json()["used"] == 20
    u = c.get("/api/v1/usage").json()
    assert u["used"] == 20 and u["left"] == 0 and u["limit"] == 20
    assert c.get("/api/v1").status_code == 200                              # weight 0 still answers


def test_usage_window_slides():
    u = apiv1.Usage(10)
    assert u.charge("k", 6, now=0.0) == (True, 6)
    assert u.charge("k", 6, now=1.0) == (False, 6)
    assert u.charge("k", 6, now=61.0) == (True, 6)
    assert u.retry_after("k", now=62.0) >= 1


def test_stream_fanout_and_push():
    eng = make_engine()
    c = TestClient(make_app(eng))
    with c.websocket_connect("/api/v1/stream?pairs=btc,eth&channels=trades,liquidations,nope") as ws:
        hello = ws.receive_json()
        assert hello["ch"] == "hello" and hello["pairs"] == ["BTC-USD", "ETH-USD"] and hello["channels"] == ["liquidations", "trades"]
        assert len(eng.streams) == 1
        assert apiv1.push(eng, "trades", "BTC-USD", {"px": 1.0}) == 1
        assert apiv1.push(eng, "trades", "SOL-USD", {"px": 2.0}) == 0          # not subscribed to that pair
        assert apiv1.push(eng, "book", "BTC-USD", {"tops": {}}) == 0           # not that channel
        assert apiv1.push(eng, "bars", None, {"index": 1}) == 0
        got = ws.receive_json()
        assert got == {"ch": "trades", "pair": "BTC-USD", "px": 1.0}
    assert eng.streams == []                                                  # unsubscribed on disconnect
    assert apiv1.push(eng, "trades", "BTC-USD", {"px": 1.0}) == 0


def test_stream_needs_token_when_auth_on():
    c = TestClient(make_app(make_engine(auth="all")))
    try:
        with c.websocket_connect("/api/v1/stream") as ws:
            ws.receive_text()
        raise AssertionError("accepted without a token")
    except Exception as e:  # noqa: BLE001  (a close before accept surfaces as a disconnect)
        assert "AssertionError" not in type(e).__name__
    with c.websocket_connect("/api/v1/stream?token=secret") as ws:
        assert ws.receive_json()["ch"] == "hello"


def test_push_drops_when_full():
    eng = SimpleNamespace(streams=[{"q": asyncio.Queue(maxsize=1), "pairs": None, "channels": {"trades"}, "dropped": 0}])
    assert apiv1.push(eng, "trades", "X-USD", {"a": 1}) == 1
    assert apiv1.push(eng, "trades", "X-USD", {"a": 2}) == 0
    assert eng.streams[0]["dropped"] == 1
    assert json.loads(eng.streams[0]["q"].get_nowait())["a"] == 1
