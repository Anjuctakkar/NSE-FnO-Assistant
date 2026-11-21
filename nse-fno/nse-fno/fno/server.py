"""
Standalone web server for the F&O UI. Standard library only, so it runs anywhere Python does.

    python -m fno serve --data-dir data            # http://127.0.0.1:8100

It has NO sign-in, so it refuses to listen on anything but the local machine. To share the UI with other people, host it
inside the dashboard instead (fno/api.py), where Google sign-in, per-user limits and HTTPS already exist.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import service

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/fno.js": ("fno.js", "application/javascript; charset=utf-8")}
MAX_JSON = 8 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
log = logging.getLogger("fno.server")


def make_handler(ctx: service.Context, heavy: threading.Semaphore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fno"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

        # -- plumbing
        def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):           # the tab was closed or reloaded mid-response: nothing to report
                pass

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(service.jsonable(obj), allow_nan=False).encode(), "application/json")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True                                   # not a browser cross-site request (curl, tests)
            return urlparse(origin).netloc == self.headers.get("Host", "")

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise service.ServiceError(f"Request too large (limit {limit // (1024 * 1024)} MB).")
            return self.rfile.read(n)

        def _run(self, fn, *args) -> None:
            try:
                self._json(200, fn(*args))
            except service.UpstreamServiceError as e:
                self._json(502, {"detail": str(e)})
            except service.ServiceError as e:
                self._json(400, {"detail": str(e)})
            except Exception:                                 # noqa: BLE001 - never leak internals to the page
                log.exception("request failed: %s", self.path)
                self._json(500, {"detail": "Something went wrong. The details are in the server window."})

        # -- routes
        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in FILES:
                name, ctype = FILES[u.path]
                self._send(200, (STATIC / name).read_bytes(), ctype)
            elif u.path == "/health":
                self._json(200, {"ok": True})
            elif u.path == "/api/auth/status":                # the UI asks who is signed in; standalone there is no sign-in
                self._json(200, {"auth_enabled": False, "user": None})
            elif u.path == "/api/fno/status":
                self._run(service.status, ctx)
            elif u.path == "/api/fno/live/quote":
                self._run(service.live_quote, ctx, {"symbol": parse_qs(u.query).get("symbol", [""])[0]})
            elif u.path == "/api/fno/symbols":
                self._run(service.list_symbols, ctx, parse_qs(u.query).get("source", ["bhavcopy"])[0])
            else:
                self._json(404, {"detail": "Not found."})

        def do_POST(self) -> None:
            if not self._same_origin():
                return self._json(403, {"detail": "Cross-site request blocked."})
            u = urlparse(self.path)
            try:
                if u.path == "/api/fno/bhavcopy/import":
                    name = parse_qs(u.query).get("name", [""])[0][:120]
                    return self._run(service.import_bhavcopy, ctx, name, self._body(MAX_UPLOAD))
                req = json.loads(self._body(MAX_JSON) or b"{}")
                if not isinstance(req, dict):
                    raise service.ServiceError("Send a JSON object.")
            except json.JSONDecodeError:
                return self._json(400, {"detail": "That is not valid JSON."})
            except service.ServiceError as e:
                return self._json(400, {"detail": str(e)})
            routes = {"/api/fno/suggest": service.run_suggest, "/api/fno/backtest": service.run_backtest_req,
                      "/api/fno/evaluate": service.run_evaluate, "/api/fno/bhavcopy/sync": service.sync_bhavcopy,
                      "/api/fno/scan": service.run_scan, "/api/fno/index": service.index_step,
                      "/api/fno/live/quote": service.live_quote}
            fn = routes.get(u.path)
            if fn is None:
                return self._json(404, {"detail": "Not found."})
            if fn in (service.run_suggest, service.run_backtest_req, service.run_evaluate, service.run_scan, service.index_step):
                with heavy:                                   # a few at a time: these are CPU-bound
                    return self._run(fn, ctx, req)
            return self._run(fn, ctx, req)

    return Handler


def serve(ctx: service.Context, host: str = "127.0.0.1", port: int = 8100, max_heavy: int = 2) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise SystemExit("This server has no sign-in, so it only listens on the local machine (127.0.0.1). "
                         "To share the UI, host it inside the dashboard (fno/api.py).")
    return ThreadingHTTPServer((host, port), make_handler(ctx, threading.Semaphore(max_heavy)))
