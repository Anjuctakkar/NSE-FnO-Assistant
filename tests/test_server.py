import json
import tempfile
import threading
import urllib.error
import urllib.request

try:
    from .test_bhavcopy import as_zip, udiff_csv
except ImportError:
    from test_bhavcopy import as_zip, udiff_csv

import datetime as dt

from fno import server, service


def start(ctx=None):
    ctx = ctx or service.Context(tempfile.mkdtemp())
    srv = server.serve(ctx, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", ctx


def call(url, body=None, headers=None, method=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers or {"Content-Type": "application/json"}, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_pages_health_and_security_headers():
    srv, base, _ = start()
    try:
        code, h, body = call(base + "/")
        assert code == 200 and b'id="fno-root"' in body and "text/html" in h["Content-Type"]
        assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff" and h["Cache-Control"] == "no-store"
        code, h, body = call(base + "/fno.js")
        assert code == 200 and "javascript" in h["Content-Type"] and b"NSE F&O UI" in body
        code, h, body = call(base + "/laxmi-logo-dark.png")
        logo = (server.STATIC / "laxmi-logo-dark.png").read_bytes()
        assert code == 200 and h["Content-Type"] == "image/png" and body == logo
        assert json.loads(call(base + "/health")[2]) == {"ok": True}
        assert json.loads(call(base + "/api/auth/status")[2]) == {"auth_enabled": False, "user": None}      # the UI's sign-in probe
        assert call(base + "/nope")[0] == 404 and call(base + "/../../etc/passwd")[0] == 404
    finally:
        srv.shutdown()


def test_api_round_trip_on_synthetic_data():
    srv, base, _ = start()
    try:
        syms = json.loads(call(base + "/api/fno/symbols?source=synthetic")[2])
        assert "DEMO" in syms["symbols"]
        code, _, body = call(base + "/api/fno/suggest", {"symbol": "ALPHA", "source": "synthetic", "asof": "2024-09-20", "min_score": 0})
        r = json.loads(body)                                                   # strict JSON: no NaN or Infinity made it through
        assert code == 200 and r["symbol"] == "ALPHA" and "regime" in r
        code, _, body = call(base + "/api/fno/suggest", {"symbol": "TCS", "source": "bhavcopy"})
        assert code == 400 and "No bhavcopy is stored yet" in json.loads(body)["detail"]          # a message the user can act on
        assert call(base + "/api/fno/suggest", raw=b"{not json")[0] == 400
        assert call(base + "/api/fno/suggest", raw=b"[1,2]")[0] == 400
    finally:
        srv.shutdown()


def test_live_quote_endpoint_calls_breeze_market_service():
    from unittest.mock import patch

    srv, base, ctx = start()
    try:
        with patch.object(service, "live_quote", return_value={"provider": "ICICI Breeze", "symbol": "TCS", "ltp": 2406.6}) as quote:
            code, _, body = call(base + "/api/fno/live/quote?symbol=TCS")
        assert code == 200 and json.loads(body)["ltp"] == 2406.6
        quote.assert_called_once_with(ctx, {"symbol": "TCS"})
        with patch.object(service, "live_quote", return_value={"provider": "ICICI Breeze", "symbol": "TCS", "ltp": 2406.6}) as quote:
            code, _, body = call(base + "/api/fno/live/quote", {"symbol": "TCS", "session_token": "entered-token"})
        assert code == 200 and json.loads(body)["ltp"] == 2406.6
        quote.assert_called_once_with(ctx, {"symbol": "TCS", "session_token": "entered-token"})
    finally:
        srv.shutdown()


def test_breeze_connection_test_endpoint_forwards_credentials():
    from unittest.mock import patch

    srv, base, _ = start()
    credentials = {"api_key": "key", "api_secret": "secret", "session_token": "session"}
    try:
        with patch.object(service, "test_breeze_connection", return_value={"ok": True, "message": "Connected."}) as test_connection:
            code, _, body = call(base + "/api/fno/breeze/test", credentials)
        assert code == 200 and json.loads(body)["ok"] is True
        test_connection.assert_called_once_with(credentials)
    finally:
        srv.shutdown()


def test_model_chat_endpoint_forwards_request():
    from unittest.mock import patch

    srv, base, _ = start()
    payload = {"provider": "openai", "model": "gpt-4.1-mini", "api_key": "temporary",
               "messages": [{"role": "user", "content": "What is the quote?"}], "context": "TCS LTP ₹100"}
    result = {"text": "TCS is at ₹100.", "provider": "openai", "model": "gpt-4.1-mini"}
    try:
        with patch.object(service, "run_chat", return_value=result) as model_chat:
            code, _, body = call(base + "/api/fno/chat", payload)
        assert code == 200 and json.loads(body) == result
        model_chat.assert_called_once_with(payload)
    finally:
        srv.shutdown()


def test_import_endpoint_accepts_raw_bytes_and_rejects_junk():
    srv, base, ctx = start()
    try:
        d, ex = dt.date(2026, 10, 1), dt.date(2026, 10, 27)
        code, _, body = call(base + "/api/fno/bhavcopy/import?name=fo.zip", raw=as_zip("x.csv", udiff_csv(d, ex)), headers={"Content-Type": "application/octet-stream"})
        r = json.loads(body)
        assert code == 200 and r["imported"]["date"] == "2026-10-01" and r["bhavcopy"]["days"] == 1
        assert json.loads(call(base + "/api/fno/status")[2])["bhavcopy"]["symbols"] == 1
        code, _, body = call(base + "/api/fno/expiries?asof=2026-10-01")
        assert code == 200 and json.loads(body)["expiries"] == ["2026-10-27"]
        code, _, body = call(base + "/api/fno/expiries?asof=2026-10-01&symbol=TEST")
        assert code == 200 and json.loads(body)["expiries"] == ["2026-10-27"]
        code, _, body = call(base + "/api/fno/bhavcopy/import?name=junk.csv", raw=b"a,b\n1,2\n", headers={"Content-Type": "application/octet-stream"})
        assert code == 400 and "junk.csv" in json.loads(body)["detail"]
    finally:
        srv.shutdown()


def test_cross_site_posts_are_blocked_and_same_origin_allowed():
    srv, base, _ = start()
    try:
        body = {"symbol": "DEMO", "source": "synthetic", "asof": "2024-09-20", "min_score": 0}
        evil = call(base + "/api/fno/suggest", body, {"Content-Type": "application/json", "Origin": "http://evil.example"})
        assert evil[0] == 403 and "Cross-site" in json.loads(evil[2])["detail"]
        ok = call(base + "/api/fno/suggest", body, {"Content-Type": "application/json", "Origin": base})
        assert ok[0] == 200
    finally:
        srv.shutdown()


def test_unexpected_errors_do_not_leak_internals():
    import logging
    logging.getLogger("fno.server").disabled = True                    # the server logs the injected failure; the test only checks the client
    srv, base, _ = start()
    original = service.run_suggest
    try:
        service.run_suggest = lambda ctx, req: (_ for _ in ()).throw(RuntimeError("secret /etc/passwd detail"))
        code, _, body = call(base + "/api/fno/suggest", {"symbol": "DEMO", "source": "synthetic"})
        assert code == 500 and "secret" not in body.decode() and "passwd" not in body.decode()
    finally:
        service.run_suggest = original
        logging.getLogger("fno.server").disabled = False
        srv.shutdown()


def test_refuses_to_listen_beyond_this_machine():
    for host in ("0.0.0.0", "192.168.1.5", ""):
        try:
            server.serve(service.Context(tempfile.mkdtemp()), host, 0)
        except SystemExit as e:
            assert "no sign-in" in str(e)
        else:
            raise AssertionError(f"served on {host!r}")


def test_explicit_external_binding_is_an_opt_in():
    srv = server.serve(
        service.Context(tempfile.mkdtemp()),
        "0.0.0.0",
        0,
        allow_external=True,
    )
    try:
        assert srv.server_address[0] == "0.0.0.0"
    finally:
        srv.server_close()          # shutdown() would wait forever: serve_forever() was never started
