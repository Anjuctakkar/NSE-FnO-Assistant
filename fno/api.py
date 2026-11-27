"""
Hosting the F&O UI inside the dashboard (FastAPI). Two parts:

  Backend       all the logic: fetch the dashboard's adjusted price history for the stock, run the CPU-bound work off the
                event loop, keep heavy jobs to a few at a time. Plain asyncio, so it is tested without FastAPI.
  build_router  about thirty lines of glue that map HTTP onto Backend and apply the dashboard's sign-in.

Wiring (server.py of the dashboard):

    import fno.api, fno.service
    from pathlib import Path

    async def _fno_bars(symbol):                       # the dashboard's own split/bonus-adjusted history
        return await _fetch_history_adjusted(_server_url(ANALYSIS_SERVER), symbol, 12)

    _fno = fno.api.Backend(fno.service.Context(config.DATA_DIR / "fno"), fetch_bars=_fno_bars, heavy=heavy_slot)
    app.include_router(fno.api.build_router(_fno, auth.current_user, auth.require_admin))

and copy fno/static/fno.js to the dashboard's static/ folder, then add one line to index.html:

    <script src="/fno.js" defer></script>

It adds an "F&O" tab by itself and works under the dashboard's content-security policy (no external scripts or styles).
Syncing and importing bhavcopy change shared data, so they are admin-only; everything else needs a signed-in user.
The user's suggestion log stays in their own browser, like the dashboard's Evaluation list.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from . import service


class Backend:
    def __init__(self, ctx: service.Context, fetch_bars: Optional[Callable[[str], Awaitable[list[dict[str, Any]]]]] = None,
                 heavy: Optional[Callable[[], Any]] = None):
        self.ctx, self.fetch_bars, self._heavy = ctx, fetch_bars, heavy

    def heavy(self):
        return self._heavy() if self._heavy else _Null()

    async def _bars(self, req: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
        """Cash-market history for real-data requests. If it cannot be fetched the engine falls back to the bhavcopy's own
        underlying close and says so in the response notes, so a failure here degrades the answer instead of breaking it."""
        sym = service._sym(req)                              # validate before touching NSE
        if service._source(req) != "bhavcopy" or not self.fetch_bars:
            return None
        try:
            return service.bars_from_records(await self.fetch_bars(sym)) or None
        except Exception:                                    # noqa: BLE001
            return None

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(service.status, self.ctx)

    async def live_quote(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.live_quote, self.ctx, req)

    async def test_breeze_connection(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.test_breeze_connection, req)

    async def chat(self, req: dict[str, Any]) -> dict[str, str]:
        return await asyncio.to_thread(service.run_chat, req)

    async def symbols(self, source: str) -> dict[str, Any]:
        return await asyncio.to_thread(service.list_symbols, self.ctx, source)

    async def expiries(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.list_expiries, self.ctx, req)

    async def suggest(self, req: dict[str, Any]) -> dict[str, Any]:
        bars = await self._bars(req)
        async with self.heavy():
            return await asyncio.to_thread(service.run_suggest, self.ctx, req, bars)

    async def backtest(self, req: dict[str, Any]) -> dict[str, Any]:
        bars = await self._bars(req)
        async with self.heavy():
            return await asyncio.to_thread(service.run_backtest_req, self.ctx, req, bars)

    async def scan(self, req: dict[str, Any]) -> dict[str, Any]:
        """Suggest across the whole market: no stock, no cash-market fetch (the scan uses the closes in the bhavcopy index)."""
        async with self.heavy():
            return await asyncio.to_thread(service.run_scan, self.ctx, req)

    async def index(self, req: dict[str, Any]) -> dict[str, Any]:
        async with self.heavy():
            return await asyncio.to_thread(service.index_step, self.ctx, req)

    async def evaluate(self, req: dict[str, Any]) -> dict[str, Any]:
        async with self.heavy():
            return await asyncio.to_thread(service.run_evaluate, self.ctx, req)

    async def sync(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.sync_bhavcopy, self.ctx, req)

    async def live_scan_start(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.live_scan_start, self.ctx, req)      # returns at once; the scan runs in its own thread

    async def live_scan_status(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.live_scan_status, self.ctx, req)

    async def live_scan_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.live_scan_cancel, self.ctx, req)

    async def calibrate(self, req: dict[str, Any]) -> dict[str, Any]:
        async with self.heavy():
            return await asyncio.to_thread(service.run_calibrate, self.ctx, req)

    async def import_file(self, name: str, data: bytes) -> dict[str, Any]:
        return await asyncio.to_thread(service.import_bhavcopy, self.ctx, name, data)


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def build_router(backend: Backend, require_user, require_admin):
    """FastAPI router for /api/fno/*. require_user / require_admin are the dashboard's auth dependencies."""
    from fastapi import APIRouter, Depends, HTTPException, Request

    r = APIRouter(prefix="/api/fno")

    async def call(coro):
        try:
            return await coro
        except service.UpstreamServiceError as e:
            raise HTTPException(502, str(e))
        except service.ServiceError as e:
            raise HTTPException(400, str(e))

    @r.get("/status", dependencies=[Depends(require_user)])
    async def status():
        return await call(backend.status())

    @r.get("/live/quote", dependencies=[Depends(require_user)])
    async def live_quote(symbol: str = ""):
        return await call(backend.live_quote({"symbol": symbol}))

    @r.post("/live/quote", dependencies=[Depends(require_user)])
    async def live_quote_with_token(payload: dict[str, Any]):
        return await call(backend.live_quote(payload))

    @r.post("/breeze/test", dependencies=[Depends(require_user)])
    async def test_breeze_connection(payload: dict[str, Any]):
        return await call(backend.test_breeze_connection(payload))

    @r.post("/chat", dependencies=[Depends(require_user)])
    async def chat(payload: dict[str, Any]):
        return await call(backend.chat(payload))

    @r.get("/symbols", dependencies=[Depends(require_user)])
    async def symbols(source: str = "bhavcopy"):
        return await call(backend.symbols(source))

    @r.get("/expiries", dependencies=[Depends(require_user)])
    async def expiries(source: str = "bhavcopy", asof: str = "", symbol: str = ""):
        return await call(backend.expiries({"source": source, "asof": asof, "symbol": symbol}))

    @r.post("/suggest", dependencies=[Depends(require_user)])
    async def suggest(payload: dict[str, Any]):
        return await call(backend.suggest(payload))

    @r.post("/backtest", dependencies=[Depends(require_user)])
    async def backtest(payload: dict[str, Any]):
        return await call(backend.backtest(payload))

    @r.post("/scan", dependencies=[Depends(require_user)])
    async def scan(payload: dict[str, Any]):
        return await call(backend.scan(payload))

    @r.post("/index", dependencies=[Depends(require_user)])
    async def index(payload: dict[str, Any]):
        return await call(backend.index(payload))

    @r.post("/evaluate", dependencies=[Depends(require_user)])
    async def evaluate(payload: dict[str, Any]):
        return await call(backend.evaluate(payload))

    @r.post("/live-scan/start", dependencies=[Depends(require_user)])
    async def live_scan_start(payload: dict[str, Any]):
        return await call(backend.live_scan_start(payload))

    @r.get("/live-scan/status", dependencies=[Depends(require_user)])
    async def live_scan_status(id: str = ""):
        return await call(backend.live_scan_status({"id": id}))

    @r.post("/live-scan/cancel", dependencies=[Depends(require_user)])
    async def live_scan_cancel(payload: dict[str, Any]):
        return await call(backend.live_scan_cancel(payload))

    @r.post("/calibrate", dependencies=[Depends(require_admin)])         # writes the shared calibration record
    async def calibrate(payload: dict[str, Any]):
        return await call(backend.calibrate(payload))

    @r.post("/bhavcopy/sync", dependencies=[Depends(require_admin)])
    async def sync(payload: dict[str, Any]):
        return await call(backend.sync(payload))

    @r.post("/bhavcopy/import", dependencies=[Depends(require_admin)])
    async def import_file(request: Request, name: str = ""):
        return await call(backend.import_file(name[:120], await request.body()))

    return r
