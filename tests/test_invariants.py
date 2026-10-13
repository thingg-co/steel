"""Tests that pin the project's hard rules from CLAUDE.md"""
import re
from pathlib import Path

import numpy

from steel.accounts import ACCOUNTS

from steel.config import Config
from steel.paper import PaperBook

TESTS_DIR = Path(__file__).resolve().parents[1]
STEEL_DIR = TESTS_DIR / "steel"


ORDER_WORDS = ["/orders", "placeorder", "previeworder", "cancelorder", "replaceorder", "addorder", "/order/", "/order?", "/order\"",
               "withdraw", "transfer", "amend", "borrow", "repay", "/convert", "/loan", "submit_order", "create_order", "trade/order",
               "/trade/place", "/trade/cancel", "/trade/batch"]
READ_WORDS = re.compile(r"account|balance|wallet|asset|margin|position|clearinghouse|user|portfolio|fund|summar|address|subaccount|"
                        r"/info$|holding|equity|^/v1/me$|collateral|capital|fee|commission|notionalvolume|/TradeVolume$", re.I)
# A reader's fee-tier read (what the account is charged: `Account.fees`). Its path may carry "trade"
# (/api/v5/account/trade-fee, /sapi/v1/asset/tradeFee, Kraken's /0/private/TradeVolume), so that one
# word is excused for it; every other order, withdrawal and transfer word still applies.
FEE_READS = re.compile(r"fee|/TradeVolume$", re.I)
ORDER_PATH = re.compile(r"order|trade|withdraw|transfer|cancel|place|amend|convert|borrow|repay|loan|stake|swap", re.I)
ORDER_PATH_FEE = re.compile(r"order|withdraw|transfer|cancel|place|amend|convert|borrow|repay|loan|stake|swap", re.I)


def test_steel_never_places_orders():
    """Steel never places, changes, or cancels orders on any venue, nor moves funds: no order,
    withdrawal or transfer endpoint anywhere in the package, and every account reader's declared
    endpoints read balances, positions or the account's fee tier."""
    for pyfile in STEEL_DIR.rglob("*.py"):
        if pyfile.parent.name == "execution":                # the one package that may hold an order endpoint (behind its own gate)
            continue
        # every string literal that looks like a path or URL (prose may say "borrow"; a path may not)
        for lit in re.findall(r"""(?:f|r|b)?["']([^"'\n]*[/.][^"'\n]*)["']""", pyfile.read_text()):
            low = lit.lower()
            if "/" not in low and "http" not in low:
                continue
            for word in ORDER_WORDS:
                assert word not in low, f"{pyfile.relative_to(STEEL_DIR)} contains '{word}' in {lit!r}"
    for cls in ACCOUNTS:
        assert cls.endpoints, f"{cls.id} declares no endpoints"
        for e in cls.endpoints:
            assert READ_WORDS.search(e), f"{cls.id} endpoint {e} does not read like a balance, position or fee read"
            assert not (ORDER_PATH_FEE if FEE_READS.search(e) else ORDER_PATH).search(e), f"{cls.id} endpoint {e} is not a read"


def test_execution_is_off_and_gated_by_default():
    """Nothing executes out of the box: the switch is off, no venue is armed, every cap is zero,
    the only HTTP in the package is the gate's, trade keys are their own gitignored files, and
    no module outside steel/execution holds an order endpoint (the grep above)."""
    from steel.execution import ExecutionGate, Order
    cfg = Config()
    assert cfg.execution is False and cfg.execution_venues == "" and cfg.execution_dry_run is True
    assert cfg.exec_max_order_usd == 0.0 and cfg.exec_max_day_usd == 0.0
    gate = ExecutionGate(cfg, state_dir="/nonexistent-steel-state")
    assert "execution is off" in gate.check(Order("binance", "BTC-USD", "buy", 0.01, notional_usd=800.0))
    pkg = STEEL_DIR / "execution"
    for pyfile in pkg.glob("*.py"):
        text = pyfile.read_text()
        if pyfile.name == "base.py":
            assert text.count("httpx.AsyncClient(") == 1
            continue
        for word in ("httpx", "aiohttp", "requests.", "urllib.request", "AsyncClient"):
            assert word not in text, f"steel/execution/{pyfile.name} talks to the network outside the gate: {word}"
    assert "*-trade.json" in (Path(__file__).resolve().parents[1] / ".gitignore").read_text().split()


def test_the_only_http_in_accounts_is_the_gate():
    """Every venue reader goes through `Account._request`, which refuses undeclared paths: no
    reader imports an HTTP client or calls one directly, and only GET and POST exist in the gate."""
    pkg = STEEL_DIR / "accounts"
    for pyfile in pkg.glob("*.py"):
        text = pyfile.read_text()
        if pyfile.name == "base.py":
            assert text.count("httpx.AsyncClient(") == 1 and "c.request(method, url" in text
            for method in (".put(", ".delete(", ".patch("):
                assert method not in text
            continue
        for word in ("httpx", "aiohttp", "requests.", "urllib.request", "websockets", ".get(f\"http", ".post(f\"http", "AsyncClient"):
            assert word not in text, f"steel/accounts/{pyfile.name} talks to the network outside the gate: {word}"
        assert "self._json(" in text or "self._request(" in text or pyfile.name in ("__init__.py", "registry.py"), \
            f"steel/accounts/{pyfile.name} does not use the gate"


def test_brief_backend_defaults_are_local():
    """The narrative brief runs on a local LLM backend only."""
    cfg = Config()
    from urllib.parse import urlparse
    host = urlparse(cfg.brief_openai_base).hostname
    assert host in ("localhost", "127.0.0.1"), f"brief_openai_base hostname is {host}, expected localhost or 127.0.0.1"
    ollama_host = urlparse(cfg.ollama_host).hostname
    assert ollama_host in ("localhost", "127.0.0.1"), f"ollama_host hostname is {ollama_host}, expected localhost or 127.0.0.1"
    brief_source = (STEEL_DIR / "brief.py").read_text()
    for domain in ["api.openai.com", "anthropic.com", "googleapis.com", "openrouter.ai", "api.together", "groq.com"]:
        assert domain not in brief_source, f"steel/brief.py contains {domain} - cloud APIs not allowed"


def test_config_builds_from_an_empty_environment(monkeypatch):
    """Config constructs without environment variables set, with a crypto universe and Steel's port."""
    import os
    for key in list(os.environ):
        if key.startswith("STEEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(Path(__file__).parent)            # no key files here
    cfg = Config()
    assert cfg.port == 8100                             # Flint owns 8000 on a box that runs both
    assert isinstance(cfg.symbols, list) and len(cfg.symbols) >= 40
    assert all(s.endswith("-USD") for s in cfg.symbols)
    assert "BTC-USD" in cfg.symbols and "ETH-USD" in cfg.symbols
    assert cfg.brief_enabled is False
    assert cfg.coinbase_creds == ("", "") and cfg.kraken_creds == ("", "") and cfg.coingecko_key == ""
    assert cfg.bars_per_day == 288


def test_paper_book_never_holds_negative_spot_or_leverage():
    """PaperBook constraints under a fuzz: no negative spot, no leverage on spot, every short an
    isolated-margin position whose margin never exceeded short_max_frac of the equity it was
    opened against, cash finite."""
    book = PaperBook(start=100_000.0, cost_bps=8.0, perp_fee_bps=5.0, short_max_frac=0.02, short_leverage=2.0)
    symbols = [f"S{i}" for i in range(3)]
    rng = numpy.random.default_rng(0)
    ts = 1_800_000_000.0
    prices = {s: 100.0 for s in symbols}
    opened_against: dict[str, float] = {}
    for step in range(300):
        for s in symbols:
            prices[s] = prices[s] * numpy.exp(rng.normal(0, 0.01))
        targets = {s: (0.0 if rng.random() < 0.33 else rng.uniform(-0.3, 0.3)) for s in symbols}
        quotes = {s: {"bid": prices[s] * (1 - 0.0005), "ask": prices[s] * (1 + 0.0005)} for s in symbols}
        funding = {s: rng.uniform(-0.001, 0.001) for s in symbols}
        eq_before = book.equity(prices, ts)
        before = set(book.shorts)
        book.rebalance(targets, prices, ts, quotes=quotes, funding=funding)
        for s in set(book.shorts) - before:
            opened_against[s] = eq_before
        for sym, pos in book.pos.items():
            assert pos > 0, f"negative spot: {sym} = {pos}"
        for sym, sh in book.shorts.items():
            assert sh["qty"] > 0 and sh["margin"] > 0 and sh["liq"] > sh["entry"]
            assert sh["margin"] <= 0.02 * opened_against[sym] * 1.02 + 1e-6, f"margin too high for {sym}"
            assert sym not in book.pos                                    # never both sides of a name
        assert numpy.isfinite(book.cash)
        equity = book.equity(prices, ts)
        spot = sum(book.pos[s] * prices[s] for s in book.pos)
        assert spot <= equity * (1 + 1e-6) + 1e-6, f"leverage: spot {spot} > equity {equity}"
        ts += 300
    assert book.n_trades > 0, "The fuzz did not trade"


def test_defined_risk_a_short_cannot_lose_more_than_its_margin():
    """A short marks at zero at worst: equity never falls below cash, whatever the price does,
    and at the liquidation price the loss is exactly the margin."""
    book = PaperBook(start=100_000.0, cost_bps=0.0, perp_fee_bps=0.0, short_max_frac=0.05, short_leverage=3.0)
    ts = 1_800_000_000.0
    book.rebalance({"S0": -0.3}, {"S0": 200.0}, ts)
    sh = book.shorts["S0"]
    cash = book.cash
    for px in (1.0, 200.0, 266.0, 267.0, 10_000.0, 1e9):
        eq = book.equity({"S0": px}, ts)
        assert numpy.isfinite(eq) and eq >= cash - 1e-6, f"equity {eq} < cash {cash} at {px}"
    assert book.equity({"S0": sh["liq"]}, ts) == 100_000.0 - sh["margin"]
    assert book.equity({"S0": 1e9}, ts) == 100_000.0 - sh["margin"]
