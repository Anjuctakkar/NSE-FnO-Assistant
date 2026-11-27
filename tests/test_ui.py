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
    assert 'id="fnoBreezeApiKey"' in js and 'id="fnoBreezeApiSecret"' in js and 'id="fnoBreezeSessionToken"' in js
    assert 'id="fnoBreezeTest"' in js and 'post("/breeze/test"' in js
    assert 'id="fnoLiveRefresh"' in js and 'post("/live/quote"' in js
    assert "ICICI Breeze quote is fetched on demand" in js
    assert 'id="fnoAppVersion"' in js and 'textContent = appVersion ? `App version ${appVersion}`' in js
    assert "loaded NSE option data" in js and "no loaded NSE contracts" in js
    assert 'class="fno-toolbar"' in js
    assert 'src="/laxmi-logo-dark.png" alt="Laxmi Edge with AI"' in html
    assert 'id="fnoChatToggle"' in js and 'id="fnoChatPanel"' in js
    assert 'id="fnoChatMode"' in js and "App data only · no model" in js and "AI model" in js
    assert 'id="fnoChatProvider"' in js and 'id="fnoChatApiKey"' in js
    assert "function answerAppChat" in js and 'post("/chat"' in js
    assert 'localStorage.setItem("fnoChatApiKey"' not in js
    assert 'value="breeze-live"' in js and "Breeze-live evaluates one stock" in js
    assert "strategy inputs remain bhavcopy-based." not in js
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_ui_has_the_live_market_scan_and_win_probability_controls():
    js = (Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js").read_text(encoding="utf-8")
    assert 'id="fnoLiveN"' in js and 'post("/live-scan/start"' in js and "/live-scan/status?id=" in js and 'post("/live-scan/cancel"' in js
    assert "async function runLiveScan" in js and "function liveStrip" in js and "function winBlock" in js
    assert "win probability" in js and '"win", "regime"' in js           # shown on the card and kept in the user's log
    assert "Breeze-live supports one stock at a time" not in js


def test_ui_has_the_hybrid_controls_refresh_and_auto_refresh():
    js = (Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js").read_text(encoding="utf-8")
    assert "Hybrid: live Breeze + bhavcopy EOD" in js and "(5-minute)</option>" not in js
    assert "async function liveJob" in js and "async function refreshLive" in js and "function autoTick" in js and "function setAuto" in js
    assert 'id="fnoRefreshLive"' in js and 'id="fnoAutoLive"' in js and "market_session" in js and "refresh: true" in js
    assert "live_chg_pct" in js and "re-ranked on today's moves" in js


def test_ui_shows_the_version_with_its_release_date():
    js = (Path(__file__).resolve().parent.parent / "fno" / "static" / "fno.js").read_text(encoding="utf-8")
    assert "app_version_date" in js and "S.status.app_version +" in js
