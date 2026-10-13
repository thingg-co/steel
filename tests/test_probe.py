"""The venue probe: what counts as a contract still there, and the report shape, without the network."""
import asyncio
from types import SimpleNamespace

import httpx

from steel import probe


class FakeClient:
    def __init__(self, codes):
        self.codes = codes
        self.calls = []

    async def request(self, method, url):
        self.calls.append((method, url))
        code = self.codes.get(url.split("?")[0], 404)
        if code == "boom":
            raise httpx.ConnectError("nope")
        return SimpleNamespace(status_code=code)


def test_endpoint_existence_rules():
    class Reader:
        id = "x"; HOST = "https://x.test"; endpoints = ("/v1/balance", "/v1/gone", "/v1/geo", "/v1/down", "/v1/prefix*", "/v1/addr/", "/v1/futures", "/v1/flaky")
    import sys
    sys.modules[Reader.__module__].PROBE_FUTURES_HOST = "https://fut.test"    # a second host the module declares
    c = FakeClient({"https://x.test/v1/balance": 401, "https://x.test/v1/gone": 404, "https://x.test/v1/geo": 451,
                    "https://x.test/v1/down": "boom", "https://x.test/v1/prefix": 405, "https://fut.test/v1/futures": 400,
                    "https://x.test/v1/flaky": 500})
    try:
        r = asyncio.run(probe.probe_endpoints("account", Reader, c))
    finally:
        del sys.modules[Reader.__module__].PROBE_FUTURES_HOST
    assert r["paths"]["/v1/balance"] == "401" and r["paths"]["/v1/geo"] == "geo-blocked (451)" and r["paths"]["/v1/prefix"] == "405"
    assert r["paths"]["/v1/addr/"] == "prefix (404 bare, not probed)" and r["paths"]["/v1/futures"] == "400"   # found on the second host
    assert r["paths"]["/v1/flaky"] == "500: venue-side error"
    assert r["errors"] == ["/v1/gone: 404: the path is gone", "/v1/down: ConnectError"]
    assert ("POST", "https://x.test/v1/gone") in c.calls                      # a 404 on GET is retried as an unsigned POST before it counts
    assert all(m == "GET" for m, u in c.calls if "balance" in u)              # a path that exists on GET is never POSTed


def test_source_probe_counts_products_history_and_stream():
    class Src:
        id = "fake"; venue = "spot"; default_on = True
        def __init__(self, cfg, symbols): self.symbols = symbols; self.note = None
        async def list_products(self): return {"BTC-USD", "ETH-USD"}
        async def backfill(self, symbols, cutoff): return [1, 2, 3]
        async def run(self, emit):
            for i in range(3):
                emit(i)
            await asyncio.sleep(10)
    r = asyncio.run(probe.probe_source(Src, SimpleNamespace(), stream_s=2.0))
    assert r["products"] == 2 and r["history"] == 3 and r["stream"] == 3 and r["errors"] == []

    class Dead(Src):
        async def list_products(self): raise RuntimeError("HTTP 404")
        async def run(self, emit): await asyncio.sleep(10)
    r = asyncio.run(probe.probe_source(Dead, SimpleNamespace(), stream_s=0.2))
    assert r["products"] is None and any("products: RuntimeError" in e for e in r["errors"]) and "no tick on the stream" in r["errors"][-1]
    rep = {"t": 0, "results": [r, {"kind": "account", "id": "ok", "paths": {"/v1/x": "401"}, "errors": []}], "failed": ["fake"], "critical": ["fake"]}
    text = probe.fmt(rep)
    assert "FAIL source   fake" in text and "ok   account  ok" in text and "CRITICAL: fake" in text
