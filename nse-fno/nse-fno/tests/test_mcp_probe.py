import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fno.mcp_probe import list_tools, probe

TOOLS = [{"name": "get_stock_history", "description": "Daily OHLC for a cash stock.", "inputSchema": {"type": "object", "properties": {"symbol": {}}, "required": ["symbol"]}},
         {"name": "get_fo_bhavcopy", "description": "Derivatives bhavcopy for a date: futures and options contracts.", "inputSchema": {"type": "object", "properties": {"date": {}}}}]


def serve(sse: bool, need_session: bool = True):
    seen = {"methods": [], "sessions": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["methods"].append(body["method"])
            seen["sessions"].append(self.headers.get("Mcp-Session-Id"))
            if "id" not in body:                                   # a notification: no reply body
                self.send_response(202)
                self.end_headers()
                return
            result = {"protocolVersion": "2025-03-26", "capabilities": {}} if body["method"] == "initialize" else {"tools": TOOLS}
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            self.send_response(200)
            self.send_header("Mcp-Session-Id", "sess-1")
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.end_headers()
            self.wfile.write((f"event: message\ndata: {payload}\n\n" if sse else payload).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_probe_lists_tools_over_json_and_sse_and_keeps_the_session():
    for sse in (False, True):
        srv, seen = serve(sse)
        try:
            tools = list_tools(f"http://127.0.0.1:{srv.server_address[1]}/mcp")
            assert [t["name"] for t in tools] == ["get_stock_history", "get_fo_bhavcopy"]
            assert tools[0]["required"] == ["symbol"] and tools[1]["properties"] == ["date"]
            assert seen["methods"] == ["initialize", "notifications/initialized", "tools/list"]
            assert seen["sessions"][1:] == ["sess-1", "sess-1"]          # the session id from initialize is reused
        finally:
            srv.shutdown()


def test_probe_flags_derivatives_tools_and_reports_failures_instead_of_raising():
    srv, _ = serve(False)
    try:
        r = probe(f"http://127.0.0.1:{srv.server_address[1]}/mcp")
        assert r["ok"] and r["derivatives_like"] == ["get_fo_bhavcopy"]
    finally:
        srv.shutdown()
    bad = probe("http://127.0.0.1:9/mcp", timeout=1.0)
    assert not bad["ok"] and bad["error"] and bad["tools"] == []
