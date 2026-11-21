import asyncio
import tempfile

try:
    from .test_service import first_with_suggestion, seeded_ctx, strict
except ImportError:
    from test_service import first_with_suggestion, seeded_ctx, strict

from fno import service
from fno.api import Backend, build_router


class Counting:
    """An async context manager standing in for the dashboard's heavy_slot."""
    def __init__(self):
        self.entered = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1

    async def __aexit__(self, *a):
        return False


def records(bars):
    return [{"Date": str(b["date"]), "OPEN": str(b["open"]), "High": b["high"], "low": b["low"], "Close": str(b["close"])} for b in bars]


def test_bars_from_records_handles_the_dashboards_shape():
    out = service.bars_from_records([{"date": "2026-01-02T00:00:00", "close": "10.5"}, {"DATE": "2026-01-01", "Open": "9", "HIGH": 11, "Low": "8", "CLOSE": 10},
                                     {"date": "bad", "close": 1}, {"date": "2026-01-03"}, {}])
    assert [b["date"].isoformat() for b in out] == ["2026-01-01", "2026-01-02"]
    assert out[0]["open"] == 9.0 and out[0]["high"] == 11.0 and out[1]["high"] == 10.5          # missing high/low fall back to the close


def test_backend_uses_the_dashboards_bars_and_the_heavy_slot():
    ctx, bars = seeded_ctx()
    calls, heavy = [], Counting()

    async def fetch(sym):
        calls.append(sym)
        return records(bars)

    be = Backend(ctx, fetch_bars=fetch, heavy=heavy)
    asof = ctx.store.dates()[-3].isoformat()
    r = asyncio.run(be.suggest({"symbol": "test", "source": "bhavcopy", "asof": asof, "min_score": 0}))
    strict(r)
    assert calls == ["TEST"] and heavy.entered == 1
    assert not any("underlying close recorded" in n for n in r["data_notes"])                  # real OHLC reached the engine
    asyncio.run(be.backtest({"symbol": "TEST", "source": "bhavcopy", "random_runs": 1}))
    assert heavy.entered == 2 and calls == ["TEST", "TEST"]
    assert asyncio.run(be.status())["bhavcopy"]["days"] == len(ctx.store.dates())
    assert asyncio.run(be.symbols("bhavcopy"))["symbols"] == ["TEST"]


def test_a_failing_price_feed_degrades_the_answer_instead_of_breaking_it():
    ctx, _ = seeded_ctx()

    async def broken(sym):
        raise RuntimeError("NSE did not reply")

    r = asyncio.run(Backend(ctx, fetch_bars=broken).suggest({"symbol": "TEST", "source": "bhavcopy", "min_score": 0}))
    assert any("underlying close recorded" in n for n in r["data_notes"])                       # said so in the response


def test_bad_requests_never_reach_nse_and_synthetic_never_fetches():
    ctx, _ = seeded_ctx()
    calls = []

    async def fetch(sym):
        calls.append(sym)
        return []

    be = Backend(ctx, fetch_bars=fetch)
    for req in ({"symbol": "../etc"}, {"symbol": "TEST", "source": "carrier-pigeon"}, {}):
        try:
            asyncio.run(be.suggest(req))
        except service.ServiceError:
            pass
        else:
            raise AssertionError(f"expected ServiceError for {req}")
    asyncio.run(be.suggest({"symbol": "DEMO", "source": "synthetic", "asof": "2024-09-20", "min_score": 0}))
    assert calls == []


def test_backend_runs_concurrent_requests_without_corrupting_each_other():
    ctx, _ = seeded_ctx()
    be = Backend(ctx)
    d = ctx.store.dates()[-5].isoformat()

    async def go():
        return await asyncio.gather(*[be.suggest({"symbol": "TEST", "source": "bhavcopy", "asof": d, "min_score": 0}) for _ in range(4)])

    results = asyncio.run(go())
    assert len({r["asof"] for r in results}) == 1 and len({len(r["suggestions"]) for r in results}) == 1


def test_authenticated_api_exposes_breeze_connection_test():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    app = FastAPI()
    backend = Backend(service.Context(tempfile.mkdtemp()))
    app.include_router(build_router(backend, lambda: None, lambda: None))
    credentials = {"api_key": "key", "api_secret": "secret", "session_token": "session"}
    with patch.object(service, "test_breeze_connection", return_value={"ok": True, "message": "Connected."}) as test_connection:
        response = TestClient(app).post("/api/fno/breeze/test", json=credentials)
    assert response.status_code == 200 and response.json()["ok"] is True
    test_connection.assert_called_once_with(credentials)


def test_authenticated_api_exposes_model_chat():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    app = FastAPI()
    backend = Backend(service.Context(tempfile.mkdtemp()))
    app.include_router(build_router(backend, lambda: None, lambda: None))
    payload = {"provider": "openai", "model": "gpt-4.1-mini", "api_key": "temporary",
               "messages": [{"role": "user", "content": "What is the quote?"}], "context": "TCS LTP ₹100"}
    result = {"text": "TCS is at ₹100.", "provider": "openai", "model": "gpt-4.1-mini"}
    with patch.object(service, "run_chat", return_value=result) as model_chat:
        response = TestClient(app).post("/api/fno/chat", json=payload)
    assert response.status_code == 200 and response.json() == result
    model_chat.assert_called_once_with(payload)
