"""
Ask an MCP server what it offers, using only the standard library (streamable-HTTP transport, JSON or SSE replies,
the same handshake the dashboard's server.py uses).

    python -m fno probe https://mcp.nseindia.in/bhavcopy/fo/mcp

I could not confirm that NSE's MCP service has an F&O bhavcopy endpoint, or what its tools are called, so nothing in
this package assumes it. Run the probe against a candidate URL: it lists the tools and flags the ones that look
derivatives-related, so you can decide whether to wire one in (the BhavcopyStore download path works without it).
"""
from __future__ import annotations

import itertools
import json
import re
import urllib.request
from typing import Any, Optional

_ids = itertools.count(1)
WORD_HINTS = {"fo", "foo", "oi", "fno", "f&o"}                                     # short: only as whole words ("for" must not match "fo")
SUBSTR_HINTS = ("deriv", "option", "future", "bhav", "expiry", "strike", "open_interest", "openinterest", "chain")


def _post(url: str, method: str, params: dict[str, Any], session: Optional[str], timeout: float, notify: bool = False):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notify:
        body["id"] = next(_ids)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id", session)
        raw = r.read().decode("utf-8", errors="replace")
        ctype = r.headers.get("content-type", "")
    if notify or not raw.strip():
        return None, sid
    data = None
    if "text/event-stream" in ctype:
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:") and line[5:].strip():
                try:
                    data = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
    else:
        data = json.loads(raw)
    if data is None:
        raise RuntimeError(f"could not parse the reply to {method}")
    if "error" in data:
        raise RuntimeError(f"MCP error {data['error'].get('code')}: {data['error'].get('message')}")
    return data.get("result"), sid


def list_tools(url: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Handshake, then tools/list. Returns [{name, description, required, properties}]."""
    _, sid = _post(url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "fno-probe", "version": "0.1"}}, None, timeout)
    _post(url, "notifications/initialized", {}, sid, timeout, notify=True)
    result, _ = _post(url, "tools/list", {}, sid, timeout)
    out = []
    for t in (result or {}).get("tools", []):
        schema = t.get("inputSchema") or {}
        out.append({"name": t.get("name", ""), "description": (t.get("description") or "").strip(),
                    "required": schema.get("required", []), "properties": sorted((schema.get("properties") or {}).keys())})
    return out


def looks_like_derivatives(tool: dict[str, Any]) -> bool:
    text = (tool["name"] + " " + tool["description"]).lower()
    words = set(re.findall(r"[a-z0-9&]+", text))
    return bool(words & WORD_HINTS) or any(h in text for h in SUBSTR_HINTS)


def probe(url: str, timeout: float = 20.0) -> dict[str, Any]:
    try:
        tools = list_tools(url, timeout)
    except Exception as e:                                    # noqa: BLE001
        return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}", "tools": [], "derivatives_like": []}
    return {"url": url, "ok": True, "error": None, "tools": tools, "derivatives_like": [t["name"] for t in tools if looks_like_derivatives(t)]}
