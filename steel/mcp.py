"""An MCP server over Steel's API (M23): `steel mcp` speaks the Model Context Protocol on stdin
and stdout (newline-delimited JSON-RPC 2.0), so an agent (Claude Code, Claude Desktop, any MCP
client) can ask Steel what it sees: markets, the screener, candles, the tape and delta, books,
footprints, derivatives, positioning, the liquidation map, premiums, levels, options, the
model's forecast and ticket, the calendar, the prediction markets, the alerts' status and the
series alerts. Every tool is a read of `/api/v1` or the dashboard's routes with the operator
token; the series-alert tools (add, remove) are the only writes and are off unless `--rules`
(an alert is the operator's own line on a series, never an order). Nothing here can place,
change or cancel an order, move funds, change a setting or arm a venue: the tools do not exist.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import httpx

PROTOCOL = "2025-06-18"
PAIR = {"type": "string", "description": "a pair: BTC-USD, btc, ETH/USDT all resolve"}


def _tool(name: str, description: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or [], "additionalProperties": False}}


TOOLS = [
    _tool("steel_state", "Steel's state: phase, whether the model is trusted, the metrics, the paper book's equity and positions, the feeds."),
    _tool("steel_markets", "Every modeled pair with its serving venue, kind, price, 24-hour dollar volume."),
    _tool("steel_screener", "One row per pair from every stored series (price, 1-hour change, tape delta, OI and its change, funding, liquidations, book imbalance, dislocations, the model's call), sorted by a column.",
          {"sort": {"type": "string", "description": "column to sort by, descending (default delta_1h)"}, "n": {"type": "integer", "description": "rows to return (default 25)"}}),
    _tool("steel_candles", "OHLCV with buy and sell volume per candle: the tape at any timeframe for the day held, the 5-minute bars beyond.",
          {"pair": PAIR, "tf": {"type": "number", "description": "seconds per candle (default 300)"}, "n": {"type": "integer", "description": "candles (default 100)"}}, ["pair"]),
    _tool("steel_tape", "The last prints, the size-band volumes over the hour, per-bar delta and CVD.", {"pair": PAIR, "n": {"type": "integer", "description": "prints (default 30)"}}, ["pair"]),
    _tool("steel_delta", "Per-bar buy, sell, delta and CVD, optionally for one size band (0..10) or a min_usd,max_usd range.",
          {"pair": PAIR, "bars": {"type": "integer"}, "band": {"type": "string"}}, ["pair"]),
    _tool("steel_book", "The aggregate order book across venues: the ladder with venue shares, the depth bands, the per-venue books.", {"pair": PAIR, "quote": {"type": "string", "description": "USD, USDT or USDC"}}, ["pair"]),
    _tool("steel_footprint", "Bid and ask volume per price bucket per candle, POC, value area, imbalances, large prints.",
          {"pair": PAIR, "tf": {"type": "number"}, "n": {"type": "integer", "description": "candles (default 24)"}}, ["pair"]),
    _tool("steel_derivs", "Open interest, funding and mark per venue and aggregated; liquidations per bar and the recent list.", {"pair": PAIR, "bars": {"type": "integer"}}, ["pair"]),
    _tool("steel_positioning", "OI-confirmed net longs and shorts per bar and absorption flags.", {"pair": PAIR, "n": {"type": "integer"}}, ["pair"]),
    _tool("steel_liqmap", "The modeled liquidation map: clusters per leverage band above and below.", {"pair": PAIR, "n": {"type": "integer"}}, ["pair"]),
    _tool("steel_premium", "Per-venue premium to a reference venue and the perp-spot basis, per candle.", {"pair": PAIR, "reference": {"type": "string"}, "n": {"type": "integer"}}, ["pair"]),
    _tool("steel_levels", "Day, week and month opens, highs, lows and closes.", {"pair": PAIR}, ["pair"]),
    _tool("steel_options", "Deribit's chain for BTC or ETH: GEX by strike, the smile, the term structure, max pain, zero gamma, the walls.", {"asset": {"type": "string", "description": "BTC or ETH"}}, ["asset"]),
    _tool("steel_forecast", "The model's latest call on a pair: action, quantiles, P(up), size, the extra horizons, the reasons.", {"pair": PAIR}, ["pair"]),
    _tool("steel_ticket", "An order ticket for the model's current call on a pair, as text, for a human to place by hand. It is not sent anywhere.", {"pair": PAIR}, ["pair"]),
    _tool("steel_calendar", "The next scheduled events (FOMC, CPI, jobs, expiries) and the event clock."),
    _tool("steel_predictions", "Polymarket's open crypto markets with the crowd's probabilities."),
    _tool("steel_alerts", "The alert destinations' status and the series alerts (rules) with their hit counts."),
]
RULE_TOOLS = [
    _tool("steel_rule_add", "Add a series alert: a pair (or * for any), a screener series, a comparison (>, <, >=, <=, abs>, crosses_above, crosses_below) and a value. It alerts; it never trades.",
          {"pair": {"type": "string"}, "series": {"type": "string"}, "op": {"type": "string"}, "value": {"type": "number"}, "cooldown_s": {"type": "number"}, "note": {"type": "string"}}, ["pair", "series", "op", "value"]),
    _tool("steel_rule_remove", "Remove a series alert by id.", {"id": {"type": "string"}}, ["id"]),
]


class Client:
    """Steel's HTTP API with the operator token."""

    def __init__(self, base: str, token: str = "", timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.token = token
        self.http = httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    def get(self, path: str, **params) -> Any:
        r = self.http.get(self.base + path, params={k: v for k, v in params.items() if v is not None}, headers=self._headers())
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": f"HTTP {r.status_code}", "text": r.text[:300]}

    def post(self, path: str, body: dict) -> Any:
        r = self.http.post(self.base + path, json=body, headers=self._headers())
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": f"HTTP {r.status_code}", "text": r.text[:300]}


def _canon(p: str) -> str:
    from .config import canonical
    return canonical(str(p or ""))


def call_tool(client: Client, name: str, args: dict, *, rules: bool = False) -> Any:
    """Run one tool; returns the JSON the tool answers with (an {"error": ...} for a bad call)."""
    a = args or {}
    if name == "steel_state":
        s = client.get("/api/state")
        if "error" in s:
            return s
        st, m, p = s.get("status") or {}, s.get("metrics") or {}, s.get("paper") or {}
        return {"phase": st.get("phase"), "trusted": m.get("trusted"), "feed": st.get("feed"), "metrics": {k: m.get(k) for k in ("hit_rate", "n_labels", "pinball", "sharpe", "scored_days", "net_bps_day") if k in m},
                "paper": {k: p.get(k) for k in ("equity", "cash", "pnl_day", "positions", "trusted", "drawdown") if k in p}, "symbols": len(s.get("config", {}).get("symbols", []) or [])}
    if name == "steel_markets":
        return client.get("/api/v1/markets")
    if name == "steel_screener":
        d = client.get("/api/v1/screener")
        rows = d.get("rows") or []
        key = str(a.get("sort") or "delta_1h")
        rows = sorted(rows, key=lambda r: (r.get(key) is None, -(r.get(key) or 0)))
        return {"sort": key, "rows": rows[: int(a.get("n") or 25)], "n": len(rows)}
    if name == "steel_candles":
        return client.get(f"/api/v1/candles/{_canon(a.get('pair'))}", tf=a.get("tf") or 300, n=a.get("n") or 100)
    if name == "steel_tape":
        return client.get(f"/api/v1/tape/{_canon(a.get('pair'))}", n=a.get("n") or 30)
    if name == "steel_delta":
        return client.get(f"/api/v1/delta/{_canon(a.get('pair'))}", bars=a.get("bars") or 60, band=a.get("band"))
    if name == "steel_book":
        return client.get(f"/api/v1/book/{_canon(a.get('pair'))}", quote=a.get("quote"))
    if name == "steel_footprint":
        return client.get(f"/api/v1/footprint/{_canon(a.get('pair'))}", tf=a.get("tf") or 300, n=a.get("n") or 24)
    if name == "steel_derivs":
        return client.get(f"/api/v1/derivs/{_canon(a.get('pair'))}", bars=a.get("bars") or 72)
    if name == "steel_positioning":
        return client.get(f"/api/v1/positioning/{_canon(a.get('pair'))}", n=a.get("n") or 288)
    if name == "steel_liqmap":
        return client.get(f"/api/v1/liqmap/{_canon(a.get('pair'))}", n=a.get("n") or 288)
    if name == "steel_premium":
        return client.get(f"/api/v1/premium/{_canon(a.get('pair'))}", reference=a.get("reference") or "binance", n=a.get("n") or 288)
    if name == "steel_levels":
        return client.get(f"/api/v1/levels/{_canon(a.get('pair'))}")
    if name == "steel_options":
        return client.get(f"/api/v1/options/{str(a.get('asset') or 'BTC').upper()}")
    if name == "steel_forecast":
        return client.get(f"/api/v1/forecast/{_canon(a.get('pair'))}")
    if name == "steel_ticket":
        return client.get(f"/api/ticket/{_canon(a.get('pair'))}")
    if name == "steel_calendar":
        return client.get("/api/v1/calendar")
    if name == "steel_predictions":
        return client.get("/api/v1/predictions")
    if name == "steel_alerts":
        return {"destinations": client.get("/api/alerts"), "rules": client.get("/api/rules")}
    if name in ("steel_rule_add", "steel_rule_remove"):
        if not rules:
            return {"error": "the series-alert tools are off: start the server with `steel mcp --rules`"}
        if name == "steel_rule_add":
            return client.post("/api/rules", {"action": "add", "pair": a.get("pair") or "*", "series": a.get("series"), "op": a.get("op"), "value": a.get("value"),
                                              "cooldown_s": a.get("cooldown_s") or 900, "note": a.get("note") or "via MCP"})
        return client.post("/api/rules", {"action": "remove", "id": a.get("id")})
    return {"error": f"unknown tool {name!r}"}


def handle(client: Client, msg: dict, *, rules: bool = False) -> dict | None:
    """One JSON-RPC request in, one response out (None for a notification)."""
    mid = msg.get("id")
    method = str(msg.get("method") or "")
    params = msg.get("params") or {}
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                                                        "serverInfo": {"name": "steel", "version": "1.0"},
                                                        "instructions": "Steel is a self-hosted crypto market model. These tools read what it sees; none can trade. "
                                                                        "Pairs are BASE-USD. Dollar amounts are USD; rates are fractions; bps are basis points."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS + (RULE_TOOLS if rules else [])}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        try:
            out = call_tool(client, name, params.get("arguments") or {}, rules=rules)
        except httpx.HTTPError as e:
            out = {"error": f"Steel unreachable: {type(e).__name__}: {e}"}
        is_err = isinstance(out, dict) and bool(out.get("error"))
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, default=str)}], "isError": is_err}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(base: str, token: str, *, rules: bool = False, inp=None, out=None) -> None:
    """Read newline-delimited JSON-RPC from `inp`, write answers to `out`, until EOF."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    client = Client(base, token)
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            res = handle(client, m, rules=rules)
            if res is not None:
                out.write(json.dumps(res, default=str) + "\n")
                out.flush()


def main(argv: list[str]) -> None:
    """steel mcp [--url http://127.0.0.1:8100] [--token T] [--rules]"""
    from . import auth
    from .config import Config
    cfg = Config()
    url = f"http://{cfg.host}:{cfg.port}"
    token = ""
    rules = "--rules" in argv
    it = iter(argv)
    for a in it:
        if a == "--url":
            url = next(it, url)
        elif a == "--token":
            token = next(it, "")
    if not token:
        try:
            token, _ = auth.load(cfg.state_dir, cfg.token, create=False)
        except Exception:  # noqa: BLE001
            token = ""
    print(f"steel mcp: {url}, {'rule tools on' if rules else 'reads only'}, token {'set' if token else 'none'}", file=sys.stderr)
    serve(url, token, rules=rules)
