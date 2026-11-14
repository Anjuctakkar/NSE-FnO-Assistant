import datetime as dt
import json
import tempfile
from pathlib import Path

try:
    from .test_bhavcopy import as_zip, nse, udiff_csv
except ImportError:                                      # run as a plain script by tests/run_tests.py
    from test_bhavcopy import as_zip, nse, udiff_csv

from fno import service
from fno.bhavcopy import BhavcopyStore, write_canonical
from fno.config import MarketConfig
from fno.data import SyntheticProvider
from fno.demo_data import make_synthetic_bars

MKT = MarketConfig()


def strict(x):
    """The payload must survive a strict JSON encoder: no NaN, no Infinity, no raw dates."""
    return json.loads(json.dumps(x, allow_nan=False))


def seeded_ctx(symbol="TEST", days=130, seed=5):
    """A context whose bhavcopy store holds `days` real-format daily files built from a generated stock."""
    bars = make_synthetic_bars(400, seed=seed)
    prov = SyntheticProvider(symbol, bars, MKT)
    ctx = service.Context(tempfile.mkdtemp())
    for b in bars[-days:]:
        d, rows = b["date"], []
        for ex in prov.expiries(symbol, d, 3):
            c = prov.get_chain(symbol, d, ex)
            if not c:
                continue
            rows += [{"date": d, "symbol": symbol, "inst": "OPT", "expiry": ex, "strike": q.strike, "kind": q.kind, "price": q.ltp,
                      "oi": 1000, "volume": 100, "under": c.spot, "lot": 500} for q in c.quotes]
            rows.append({"date": d, "symbol": symbol, "inst": "FUT", "expiry": ex, "strike": None, "kind": "", "price": c.futures_price,
                         "oi": 9000, "volume": 500, "under": c.spot, "lot": 500})
        if rows:
            write_canonical(ctx.store.path_for(d), rows)
    return ctx, bars


def first_with_suggestion(ctx, **extra):
    for d in reversed(ctx.store.dates()[::3]):
        r = service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "asof": d.isoformat(), "min_score": 0, **extra})
        if r["suggestions"]:
            return r
    raise AssertionError("no suggestion on any stored day")


def test_suggest_from_bhavcopy_returns_a_complete_strict_json_payload():
    ctx, _ = seeded_ctx()
    r = strict(first_with_suggestion(ctx))
    assert r["source"] == "bhavcopy" and r["regime"]["trend"] in ("bullish", "bearish", "neutral")
    assert len(r["expiries"]) == 2
    assert any("settlement prices but no bid/ask" in n for n in r["data_notes"])
    assert any("underlying close recorded in the bhavcopy" in n for n in r["data_notes"])          # no cash bars supplied: say so
    s = r["suggestions"][0]
    assert s["kind"] in ("credit", "debit", "futures") and s["source"] == "bhavcopy" and s["reasons"] and s["exit_plan"]
    p = s["payoff"]
    assert len(p["price"]) == len(p["expiry"]) == len(p["now"]) > 50 and p["price"] == sorted(p["price"])
    assert len(s["legs_detail"]) == len(s["structure"]["legs"]) and all("delta" in l for l in s["legs_detail"])
    assert abs(sum(s["risk"]["score_parts"].values()) - s["score"]) < 0.2


def test_payoff_curve_matches_the_structure_and_now_converges_to_expiry_near_expiry():
    ctx, _ = seeded_ctx()
    s = first_with_suggestion(ctx)["suggestions"][0]
    st = service.Structure.from_dict(s["structure"])
    p = s["payoff"]
    for S, e in list(zip(p["price"], p["expiry"]))[::17]:
        assert abs(st.payoff(S) - e) < 0.01
    spot, e_at_spot = p["spot"], st.payoff(p["spot"])
    assert min(abs(x - spot) for x in p["price"]) < (p["price"][-1] - p["price"][0]) / 100            # spot is inside the plotted range
    assert isinstance(e_at_spot, float)


def test_asof_floors_to_available_data_and_errors_are_user_facing():
    ctx, _ = seeded_ctx()
    last = ctx.store.dates()[-1]
    r = service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "asof": (last + dt.timedelta(days=30)).isoformat(), "min_score": 0})
    assert r["asof"] == last.isoformat()
    for req, msg in (({"symbol": "NOPE", "source": "bhavcopy"}, "no stock option contracts"), ({"symbol": "", "source": "bhavcopy"}, "NSE symbol"),
                     ({"symbol": "TEST", "source": "carrier-pigeon"}, "source must be"), ({"symbol": "TEST", "asof": "1999-01-01"}, "No data on or before"),
                     ({"symbol": "TEST", "risk_budget": "lots"}, "must be a number")):
        try:
            service.run_suggest(ctx, req)
        except service.ServiceError as e:
            assert msg in str(e), (req, str(e))
        else:
            raise AssertionError(f"expected ServiceError for {req}")
    empty = service.Context(tempfile.mkdtemp())
    try:
        service.run_suggest(empty, {"symbol": "TEST"})
    except service.ServiceError as e:
        assert "Sync" in str(e) and "Import" in str(e)


def test_cash_bars_from_a_file_replace_the_close_only_fallback():
    ctx, bars = seeded_ctx()
    Path(ctx.bars_dir).mkdir(parents=True, exist_ok=True)
    with open(ctx.bars_dir / "TEST.csv", "w") as f:
        f.write("date,open,high,low,close\n" + "\n".join(f"{b['date']},{b['open']},{b['high']},{b['low']},{b['close']}" for b in bars))
    r = service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "min_score": 0})
    assert not any("underlying close recorded" in n for n in r["data_notes"])


def test_synthetic_source_and_the_risk_budget_reach_the_engine():
    ctx = service.Context(tempfile.mkdtemp())
    found = None
    for asof in ("2024-06-14", "2024-09-20", "2024-12-13", "2025-02-14", "2025-03-28"):
        r = service.run_suggest(ctx, {"symbol": "ALPHA", "source": "synthetic", "asof": asof, "min_score": 0, "risk_budget": 80000, "allow_futures": False})
        if r["suggestions"]:
            found = r
            break
    assert found and any("Synthetic data" in n for n in found["data_notes"])
    s = found["suggestions"][0]
    assert s["risk"]["lots"] >= 1 and (s["risk"]["max_loss"] is None or -s["risk"]["max_loss"] <= 80000 + 1e-6)
    assert found["config"]["risk_budget"] == 80000
    strict(found)


def test_backtest_payload_has_signal_random_edge_and_a_sorted_equity_curve():
    ctx = service.Context(tempfile.mkdtemp())
    r = strict(service.run_backtest_req(ctx, {"symbol": "DEMO", "source": "synthetic", "random_runs": 1}))
    m = r["signal"]["metrics"]
    assert m["trades"] > 3 and "attribution" in m and "by_strategy" in m
    assert r["random"]["runs"] == 1 and r["edge"]["verdict"]
    dates = [p["date"] for p in r["signal"]["equity"]]
    assert dates == sorted(dates) and abs(r["signal"]["equity"][-1]["pnl"] - m["total_net_pnl"]) < 0.5
    assert any("persistent trend regimes" in n for n in r["data_notes"]) and any("Daily closes only" in n for n in r["data_notes"])
    assert set(r["signal"]["trades"][0]) == set(service.TRADE_FIELDS)


def test_forward_evaluation_of_browser_held_suggestions():
    ctx, _ = seeded_ctx()
    older = ctx.store.dates()[:-25]
    r = None
    for d in reversed(older[::2]):
        r = service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "asof": d.isoformat(), "min_score": 0, "allow_futures": False})
        if r["suggestions"]:
            break
    assert r and r["suggestions"]
    entry = dict(r["suggestions"][0], taken=True)
    bad = dict(r["suggestions"][0], symbol="GHOST", id="ghost")
    out = strict(service.run_evaluate(ctx, {"entries": [entry, bad]}))
    good = next(x for x in out["rows"] if x["id"] == entry["id"])
    assert good["status"] in ("target", "stop", "invalidation", "time", "expiry", "open") and good["taken"] is True
    ghost = next(x for x in out["rows"] if x["id"] == "ghost")
    assert ghost["status"] == "no_data" and "GHOST" in ghost["error"] and out["no_data"] == 1       # one bad entry does not hide the rest
    for req in ({}, {"entries": "x"}, {"entries": [{}] * 301}):
        try:
            service.run_evaluate(ctx, req)
        except service.ServiceError:
            pass
        else:
            raise AssertionError("expected ServiceError")


def test_import_and_sync_update_status_and_report_problems():
    from test_bhavcopy import FakeNse
    ctx = service.Context(tempfile.mkdtemp())
    ex = dt.date(2026, 10, 27)
    d = dt.date(2026, 10, 1)
    out = service.import_bhavcopy(ctx, "fo.zip", as_zip("x.csv", udiff_csv(d, ex)))
    assert out["imported"]["date"] == "2026-10-01" and out["bhavcopy"]["days"] == 1 and out["bhavcopy"]["symbols"] == 1
    try:
        service.import_bhavcopy(ctx, "junk.csv", b"a,b\n1,2\n")
    except service.ServiceError as e:
        assert "junk.csv" in str(e) and "colmap" in str(e)
    n = nse()
    try:
        c2 = service.Context(tempfile.mkdtemp(), store=n.store())
        r = service.sync_bhavcopy(c2, {"start": "2026-10-01", "end": "2026-10-06"})
        assert r["counts"] == {"downloaded": 2, "no_file": 2} and not r["more_to_sync"] and r["bhavcopy"]["days"] == 2   # Oct 2 and Oct 6 have no file
        again = service.sync_bhavcopy(c2, {"end": "2026-10-06"})            # no start: resumes the day after the last stored one (Oct 5)
        assert again["range"] == ["2026-10-06", "2026-10-06"] and again["counts"] == {"no_file": 1}
        assert service.sync_bhavcopy(c2, {"start": "2026-10-09", "end": "2026-10-06"})["message"] == "Already up to date."
        full = FakeNse(nse_every_weekday(dt.date(2026, 1, 1), dt.date(2026, 3, 31)))
        try:
            big = service.sync_bhavcopy(service.Context(tempfile.mkdtemp(), store=full.store()), {"start": "2026-01-01", "end": "2026-10-06"})
            first_end = dt.date(2026, 1, 1) + dt.timedelta(days=service.MAX_SYNC_DAYS - 1)
            assert big["more_to_sync"] and big["range"][1] == first_end.isoformat()                 # one window per call
            assert big["next_start"] == (first_end + dt.timedelta(days=1)).isoformat()
        finally:
            full.close()
    finally:
        n.close()
    blocked = nse(block=True)
    try:
        r = service.sync_bhavcopy(service.Context(tempfile.mkdtemp(), store=blocked.store()), {"start": "2026-10-01", "end": "2026-10-06"})
        assert r["error"] and "import" in r["error"]
    finally:
        blocked.close()


def test_status_and_symbols():
    ctx, _ = seeded_ctx(days=70)
    st = strict(service.status(ctx))
    assert st["bhavcopy"]["days"] == 70 and st["bhavcopy"]["symbols"] == 1 and len(st["bhavcopy"]["dates"]) == 70
    assert service.list_symbols(ctx, "bhavcopy")["symbols"] == ["TEST"]
    assert "DEMO" in service.list_symbols(ctx, "synthetic")["symbols"]


def test_a_short_history_is_reported_as_a_data_problem_not_as_no_trade():
    """The case a user hit: sync fetched ~2 weeks, so there were 10 bars and the screen said 'staying out is valid'."""
    ctx, _ = seeded_ctx(days=10)
    r = strict(service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "min_score": 0}))
    assert r["suggestions"] == [] and r["code"] == "insufficient_history"
    i = r["issue"]
    assert i["code"] == "insufficient_history" and i["have"] == 10 and i["need"] == 60 and i["recommended"] == 250 and i["fix"] == "sync"
    assert "data problem" in i["detail"] and "10" in i["detail"] and "60" in i["detail"] and "TEST.csv" in i["detail"]
    assert "stay" not in (i["detail"] + i["title"]).lower().replace("this is a data problem, not a decision to stay out of the trade", "")
    st = service.status(ctx)["bhavcopy"]
    assert st["readiness"] == "too_short" and st["min_days"] == 60 and st["recommended_days"] == 250


def test_a_genuine_no_trade_is_not_flagged_as_a_data_problem():
    ctx, _ = seeded_ctx(days=130)
    r = service.run_suggest(ctx, {"symbol": "TEST", "source": "bhavcopy", "min_score": 100})
    assert r["suggestions"] == [] and r["code"] in ("no_edge", "filtered") and r["issue"] is None


def test_issue_for_a_symbol_with_no_chain_and_for_no_expiry():
    ctx, _ = seeded_ctx(days=130)
    from fno.selector import SuggestResult
    res = SuggestResult("TEST", dt.date(2026, 1, 1), None, None, code="no_chain", note="x")
    assert service.data_issue(ctx, res, "TEST", "bhavcopy", dt.date(2026, 1, 1))["code"] == "no_chain"
    res.code = "no_expiry"
    issue = service.data_issue(ctx, res, "TEST", "bhavcopy", dt.date(2026, 1, 1))
    assert issue["code"] == "no_expiry"
    assert "12 to 45 day" not in issue["detail"] and "upcoming option expiry" in issue["detail"]
    res.code = "no_edge"
    assert service.data_issue(ctx, res, "TEST", "bhavcopy", dt.date(2026, 1, 1)) is None


def test_readiness_levels_follow_the_amount_of_history():
    def ctx_with(n):
        ctx = service.Context(tempfile.mkdtemp())
        d = dt.date(2025, 1, 1)
        for i in range(n):
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            write_canonical(ctx.store.path_for(d), [{"date": d, "symbol": "X", "inst": "OPT", "expiry": d + dt.timedelta(days=20), "strike": 100.0,
                                                    "kind": "CE", "price": 1.0, "oi": 1, "volume": 1, "under": 100.0, "lot": 1}])
            d += dt.timedelta(days=1)
        return ctx
    got = {n: service.status(ctx_with(n))["bhavcopy"]["readiness"] for n in (0, 59, 60, 199, 200)}
    assert got == {0: "empty", 59: "too_short", 60: "thin", 199: "thin", 200: "good"}, got


def nse_every_weekday(first, last, holidays=()):
    """A fake NSE with a file for every weekday in [first, last] except the given holidays."""
    days, d = {}, first
    while d <= last:
        if d.weekday() < 5 and d not in holidays:
            days[d.strftime("%Y%m%d")] = as_zip("x.csv", udiff_csv(d, d + dt.timedelta(days=20)))
        d += dt.timedelta(days=1)
    return days


def test_history_sync_walks_the_whole_range_in_resumable_windows():
    from test_bhavcopy import FakeNse
    hol = {dt.date(2026, 8, 14), dt.date(2026, 9, 14)}              # a Friday and a Monday (a Saturday holiday would never be requested)
    n = FakeNse(nse_every_weekday(dt.date(2026, 6, 1), dt.date(2026, 10, 6), hol))
    try:
        ctx = service.Context(tempfile.mkdtemp(), store=n.store())
        end, body, windows, downloaded, holidays = "2026-10-06", {"history_days": 120, "end": "2026-10-06"}, [], 0, 0
        for _ in range(20):
            r = service.sync_bhavcopy(ctx, body)
            windows.append(r["range"])
            downloaded += r["counts"].get("downloaded", 0)
            holidays += r["counts"].get("no_file", 0)
            assert not r["suspect_missing"]
            if not r["more_to_sync"]:
                break
            body = {"start": r["next_start"], "end": end}
        assert windows[0][0] == (dt.date(2026, 10, 6) - dt.timedelta(days=120)).isoformat() and windows[-1][1] == end
        for a, b in zip(windows, windows[1:]):                          # contiguous: no day skipped, none repeated
            assert dt.date.fromisoformat(b[0]) == dt.date.fromisoformat(a[1]) + dt.timedelta(days=1)
        assert all((dt.date.fromisoformat(w[1]) - dt.date.fromisoformat(w[0])).days + 1 <= service.MAX_SYNC_DAYS for w in windows) and len(windows) >= 5
        weekdays = sum(1 for i in range(121) if (dt.date(2026, 6, 8) + dt.timedelta(days=i)).weekday() < 5 and dt.date(2026, 6, 8) + dt.timedelta(days=i) <= dt.date(2026, 10, 6))
        assert downloaded == ctx.store.coverage()["days"] and holidays == 2 and downloaded >= 80, (downloaded, holidays, weekdays)
    finally:
        n.close()


def test_a_window_with_no_files_at_all_is_flagged_and_stops_the_loop():
    """If the download address is wrong, every day is 'no file'. That must not be reported as holidays or repeated for a year."""
    from test_bhavcopy import FakeNse
    n = FakeNse({})                                                         # NSE serves nothing at this address
    try:
        r = service.sync_bhavcopy(service.Context(tempfile.mkdtemp(), store=n.store()), {"history_days": 365, "end": "2026-10-06"})
        assert r["suspect_missing"] is True and r["more_to_sync"] is False and r["next_start"] is None
        assert r["counts"] == {"no_file": 15} or r["counts"].get("no_file", 0) >= 8
        assert len(n.hits) <= 15                                            # one window of requests, not a year's worth
    finally:
        n.close()


def test_a_blocked_sync_stops_the_loop_instead_of_returning_more_to_sync():
    b = nse(block=True)
    try:
        r = service.sync_bhavcopy(service.Context(tempfile.mkdtemp(), store=b.store()), {"history_days": 200, "end": "2026-10-06"})
        assert r["error"] and r["more_to_sync"] is False and r["next_start"] is None
    finally:
        b.close()


def test_backtest_with_too_little_history_explains_instead_of_reporting_no_trades():
    ctx, _ = seeded_ctx(days=30)
    r = strict(service.run_backtest_req(ctx, {"symbol": "TEST", "source": "bhavcopy"}))
    assert r["issue"]["code"] == "insufficient_history" and r["issue"]["have"] == 30 and r["issue"]["need"] == 80 and "signal" not in r
