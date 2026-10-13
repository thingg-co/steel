"""The MCP server (steel/mcp.py): the handshake, the tool list, calls routed to the API, the
rule tools off by default, and the stdio loop; the API is a fake client."""
import io
import json

from steel import mcp


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "/api/state":
            return {"status": {"phase": "live", "feed": "coinbase"}, "metrics": {"trusted": True, "hit_rate": 0.56}, "paper": {"equity": 101000.0}, "config": {"symbols": ["BTC-USD"]}}
        if path == "/api/v1/screener":
            return {"rows": [{"pair": "A-USD", "delta_1h": 1.0, "oi_chg_1h": None}, {"pair": "B-USD", "delta_1h": 5.0, "oi_chg_1h": 0.1}]}
        return {"path": path, **params}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"ok": True, **body}


def test_handshake_and_tool_list():
    c = FakeClient()
    res = mcp.handle(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert res["result"]["serverInfo"]["name"] == "steel" and "tools" in res["result"]["capabilities"]
    assert mcp.handle(c, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp.handle(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "steel_candles" in names and "steel_rule_add" not in names
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    with_rules = mcp.handle(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, rules=True)["result"]["tools"]
    assert "steel_rule_add" in {t["name"] for t in with_rules}
    assert mcp.handle(c, {"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601
    assert not any("order" in t["name"] or "trade" in t["name"] for t in with_rules)


def test_calls_route_to_the_api():
    c = FakeClient()
    out = mcp.call_tool(c, "steel_candles", {"pair": "btc", "tf": 60, "n": 5})
    assert out["path"] == "/api/v1/candles/BTC-USD" and out["tf"] == 60 and out["n"] == 5
    st = mcp.call_tool(c, "steel_state", {})
    assert st["phase"] == "live" and st["trusted"] is True and st["paper"]["equity"] == 101000.0
    sc = mcp.call_tool(c, "steel_screener", {"sort": "oi_chg_1h", "n": 1})
    assert sc["rows"][0]["pair"] == "B-USD" and sc["n"] == 2                 # None sorts last
    assert mcp.call_tool(c, "steel_options", {"asset": "eth"})["path"] == "/api/v1/options/ETH"
    assert mcp.call_tool(c, "steel_ticket", {"pair": "ETH/USDT"})["path"] == "/api/ticket/ETH-USD"
    assert "error" in mcp.call_tool(c, "steel_fly", {})


def test_rule_tools_gated():
    c = FakeClient()
    assert "off" in mcp.call_tool(c, "steel_rule_add", {"pair": "*", "series": "price", "op": ">", "value": 1})["error"]
    assert not any(k[0] == "POST" for k in c.calls)
    out = mcp.call_tool(c, "steel_rule_add", {"pair": "*", "series": "price", "op": ">", "value": 1}, rules=True)
    assert out["action"] == "add" and c.calls[-1][1] == "/api/rules"
    res = mcp.handle(c, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "steel_rule_remove", "arguments": {"id": "x"}}})
    assert res["result"]["isError"] is True and "off" in res["result"]["content"][0]["text"]


def test_stdio_loop(monkeypatch):
    monkeypatch.setattr(mcp, "Client", lambda base, token: FakeClient())
    inp = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n\nnot json\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"steel_levels","arguments":{"pair":"sol"}}}\n')
    out = io.StringIO()
    mcp.serve("http://x", "t", inp=inp, out=out)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert lines[0]["id"] == 1 and lines[1]["error"]["code"] == -32700
    body = json.loads(lines[2]["result"]["content"][0]["text"])
    assert body["path"] == "/api/v1/levels/SOL-USD"
