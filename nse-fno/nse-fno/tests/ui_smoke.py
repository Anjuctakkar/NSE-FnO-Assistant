"""
Browser smoke test of the real UI: python tests/ui_smoke.py [--shots DIR]
Needs:  pip install playwright && playwright install chromium     (not part of run_tests.py)

It seeds a throwaway store with GENERATED prices (labelled TEST), starts the server, and drives the page like a trader would:
suggest, read the card, hover the chart, add to the log, backtest, replay the log, export, reload and find the log still there.
It fails on any console error, page error or failed request.
"""
import re
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402
from test_service import seeded_ctx  # noqa: E402
from test_scanner import build_market  # noqa: E402
from test_bhavcopy import FakeNse, as_zip, udiff_csv  # noqa: E402
import datetime as dt  # noqa: E402

from fno import server  # noqa: E402


def main() -> int:
    from playwright.sync_api import sync_playwright
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    ctx, _ = seeded_ctx()
    mctx = build_market(6)
    msrv = server.serve(mctx, "127.0.0.1", 0)
    threading.Thread(target=msrv.serve_forever, daemon=True).start()
    srv = server.serve(ctx, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base, problems, ok = f"http://127.0.0.1:{srv.server_address[1]}", [], []

    def check(cond, what):
        (ok if cond else problems).append(what)
        print(("  ok   " if cond else "  FAIL ") + what)

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1360, "height": 900}, accept_downloads=True)
        pg.on("console", lambda m: problems.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))
        pg.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        # ---- the whole-market scan: no stock is typed anywhere ----
        print("market scan scenario")
        pm = b.new_page(viewport={"width": 1360, "height": 900})
        pm.on("console", lambda m: problems.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
        pm.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))
        pm.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pm.goto(f"http://127.0.0.1:{msrv.server_address[1]}")
        pm.wait_for_selector("#fnoStatus")                 # the UI mounts after a sign-in probe: wait for it to exist
        pm.wait_for_function("document.querySelector('#fnoStatus').textContent.includes('days')")
        pm_steps = pm.locator(".steps .step").count()
        check(pm.inner_text("#fnoRunSug") == "Find trades" and not pm.locator("#fnoSym").is_visible() and pm.locator("#fnoModeMkt.on").count() == 1, "it opens on 'Whole market' with no stock box and a 'Find trades' button")
        pm.click("details.adv summary")
        pm.fill("#fnoMin", "0")
        pm.click("#fnoRunSug")
        pm.wait_for_function("document.querySelector('#fnoPanSug .issue')", timeout=60000)
        check("One-time index needed" in pm.inner_text("#fnoPanSug") and pm.locator('[data-fix="index"]').count() == 1, "the first scan asks for the one-time index instead of failing")
        pm.click('[data-fix="index"]')
        pm.wait_for_function("document.querySelectorAll('#fnoPanSug .sug').length > 0 || (document.querySelector('#fnoPanSug h3') && document.querySelector('#fnoPanSug h3').textContent.includes('No trade'))", timeout=240000)
        check(pm.locator("#fnoPanSug .sug").count() > 0, "after indexing, the scan returns ideas without a stock being entered")
        check(pm.locator("#fnoPanSug .strip .tile").count() == 5, "a market overview (stocks, trend mix, IV rank, put/call, build-ups) sits above the ideas")
        n_all = pm.locator("#fnoPanSug .sug").count()
        scores = [int(x) for x in pm.eval_on_selector_all("#fnoPanSug .sug .score", "e => e.map(x => x.textContent.split('/')[0].trim())")]
        syms = pm.eval_on_selector_all("#fnoPanSug .sug .sym", "e => e.map(x => x.textContent)")
        check(scores == sorted(scores, reverse=True) and len(set(syms)) == len(syms) and not ({"SPLT", "NEWB", "DEAD"} & set(syms)), f"ideas are best-first, one per stock, and the bad stocks are absent ({syms})")
        check(pm.locator("#fnoPanSug .sug .oichips .oichip").count() >= n_all, "every idea shows its open-interest evidence")
        check("were skipped" in pm.inner_text("#fnoPanSug") or pm.locator("#fnoPanSug details summary").count() > 0, "skipped stocks are listed, not hidden")
        check(pm.locator("#fnoPanSug .sug .sug-more:not([hidden])").count() == 0, "ideas start collapsed so the list is scannable")
        pm.locator("#fnoPanSug .sug [data-act='toggle']").first.click()
        check(pm.locator("#fnoPanSug .sug .sug-more:not([hidden]) .chart").count() == 1 and pm.locator("#fnoPanSug .sug .sug-more:not([hidden]) .chip").count() == 3, "opening an idea shows its chart and exit plan")
        pm.locator("#fnoPanSug .sug summary").first.click()
        check("Put/call OI ratio" in pm.inner_text("#fnoPanSug") and "Open interest" in pm.inner_text("#fnoPanSug"), "'Why this trade' includes an open-interest section")
        pm.click('[data-fb="bullish"]')
        shown = pm.locator("#fnoPanSug .sug").count()
        heads = pm.eval_on_selector_all("#fnoPanSug .sug .sug-head", "e => e.map(h => [...h.querySelectorAll('.badge')].map(b => b.textContent))")
        check(0 < shown < n_all and all("bullish" in h for h in heads) and pm.locator('[data-fb="bullish"].on').count() == 1,
              f"the Bullish chip keeps only bullish ideas ({shown} of {n_all}, badges {heads})")
        pm.click('[data-fb="all"]')
        check(pm.locator("#fnoPanSug .sug").count() == n_all, "'All' restores the list")
        if shots:
            pm.screenshot(path=str(shots / "scan.png"), full_page=True)
        first_sym = pm.locator("#fnoPanSug .sug .sym").first.inner_text()
        first = pm.locator("#fnoPanSug .sug").first
        if first.locator(".sug-more[hidden]").count():                # open it only if it is closed
            first.locator("[data-act='toggle']").click()
        first.locator("[data-act='stock']").click()
        pm.wait_for_function("!document.querySelector('#fnoRunSug').disabled && document.querySelector('#fnoPanSug .strip')", timeout=60000)
        check(pm.input_value("#fnoSym") == first_sym and pm.locator("#fnoModeOne.on").count() == 1 and "Open interest" in pm.inner_text("#fnoPanSug .strip"), f"'Look at {first_sym} on its own' switches to that stock, with its open-interest tile")
        pm.close()
        msrv.shutdown()

        pg.goto(base)
        pg.wait_for_selector("#fnoStatus")                 # the UI mounts after a sign-in probe: wait for it to exist
        pg.wait_for_function("document.querySelector('#fnoStatus').textContent.includes('days')")
        pg.click("#fnoModeOne")                                   # the single-stock flows below start from 'One stock'
        check(pg.locator("#fnoSym").is_visible() and pg.inner_text("#fnoRunSug") == "Suggest trades", "'One stock' reveals the symbol box")
        check("130 days stored" in pg.inner_text("#fnoStatus") and "about a year" in pg.inner_text("#fnoStatus"), "130 days is flagged as usable but thin for IV rank")
        check(pm_steps == 3, "the first screen shows the three-step guide")
        pg.fill("#fnoSym", "TEST")
        check(not pg.locator("#fnoMin").is_visible(), "advanced settings start tucked away")
        pg.click("details.adv summary")
        pg.fill("#fnoMin", "0")                                   # accept any score so a suggestion is found quickly
        found = False
        for d in pg.eval_on_selector_all("#fnoAsof option", "o => o.map(x => x.value)")[1:70:2]:
            pg.select_option("#fnoAsof", d)
            pg.click("#fnoRunSug")
            pg.wait_for_function("!document.querySelector('#fnoRunSug').disabled", timeout=60000)
            if pg.locator(".sug").count():
                found = True
                break
        check(found, "a day with a suggestion is found")
        card = pg.locator(".sug").first
        check(re.search(r"Risk ₹[\d,]+ to (make up to ₹[\d,]+|profit is open-ended)", card.locator(".verdict .big").inner_text()) is not None
              or "Plan to make" in card.locator(".verdict .big").inner_text(), "the card leads with what you can lose and make")
        check(card.locator(".chips .chip").count() == 3, "three exit chips: take profit, cut loss, latest exit")
        check(not card.locator("details[open]").count(), "the Greeks and reasoning start collapsed")
        card.locator("details summary").click()
        check(card.locator(".greeks, .grid4").count() > 0 and card.locator("ul.why li").count() >= 3, "opening 'Why this trade' shows Greeks, score and reasoning")
        svg = card.locator(".chart svg").first.bounding_box()
        pg.mouse.move(svg["x"] + svg["width"] * 0.5, svg["y"] + svg["height"] * 0.5)
        check(pg.locator(".chart .tip").first.is_visible(), "hovering the payoff chart shows the P&L at that price")
        if shots:
            card.screenshot(path=str(shots / "suggestion.png"))
        card.locator('[data-act="log"]').click()
        check(pg.inner_text("#fnoLogN") == "1", "adding to the log updates the counter")

        pg.click("#fnoRunBt")
        pg.wait_for_function("!document.querySelector('#fnoRunBt').disabled", timeout=180000)
        check(pg.locator("#fnoPanBt .cards .tile").count() == 8 and pg.locator("#fnoPanBt .chart").count() >= 1, "backtest shows its summary tiles and the P&L curve")
        check(pg.locator("#fnoPanBt .note").count() >= 2, "backtest shows its caveats")
        if shots:
            pg.screenshot(path=str(shots / "backtest.png"), full_page=True)

        pg.click('.fno-tabs button[data-t="log"]')
        pg.click("#fnoLogRefresh")
        pg.wait_for_function("document.querySelector('#fnoLogMsg') && document.querySelector('#fnoLogMsg').textContent.includes('Updated')", timeout=60000)
        row = pg.locator("#fnoPanLog tr:has([data-rm])")
        status = row.locator(".badge").inner_text() if row.locator(".badge").count() else ""
        check(row.count() == 1 and status in ("Open", "Target hit", "Stopped", "Invalidated", "Time exit", "Expired"),
              f"refreshing outcomes gives the logged trade a real status (got {status!r}) and keeps the message")
        check(pg.locator("#fnoPanLog .panel h3").first.inner_text() == "How your suggestions played out", "a summary of how suggestions played out appears")
        with pg.expect_download() as dl:
            pg.click("#fnoLogExport")
        check(dl.value.suggested_filename.startswith("fno-log-"), "export downloads the log")
        pg.reload()
        pg.wait_for_selector("#fnoLogN")
        check(pg.inner_text("#fnoLogN") == "1", "the log survives a reload (kept in this browser)")
        pg.click('.fno-tabs button[data-t="log"]')
        pg.click("[data-rm]")
        check(pg.inner_text("#fnoLogN") == "0", "removing a trade empties the log")

        pg.select_option("#fnoSource", "synthetic")
        pg.wait_for_function("document.querySelector('#fnoSym').value === 'DEMO' || document.querySelector('#fnoSym').value === 'TEST'")
        pg.click("#fnoModeMkt")                                    # (a reload resets the page to 'Whole market')
        pg.click("#fnoRunSug")
        pg.wait_for_function("!document.querySelector('#fnoRunSug').disabled && document.querySelector('#fnoPanSug .note.bad')", timeout=120000)
        check("no open-interest data" in pg.inner_text("#fnoPanSug") and pg.locator("#fnoPanSug .sug").count() > 0, "the demo scan works with no stock and says it has no open-interest data")
        pg.click("#fnoModeOne")
        pg.fill("#fnoSym", "DEMO")
        pg.click("#fnoRunSug")
        pg.wait_for_function("!document.querySelector('#fnoRunSug').disabled", timeout=60000)
        check(pg.locator("#fnoPanSug .note.bad").count() >= 1, "synthetic data carries a loud 'not real' warning")

        # ---- the case a real user hit: a sync that only fetched ~2 weeks, so there are too few days to analyse ----
        print("\nshort-history scenario")
        short_ctx, _ = seeded_ctx(days=10)
        today = dt.date.today()
        weekdays = [today - dt.timedelta(days=i) for i in range(1, 9) if (today - dt.timedelta(days=i)).weekday() < 5][:5]
        fake = FakeNse({d.strftime("%Y%m%d"): as_zip("x.csv", udiff_csv(d, d + dt.timedelta(days=20))) for d in weekdays})
        fs = fake.store(short_ctx.data_dir)
        short_ctx.store.url_templates, short_ctx.store.warmup_url, short_ctx.store.pause, short_ctx.store.retries = fs.url_templates, fs.warmup_url, 0, 0
        srv2 = server.serve(short_ctx, "127.0.0.1", 0)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        pg2 = b.new_page(viewport={"width": 1360, "height": 900})
        pg2.on("console", lambda m: problems.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
        pg2.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))
        pg2.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg2.goto(f"http://127.0.0.1:{srv2.server_address[1]}")
        pg2.wait_for_selector("#fnoStatus")                 # the UI mounts after a sign-in probe: wait for it to exist
        pg2.wait_for_function("document.querySelector('#fnoStatus').textContent.includes('days')")
        check("Only 10 days stored" in pg2.inner_text("#fnoStatus") and "at least 60" in pg2.inner_text("#fnoStatus"), "the sidebar warns about short history BEFORE you click anything")
        pg2.click("#fnoRunSug")                                   # default mode is the market scan
        pg2.wait_for_function("document.querySelector('#fnoPanSug .issue')", timeout=60000)
        check("Not enough price history to scan the market" in pg2.inner_text("#fnoPanSug") and "10 of the 60" in pg2.inner_text("#fnoPanSug"), "the market scan also reports short history as a data problem")
        pg2.click("#fnoModeOne")
        pg2.fill("#fnoSym", "TEST")
        pg2.click("#fnoRunSug")
        pg2.wait_for_function("!document.querySelector('#fnoRunSug').disabled", timeout=60000)
        txt = pg2.inner_text("#fnoPanSug")
        check(pg2.locator(".issue").count() == 1 and "Not enough price history for TEST" in txt and "10 of the 60" in txt, "Suggest shows a data-problem panel with the numbers")
        check("valid output" not in txt and "No trade" not in txt and pg2.locator(".sug").count() == 0, "it does not call missing data 'no trade' or say staying out is valid")
        if shots:
            pg2.screenshot(path=str(shots / "short_history.png"))
        # A) NSE has files for only 5 recent days, so older windows are empty: that looks like a wrong address, and it must stop
        pg2.click('[data-fix="sync"]')
        pg2.wait_for_function("document.querySelector('#fnoDataMsg').textContent.includes('no file for any of') && document.querySelector('#fnoStop').hidden", timeout=120000)
        msg = pg2.inner_text("#fnoDataMsg")
        check("Import file" in msg and "more than" not in msg.replace("Holidays explain only a few days a year", "") and len(fake.hits) < 40,
              f"a window with no files is called out (not blamed on holidays) and the loop stops early after {len(fake.hits)} requests")
        pg2.close()
        fake.close()
        srv2.shutdown()

        # B) a healthy NSE: every weekday of the last ~110 days exists, so the loop runs window after window and the warning clears
        from test_service import nse_every_weekday
        ctx3, _ = seeded_ctx(days=10)
        fake3 = FakeNse(nse_every_weekday(today - dt.timedelta(days=110), today))
        fs3 = fake3.store(ctx3.data_dir)
        ctx3.store.url_templates, ctx3.store.warmup_url, ctx3.store.pause, ctx3.store.retries = fs3.url_templates, fs3.warmup_url, 0, 0
        srv3 = server.serve(ctx3, "127.0.0.1", 0)
        threading.Thread(target=srv3.serve_forever, daemon=True).start()
        pg3 = b.new_page(viewport={"width": 1360, "height": 900})
        pg3.on("console", lambda m: problems.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
        pg3.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))
        pg3.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg3.goto(f"http://127.0.0.1:{srv3.server_address[1]}")
        pg3.wait_for_selector("#fnoStatus")                 # the UI mounts after a sign-in probe: wait for it to exist
        pg3.wait_for_function("document.querySelector('#fnoStatus').textContent.includes('Only 10 days stored')")
        pg3.select_option("#fnoHist", "90")
        pg3.click("#fnoSync")
        pg3.wait_for_function("document.querySelector('#fnoDataMsg').textContent.match(/Downloaded \\d+ new days/) && document.querySelector('#fnoStop').hidden", timeout=180000)
        msg = pg3.inner_text("#fnoDataMsg")
        check(re.search(r"Downloaded (\d+) new days", msg) is not None and int(re.search(r"Downloaded (\d+)", msg).group(1)) >= 55, f"the progress loop downloaded a quarter of history ({msg!r})")
        check("Only" not in pg3.inner_text("#fnoStatus"), "the 'too short' warning clears once there is enough history")
        pg3.click("#fnoModeOne")
        pg3.fill("#fnoSym", "TEST")
        pg3.click("#fnoRunSug")
        pg3.wait_for_function("!document.querySelector('#fnoRunSug').disabled", timeout=60000)
        check(pg3.locator(".issue").count() == 0, "with enough history Suggest gives a real answer, not a data problem")
        pg3.close()
        fake3.close()
        srv3.shutdown()
        b.close()
    srv.shutdown()
    print(f"\n{len(ok)} checks passed, {len(problems) - sum(1 for x in problems if x.startswith(('  ',)))} problem(s)")
    real = [x for x in problems if not x.startswith("  ")]
    for x in real:
        print("  PROBLEM:", x)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
