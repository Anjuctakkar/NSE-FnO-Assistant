import shutil
import subprocess
from pathlib import Path


def test_ui_helpers_in_node():
    """The UI's pure functions (escaping, rupee formatting, chart ticks, plain-English Greeks). Skipped without node."""
    node = shutil.which("node")
    if not node:
        print("   (node not installed: UI helper tests skipped)")
        return
    r = subprocess.run([node, str(Path(__file__).parent / "ui_helpers.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_ui_script_has_valid_syntax_and_no_cdn_dependencies():
    js = (Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parent.parent / "fno" / "static" / "index.html").read_text(encoding="utf-8")
    for text in (js, html):
        assert "http://" not in text.replace("http://127.0.0.1", "") and "https://" not in text, "no external resources: the dashboard's CSP forbids them"
    assert "innerHTML" in js and "esc(" in js                    # dynamic text is escaped before it is inserted
    assert "end-of-day settlement data, not live or intraday quotes" in js
    assert "Latest bhavcopy settlement date" in js
    assert 'id="fnoSyncLatest"' in js and 'doSync({})' in js
    assert 'id="fnoBreezeSessionToken"' in js and 'type="password"' in js
    assert 'id="fnoLiveRefresh"' in js and 'post("/live/quote"' in js
    assert "ICICI Breeze quote is fetched on demand" in js
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
