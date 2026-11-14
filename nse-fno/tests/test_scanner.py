import asyncio
import datetime as dt
import json
import math
import random
import tempfile

try:
    from .test_server import call, start
except ImportError:
    from test_server import call, start

from fno import service
from fno.api import Backend
from fno.bhavcopy import write_canonical
from fno.config import MarketConfig, StrategyConfig
from fno.data import SyntheticProvider
from fno.demo_data import make_synthetic_bars
from fno.scanner import ScanConfig, market_context, pass_one, shortlist_score
from fno.summary import SummaryIndex

MKT = MarketConfig()
_CACHE: dict = {}


def build_market(symbols, days=140, newb_days=30, tag=""):
    """A throwaway store of several generated stocks, plus three deliberately bad ones the scan must skip and count."""
    ctx = service.Context(tempfile.mkdtemp())
    by_date: dict = {}
    def add(sym, bars, lot, dead=False, only_last=None):
        prov = SyntheticProvider(sym, bars, MKT)
        rng = random.Random(sym)
        use = bars[-only_last:] if only_last else bars[-days:]
        for i, b in enumerate(use):
            d = b["date"]
            for ex in prov.expiries(sym, d, 3):
                c = prov.get_chain(sym, d, ex)
                if not c:
                    continue
                up = min((q.strike for q in c.quotes if q.kind == "CE"), key=lambda k: abs(k - c.spot * 1.05))
                dn = min((q.strike for q in c.quotes if q.kind == "PE"), key=lambda k: abs(k - c.spot * 0.95))
                for q in c.quotes:
                    wall = (q.kind == "CE" and q.strike == up) or (q.kind == "PE" and q.strike == dn)
                    oi = 0 if dead else (30000 if wall else int(rng.uniform(100, 3000)))
                    by_date.setdefault(d, []).append({"date": d, "symbol": sym, "inst": "OPT", "expiry": ex, "strike": q.strike, "kind": q.kind, "price": q.ltp,
                                                      "oi": oi, "volume": 0 if dead else int(rng.uniform(1, 800)), "under": c.spot, "lot": lot})
                by_date[d].append({"date": d, "symbol": sym, "inst": "FUT", "expiry": ex, "strike": None, "kind": "", "price": c.futures_price,
                                   "oi": 0 if dead else int(100000 * (1 + 0.003 * i) * (1.08 if i == len(use) - 1 and sym in ("AAA", "BBB") else 1)), "volume": 50, "under": c.spot, "lot": lot})
    good = {"AAA": (11, 175, 3000.0), "BBB": (12, 400, 1500.0), "CCC": (13, 500, 800.0), "DDD": (14, 250, 2000.0), "EEE": (15, 100, 600.0), "FFF": (16, 300, 1200.0)}
    for sym in list(good)[:symbols]:
        seed, lot, s0 = good[sym]
        add(sym, make_synthetic_bars(400, seed=seed, s0=s0), lot)
    if symbols >= 6:
        raw = make_synthetic_bars(400, seed=21, s0=1000.0)
        cut = len(raw) - 40                                           # a 2-for-1 split 40 days ago: prices before it are double
        split = [dict(b, **{k: round(b[k] * 2, 2) for k in ("open", "high", "low", "close")}) if i < cut else b for i, b in enumerate(raw)]
        add("SPLT", split, 200)
        add("NEWB", make_synthetic_bars(400, seed=22, s0=900.0), 150, only_last=newb_days)
        add("DEAD", make_synthetic_bars(400, seed=23, s0=700.0), 100, dead=True)
    for d, rows in by_date.items():
        write_canonical(ctx.store.path_for(d), rows)
    return ctx


def market():
    if "m" not in _CACHE:
        ctx = build_market(6)
        SummaryIndex(ctx.store, MKT).build()
        _CACHE["m"] = ctx
    return _CACHE["m"]


def strict(x):
    return json.loads(json.dumps(x, allow_nan=False))


def test_scan_needs_no_stock_and_returns_ranked_explained_ideas():
    r = strict(service.run_scan(market(), {"source": "bhavcopy", "min_score": 0}))
    assert r["issue"] is None and r["mode"] == "scan" and "symbol" not in r
    assert r["universe"] == 9 and r["eligible"] == 6
    assert r["skipped"] == {"probable split or bonus": 1, "less than 60 days of history": 1, "no traded options": 1}      # each guard fired once
    ideas = r["ideas"]
    assert ideas and [i["scan"]["rank"] for i in ideas] == list(range(1, len(ideas) + 1))
    assert [i["score"] for i in ideas] == sorted((i["score"] for i in ideas), reverse=True)
    assert len({i["symbol"] for i in ideas}) == len(ideas)                                # one idea per stock
    assert not {"SPLT", "NEWB", "DEAD"} & {i["symbol"] for i in ideas}
    for i in ideas:
        assert i["risk"]["oi"] is not None and i["risk"]["oi"]["pcr"] is not None and "oi" in i["risk"]["score_parts"]       # OI really fed the score
        assert i["scan"]["bias"] in ("bullish", "bearish", "neutral") and i["legs_detail"] and i["payoff"]["price"] and i["exit_plan"]
        assert abs(sum(i["risk"]["score_parts"].values()) - i["score"]) < 0.2 or i["score"] >= 99.9
    m = r["market"]
    assert m["stocks"] == 6 and m["bullish"] + m["bearish"] + m["neutral"] == 6 and 0 <= m["above_sma20_pct"] <= 100
    assert any("close-to-close" in n for n in r["data_notes"])


def test_the_scan_is_deterministic_and_reads_the_requested_day():
    a = service.run_scan(market(), {"source": "bhavcopy", "min_score": 0})
    b = service.run_scan(market(), {"source": "bhavcopy", "min_score": 0})
    assert [(i["symbol"], i["score"]) for i in a["ideas"]] == [(i["symbol"], i["score"]) for i in b["ideas"]]
    earlier = market().store.dates()[-20].isoformat()
    c = service.run_scan(market(), {"source": "bhavcopy", "min_score": 0, "asof": earlier})
    assert c["asof"] == earlier and c["asof"] != a["asof"]
    try:
        service.run_scan(market(), {"source": "bhavcopy", "asof": "1999-01-01"})
    except service.ServiceError as e:
        assert "No data on or before" in str(e)
    else:
        raise AssertionError("expected ServiceError")


def test_scan_uses_the_selected_expiry_for_its_top_10():
    ctx = market()
    asof = ctx.store.dates()[-1]
    expiries = service.list_expiries(ctx, {"source": "bhavcopy", "asof": asof.isoformat()})["expiries"]
    assert expiries
    expiry = expiries[0]
    r = service.run_scan(ctx, {"source": "bhavcopy", "min_score": 0, "top_n": 10, "expiry": expiry})
    assert r["expiry"] == expiry and len(r["ideas"]) <= 10
    assert all(i["structure"]["legs"][0]["expiry"] == expiry for i in r["ideas"])
    assert len({i["symbol"] for i in r["ideas"]}) == len(r["ideas"])
    try:
        service.run_scan(ctx, {"source": "bhavcopy", "expiry": "1999-01-01"})
    except service.ServiceError as e:
        assert "No option contracts are available" in str(e)
    else:
        raise AssertionError("expected ServiceError for an expiry absent from stored data")


def test_risk_budget_and_minimum_score_apply_to_the_scan():
    r = service.run_scan(market(), {"source": "bhavcopy", "min_score": 0, "risk_budget": 80000})
    for i in r["ideas"]:
        assert i["risk"]["lots"] >= 1 and (i["risk"]["max_loss"] is None or -i["risk"]["max_loss"] <= 80000 + 1e-6)
    none = service.run_scan(market(), {"source": "bhavcopy", "min_score": 100, "allow_futures": False})
    assert none["ideas"] == [] and none["issue"] is None and "none produced a trade" in none["note"]            # a real no-trade, not a data problem


def test_a_scan_before_the_index_exists_asks_for_it_then_works():
    ctx = build_market(3, days=70)
    r = service.run_scan(ctx, {"source": "bhavcopy", "min_score": 0})
    assert r["issue"]["code"] == "index_needed" and r["issue"]["missing"] == 70 and r["issue"]["fix"] == "index" and r["ideas"] == []
    assert service.status(ctx)["bhavcopy"]["index_missing"] == 70
    first = service.index_step(ctx, {"max_days": 40})
    assert first == {"built": 40, "remaining": 30, "total": 70}
    assert service.index_step(ctx, {"max_days": 40}) == {"built": 30, "remaining": 0, "total": 70}
    assert service.status(ctx)["bhavcopy"]["index_missing"] == 0
    done = service.run_scan(ctx, {"source": "bhavcopy", "min_score": 0})
    assert done["issue"] is None and done["eligible"] == 3


def test_a_few_new_days_are_indexed_on_the_fly():
    ctx = build_market(3, days=70)
    SummaryIndex(ctx.store, MKT).build(max_days=66)                       # 4 days missing: within the inline limit
    r = service.run_scan(ctx, {"source": "bhavcopy", "min_score": 0})
    assert r["issue"] is None and service.status(ctx)["bhavcopy"]["index_missing"] == 0


def test_a_short_history_is_a_data_problem_for_the_scan_too():
    ctx = build_market(3, days=30)
    r = service.run_scan(ctx, {"source": "bhavcopy"})
    assert r["issue"]["code"] == "insufficient_history" and r["issue"]["have"] == 30 and r["issue"]["need"] == 60 and "scan the market" in r["issue"]["title"]
    assert "TEST.csv" not in r["issue"]["detail"]
    empty = service.Context(tempfile.mkdtemp())
    try:
        service.run_scan(empty, {"source": "bhavcopy"})
    except service.ServiceError as e:
        assert "Sync" in str(e)


# ---------------------------------------------------------------- pass one, on hand-built summaries
def srow(d, sym, spot, iv, call_oi=10000, put_oi=10000, atm_oi=500, vol=1000, fut=(100.0, 100000), fut_exp=dt.date(2026, 12, 29)):
    return {"date": d, "symbol": sym, "spot": spot, "expiry": fut_exp, "atm_iv": iv, "call_oi": call_oi, "put_oi": put_oi, "atm_oi": atm_oi,
            "atm_volume": 10, "opt_volume": vol, "fut_expiry": fut_exp, "fut_price": fut[0], "fut_oi": fut[1], "lot": 100}


def days(n=80, end=dt.date(2026, 10, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


def sideways_phase(n=80):
    """A phase of a sine wave at which the ENGINE (not my assumption) calls the trend neutral. A constant price would be read as bearish -3:
    the rules use strict inequalities, so exact ties count against the trend."""
    from fno.signals import compute_regime
    for ph in range(60):
        bars = [{"date": dt.date(2026, 1, 1) + dt.timedelta(days=i), "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(100 + 2 * math.sin((i + ph) / 3) for i in range(n))]
        g = compute_regime(bars)
        if g and g.trend == "neutral":
            return ph
    raise AssertionError("no neutral phase found")


def hand_data(n=80):
    ds = days(n)
    ph = sideways_phase(n)
    data = {"UPP": [], "DNN": [], "FLT": []}
    for i, d in enumerate(ds):
        last = i == n - 1
        data["UPP"].append(srow(d, "UPP", 100 + 0.012 * i * i, 0.35 - 0.0015 * i, put_oi=14000 if last else 10000, fut=(102.0, 108000) if last else (100.0, 100000), vol=3000))
        data["DNN"].append(srow(d, "DNN", 300 - 0.012 * i * i, 0.20 + 0.0015 * i, put_oi=6000 if last else 10000, fut=(98.0, 108000) if last else (100.0, 100000), vol=2000))
        iv = 0.15 if i == 0 else 0.40 if i == 1 else 0.20 if last else 0.25            # wide range in the history, today in the lower part: IV rank 20
        data["FLT"].append(srow(d, "FLT", 100 + 2 * math.sin((i + ph) / 3), iv, vol=500))
    return data, ds[-1]


def test_pass_one_ranks_stocks_whose_trend_iv_and_oi_agree_above_a_sideways_one():
    data, asof = hand_data()
    res = pass_one(data, asof, StrategyConfig(), MKT, ScanConfig())
    by = {r.symbol: r for r in res.rows}
    assert res.universe == 3 and res.skipped == {} and len(res.rows) == 3
    assert by["UPP"].regime.trend == "bullish" and by["UPP"].buildup == "long_buildup" and by["UPP"].buildup_strength == "strong" and abs(by["UPP"].pcr - 1.4) < 1e-9
    assert by["DNN"].regime.trend == "bearish" and by["DNN"].buildup == "short_buildup" and abs(by["DNN"].pcr - 0.6) < 1e-9
    assert by["UPP"].regime.iv_rank == 0.0 and by["DNN"].regime.iv_rank == 100.0
    assert [r.symbol for r in res.rows][-1] == "FLT" and by["UPP"].score > by["FLT"].score + 30 and by["DNN"].score > by["FLT"].score + 30
    for r in res.rows:
        assert abs(sum(r.parts.values()) - r.score) < 0.2 and 0 <= r.score <= 100
    assert by["UPP"].parts["oi"] == 25.0 and by["DNN"].parts["oi"] == 25.0                       # OI fully backs both views
    assert res.market["bullish"] == 1 and res.market["bearish"] == 1 and res.market["long_buildup"] == 1 and res.market["short_buildup"] == 1


def test_an_oi_picture_that_contradicts_the_trend_lowers_the_shortlist_score():
    data, asof = hand_data()
    base = {r.symbol: r for r in pass_one(data, asof, StrategyConfig(), MKT, ScanConfig()).rows}["UPP"]
    last = data["UPP"][-1]
    last.update(fut_price=98.0, fut_oi=108000, put_oi=6000)                                   # a short build-up and call-heavy OI under an uptrend
    flipped = {r.symbol: r for r in pass_one(data, asof, StrategyConfig(), MKT, ScanConfig()).rows}["UPP"]
    assert flipped.buildup == "short_buildup" and flipped.parts["oi"] < base.parts["oi"] - 20 and flipped.score < base.score


def test_pass_one_skips_and_counts_stocks_it_cannot_judge():
    data, asof = hand_data()
    data["NOIV"] = [dict(r, symbol="NOIV", atm_iv=None) if r["date"] == asof else dict(r, symbol="NOIV") for r in data["FLT"]]
    data["DEAD"] = [dict(r, symbol="DEAD", opt_volume=0) for r in data["FLT"]]
    data["SHORT"] = [dict(r, symbol="SHORT") for r in data["FLT"][-30:]]
    data["SPLIT"] = [dict(r, symbol="SPLIT", spot=r["spot"] * (2 if i < 70 else 1)) for i, r in enumerate(data["FLT"])]
    data["GONE"] = [dict(r, symbol="GONE") for r in data["FLT"][:-1]]                           # did not trade on the scan day: not part of the universe
    res = pass_one(data, asof, StrategyConfig(), MKT, ScanConfig())
    assert res.universe == 7 and len(res.rows) == 3
    assert res.skipped == {"no ATM implied volatility": 1, "no traded options": 1, "less than 60 days of history": 1, "probable split or bonus": 1}


def test_liquidity_percentile_and_score_parts_are_bounded():
    data, asof = hand_data()
    res = pass_one(data, asof, StrategyConfig(), MKT, ScanConfig())
    liq = {r.symbol: r.liq_pct for r in res.rows}
    assert liq["UPP"] == 1.0 and liq["FLT"] == 0.0 and 0 < liq["DNN"] < 1
    for r in res.rows:
        s, parts = shortlist_score(r, StrategyConfig())
        assert 0 <= parts["trend"] <= 40 and 0 <= parts["oi"] <= 25 and 0 <= parts["iv"] <= 20 and 0 <= parts["liquidity"] <= 15
    assert market_context([]) == {}


# ---------------------------------------------------------------- demo mode, server, dashboard backend
def test_the_demo_scan_works_without_any_bhavcopy_and_says_it_has_no_oi():
    ctx = service.Context(tempfile.mkdtemp())
    r = strict(service.run_scan(ctx, {"source": "synthetic", "min_score": 0}))
    assert r["issue"] is None and r["universe"] == 4 and r["ideas"]
    assert len({i["symbol"] for i in r["ideas"]}) == len(r["ideas"]) and all(i["source"] == "synthetic" for i in r["ideas"])
    assert any("no open-interest data" in n for n in r["data_notes"])
    assert all(i["risk"]["oi"] is None for i in r["ideas"])


def test_server_and_dashboard_backend_expose_the_scan():
    srv, base, _ = start()
    try:
        code, _, body = call(base + "/api/fno/scan", {"source": "synthetic", "min_score": 0})
        assert code == 200 and json.loads(body)["mode"] == "scan" and json.loads(body)["ideas"]
        code, _, body = call(base + "/api/fno/scan", {"source": "bhavcopy"})
        assert code == 400 and "No bhavcopy is stored yet" in json.loads(body)["detail"]
        code, _, body = call(base + "/api/fno/index", {"max_days": 10})
        assert code == 200 and json.loads(body) == {"built": 0, "remaining": 0, "total": 0}
    finally:
        srv.shutdown()
    be = Backend(market())
    r = asyncio.run(be.scan({"source": "bhavcopy", "min_score": 0}))
    assert r["issue"] is None and r["ideas"]
    assert asyncio.run(be.index({"max_days": 10}))["remaining"] == 0


def test_the_demo_scan_finds_a_day_with_ideas_but_never_when_a_date_is_given():
    ctx = service.Context(tempfile.mkdtemp())
    default = service.run_scan(ctx, {"source": "synthetic"})                         # default minimum score 55: the latest day alone may have nothing
    assert default["ideas"], "the demo screen should have something to show"
    last = max(b["date"] for b in service.demo_bars("DEMO")).isoformat()
    assert default["asof"] <= last
    if default["asof"] != last:
        assert any("Demo day" in n for n in default["data_notes"])
    pinned = service.run_scan(ctx, {"source": "synthetic", "asof": last})            # an explicit date is honoured exactly
    assert pinned["asof"] == last and not any("Demo day" in n for n in pinned["data_notes"])
