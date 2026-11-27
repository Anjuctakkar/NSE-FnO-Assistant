import datetime as dt
import math
import tempfile
import threading
import time

try:
    from .test_scanner import build_market, MKT
except ImportError:
    from test_scanner import build_market, MKT

from fno import greeks as bs
from fno import live_scan, service
from fno.breeze_market import MarketDataError, RateLimiter
from fno.demo_data import make_synthetic_bars
from fno.summary import SummaryIndex

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SEEDS = {"AAA": (11, 3000.0), "BBB": (12, 1500.0), "CCC": (13, 800.0), "DDD": (14, 2000.0), "EEE": (15, 600.0), "FFF": (16, 1200.0)}
_CACHE: dict = {}


def market():
    if "m" not in _CACHE:
        ctx = build_market(6)
        SummaryIndex(ctx.store, MKT).build()
        _CACHE["m"] = ctx
    return _CACHE["m"]


def bars_for(sym):
    seed, s0 = SEEDS.get(sym, (31, 1000.0))
    return make_synthetic_bars(400, seed=seed, s0=s0)


class FakeBreeze:
    """Stands in for BreezeMarketClient: serves synthetic prices and a Black-Scholes chain, and counts calls the way the real one does
    (quote 1, daily candles 1, option chain 2)."""

    def __init__(self, fail=None, fatal=None, slow=0.0, today=None, moves=None):
        self.limiter = RateLimiter(10 ** 6, 10 ** 6)
        self.fail, self.fatal, self.slow = fail or {}, fatal, slow
        self.seen: list[str] = []
        self.today = today
        self.moves = moves or {}                                   # symbol -> live price as a multiple of the last stored close
        self.kinds: list[str] = []

    def _px(self, sym):
        return round(bars_for(sym)[-1]["close"] * self.moves.get(sym, 1.004), 2)

    def _t(self, sym):
        self.limiter.acquire()
        if self.slow:
            time.sleep(self.slow)
        if self.fatal:
            raise MarketDataError(self.fatal)
        if sym in self.fail:
            raise MarketDataError(self.fail[sym])

    def get_stock_quote(self, sym):
        self.seen.append(sym)
        self.kinds.append("quote")
        self._t(sym)
        last = bars_for(sym)[-1]["close"]
        px = self._px(sym)
        return {"ltp": px, "open": last, "high": max(px, last) * 1.002, "low": min(px, last) * 0.998, "timestamp": "now"}

    def get_daily_bars(self, sym, a, b):
        self.kinds.append("daily")
        self._t(sym)
        return [{"datetime": f"{x['date']}T00:00:00.000Z", "open": x["open"], "high": x["high"], "low": x["low"], "close": x["close"]}
                for x in bars_for(sym)]

    def get_option_chain_records(self, sym, expiry_str):
        self.kinds.append("chain")
        self._t(sym)
        self.limiter.acquire()
        exp = dt.date.fromisoformat(expiry_str[:10])
        spot = self._px(sym)
        T = max((exp - self.today).days, 1) / 365.0
        step = 10 ** max(0, int(math.log10(spot)) - 2)
        out = []
        k = round(spot * 0.75 / step) * step
        while k <= spot * 1.25:
            for right, kind in (("call", "CE"), ("put", "PE")):
                iv = 0.28 * (1 + (-0.5) * math.log(k / spot) + 1.5 * math.log(k / spot) ** 2)
                px = max(bs.bs_price(spot, k, T, MKT.risk_free, 0.0, iv, kind), 0.05)
                out.append({"strike_price": k, "right": right, "ltp": round(px, 2), "best_bid_price": round(px * 0.99, 2),
                            "best_offer_price": round(px * 1.01 + 0.05, 2), "open_interest": 5000, "total_quantity_traded": 300})
            k += step
        return out


def setup(monkeypatch_today=True):
    ctx = market()
    today = ctx.store.dates()[-1] + dt.timedelta(days=1)
    service._india_now = lambda: dt.datetime.combine(today, dt.time(11, 0), tzinfo=IST)
    return ctx, today


def run(req=None, client=None, lcfg=None):
    ctx, today = setup()
    client = client or FakeBreeze(today=today)
    client.today = today
    job = live_scan.LiveScanJob("t")
    out = live_scan.run_live_scan(ctx, dict({"min_score": 0}, **(req or {})), client, job, service, lcfg or live_scan.LiveScanConfig(reserve_calls=0))
    return out, client, job


# ---------------------------------------------------------------- rate limiter
def test_rate_limiter_waits_when_the_minute_is_full_and_refuses_when_the_day_is_spent():
    now = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(per_minute=3, per_day=5, clock=lambda: now[0], sleep=sleep, today=lambda: "2026-11-26")
    for _ in range(3):
        rl.acquire()
    assert not slept
    rl.acquire()                                                   # the 4th call inside a minute must wait for the first to age out
    assert slept and 59.9 < sum(slept) < 61
    rl.acquire()
    assert rl.used_today == 5 and rl.remaining_today == 0
    try:
        rl.acquire()
        raise AssertionError("expected the daily limit to refuse")
    except MarketDataError as e:
        assert "daily limit" in str(e)


def test_rate_limiter_remembers_the_days_count_across_restarts_and_resets_on_a_new_day():
    p = tempfile.mkdtemp() + "/usage.json"
    day = ["2026-11-26"]
    a = RateLimiter(100, 1000, sleep=lambda s: None, today=lambda: day[0], state_path=p)
    for _ in range(7):
        a.acquire()
    b = RateLimiter(100, 1000, sleep=lambda s: None, today=lambda: day[0], state_path=p)
    assert b.used_today == 7
    day[0] = "2026-11-27"
    assert b.used_today == 0


# ---------------------------------------------------------------- whole-market live scan
def test_stage_a_prices_every_stock_live_then_only_the_best_are_evaluated_in_full():
    out, client, job = run({"shortlist": 3, "top_n": 5})
    assert out["source"] == "breeze-live" and out["mode"] == "scan"
    assert out["eligible"] == 6 and out["priced_live"] == 6 and out["shortlisted"] == 3 and len(set(client.seen)) == 6      # every eligible stock priced
    assert client.kinds.count("quote") == 6 and client.kinds.count("daily") == 3 and client.kinds.count("chain") == 3       # full evaluation: 3 stocks
    assert out["api_calls"] == 6 + 3 * 3 == client.limiter.used_today                                                       # the stage-A quote is reused, not refetched
    assert out["stage_a"] == "6 of 6 stocks priced live" and out["ranking_day"] == market().store.dates()[-1].isoformat()
    assert out["failed"] == {} and out["stopped"] is None
    ideas = out["ideas"]
    assert [i["score"] for i in ideas] == sorted((i["score"] for i in ideas), reverse=True)
    assert len({i["symbol"] for i in ideas}) == len(ideas)
    for i in ideas:
        assert i["source"] == "breeze-live" and i["structure"]["legs"] and i["win"]["managed"]["p_win"] is not None
        assert i["win"]["calibrated"]["lo"] <= i["win"]["calibrated"]["p"] <= i["win"]["calibrated"]["hi"]
        assert i["scan"]["rank"] >= 1 and abs(i["spot"] - bars_for(i["symbol"])[-1]["close"] * 1.004) < 0.02
        assert i["regime"]["adx"] is not None                                    # the regime was computed on daily candles
        assert i["scan"]["live_chg_pct"] is not None and i["scan"]["eod_rank"] and i["scan"]["live_rank"] <= 3
        assert i["regime"]["iv_rank"] is not None                                # one-year IV rank from the stored history, live IV ranked against it
    assert any("Hybrid scan" in n for n in out["data_notes"])


def test_without_stage_a_only_the_shortlist_is_touched_as_before():
    out, client, _ = run({"shortlist": 3, "quote_all": False})
    assert out["priced_live"] == 0 and len(set(client.seen)) == 3 and out["api_calls"] == 12 and out["stage_a"] == "not run"


def test_live_prices_re_rank_the_stored_end_of_day_ranking():
    from fno.scanner import rerank_with_live
    ctx, today = setup()
    rows = live_scan.shortlist_rows(ctx, today, service.strategy_config({}), live_scan.LiveScanConfig(), None, [])[0]
    eod = [r.symbol for r in rows]
    last = rows[-1].symbol
    quotes = {r.symbol: {"ltp": r.bars[-1]["close"] * 1.001, "open": r.bars[-1]["close"], "high": r.bars[-1]["close"] * 1.002, "low": r.bars[-1]["close"] * 0.999}
              for r in rows}
    quotes[last] = {"ltp": rows[-1].bars[-1]["close"] * 1.09, "open": rows[-1].bars[-1]["close"] * 1.05}             # a 9% gap up on the last-ranked stock
    quotes[rows[0].symbol] = {"ltp": rows[0].bars[-1]["close"] * 1.40}                                                # 40%: a probable split, left alone
    new = rerank_with_live(rows, quotes, today, service.strategy_config({}))
    by = {r.symbol: r for r in new}
    assert abs(by[last].live_chg - 0.09) < 1e-9 and by[last].regime.spot != rows[-1].regime.spot and by[last].bars[-1]["date"] == today
    assert by[rows[0].symbol].live_chg is None and by[rows[0].symbol].eod_rank == 1                                 # corporate-action guard
    assert [r.eod_rank for r in new if r.symbol == last] == [len(eod)] and sorted(r.symbol for r in new) == sorted(eod)
    assert [r.score for r in new] == sorted((r.score for r in new), reverse=True)


def test_stage_a_is_skipped_with_a_reason_when_the_budget_cannot_cover_it():
    out, client, _ = run({"shortlist": 3, "max_calls": 14})
    assert out["priced_live"] == 0 and out["stage_a"].startswith("skipped") and any("not fetched for every stock" in n for n in out["data_notes"])
    assert len(set(client.seen)) <= 3


def test_stage_a_is_skipped_when_the_stored_bhavcopy_already_is_todays():
    ctx = market()
    day = ctx.store.dates()[-1]
    service._india_now = lambda: dt.datetime.combine(day, dt.time(16, 0), tzinfo=IST)
    client = FakeBreeze(today=day)
    out = live_scan.run_live_scan(ctx, {"shortlist": 2, "min_score": 0}, client, live_scan.LiveScanJob("t"), service, live_scan.LiveScanConfig(reserve_calls=0))
    assert out["stage_a"].startswith("skipped: the stored bhavcopy already is today") and out["priced_live"] == 0


def test_a_refresh_re_evaluates_only_the_named_stocks_and_skips_stage_a():
    out, client, _ = run({"symbols": ["AAA", "BBB"], "refresh": True, "shortlist": 2})
    assert out["refresh"] is True and set(client.seen) == {"AAA", "BBB"} and out["api_calls"] == 8 and out["priced_live"] == 0
    assert out["stage_a"] == "skipped: refreshing named stocks"


def test_the_single_stock_view_uses_the_same_hybrid_inputs_when_bhavcopy_is_stored():
    ctx, today = setup()
    fake = FakeBreeze(today=today)
    original = service._breeze_client_from_request
    service._breeze_client_from_request = lambda req: fake
    try:
        r = service.run_suggest(ctx, {"source": "breeze-live", "symbol": "AAA", "min_score": 0, "risk_budget": 500000})
    finally:
        service._breeze_client_from_request = original
    assert fake.kinds == ["quote", "daily", "chain"]                           # no 5-minute candles, no per-option history
    text = " ".join(r["data_notes"])
    assert "Hybrid data" in text and "5-minute" not in text
    assert r["regime"]["iv_rank"] is not None and r["regime"]["adx"] is not None
    assert r["source"] == "breeze-live"


def test_the_single_stock_view_falls_back_to_live_only_data_when_nothing_is_stored():
    empty = service.Context(tempfile.mkdtemp())
    assert service._hybrid_base(empty, "AAA") is None


def test_market_session_follows_regular_hours():
    mk = lambda d, h, m: dt.datetime(2026, 11, d, h, m, tzinfo=IST)           # 23 Nov 2026 is a Monday
    assert service.market_session(mk(23, 9, 14))["open"] is False and service.market_session(mk(23, 9, 15))["open"] is True
    assert service.market_session(mk(23, 15, 29))["open"] is True and service.market_session(mk(23, 15, 30))["open"] is False
    assert service.market_session(mk(28, 11, 0))["open"] is False and "holidays" in service.market_session(mk(23, 11, 0))["note"]


def test_live_scan_uses_daily_candles_with_todays_bar_set_to_the_live_price():
    ctx, today = setup()
    bars = bars_for("AAA")
    q = {"ltp": 3100.0, "open": 3050.0, "high": 3120.0, "low": 3040.0}
    merged = live_scan._merge_today(bars, q, today)
    assert merged[-1]["date"] == today and merged[-1]["close"] == 3100.0 and merged[-1]["high"] == 3120.0 and merged[-1]["low"] == 3040.0
    assert len(merged) == len([b for b in bars if b["date"] < today]) + 1
    again = live_scan._merge_today(merged, {"ltp": 3110.0}, today)               # re-running the same day replaces, never duplicates
    assert [b["date"] for b in again].count(today) == 1 and again[-1]["close"] == 3110.0


def test_one_failing_stock_is_reported_and_the_rest_continue():
    ctx, today = setup()
    first = live_scan.shortlist_rows(ctx, today, service.strategy_config({}), live_scan.LiveScanConfig(shortlist=3), None, [])[0]
    bad = first[0].symbol
    out, client, _ = run({"shortlist": 3, "quote_all": False}, FakeBreeze(fail={bad: "ICICI Breeze option-chain failed: no data"}))
    assert list(out["failed"]) == [bad] and "no data" in out["failed"][bad]
    assert out["fetched"] == 2 and out["shortlisted"] == 3


def test_an_authentication_failure_stops_the_scan_instead_of_failing_every_stock():
    ctx, today = setup()
    client = FakeBreeze(fatal="Breeze says the session token has expired.")
    try:
        run({"shortlist": 4}, client)
        raise AssertionError("expected the scan to stop")
    except MarketDataError as e:
        assert "expired" in str(e)
    assert len(client.seen) == 1                                                  # one attempt, not one per stock


def test_the_call_budget_stops_the_scan_early_and_names_what_was_not_fetched():
    out, client, _ = run({"shortlist": 5, "max_calls": 9})
    assert out["api_calls"] <= 9 and out["stopped"] and "budget" in out["stopped"]
    assert out["fetched"] == 2 and len(out["not_reached"]) == 3
    assert any("stopped early" in n for n in out["data_notes"])


def test_the_days_reserve_is_never_spent():
    ctx, today = setup()
    client = FakeBreeze(today=today)
    client.limiter = RateLimiter(10 ** 6, 100)
    out, _, _ = run({"shortlist": 6, "max_calls": 500}, client, live_scan.LiveScanConfig(reserve_calls=80))
    assert client.limiter.remaining_today >= 80 - 5 and out["stopped"] and "allowance" in out["stopped"]


def test_without_stored_bhavcopy_the_scan_needs_named_stocks_and_says_iv_rank_is_unknown():
    empty = service.Context(tempfile.mkdtemp())
    today = dt.date(2026, 11, 26)
    service._india_now = lambda: dt.datetime.combine(today, dt.time(11, 0), tzinfo=IST)
    try:
        live_scan.run_live_scan(empty, {}, FakeBreeze(today=today), live_scan.LiveScanJob("t"), service)
        raise AssertionError("expected a request for a universe")
    except ValueError as e:
        assert "bhavcopy" in str(e)
    out = live_scan.run_live_scan(empty, {"symbols": ["AAA"], "min_score": 0}, FakeBreeze(today=today), live_scan.LiveScanJob("t"), service)
    assert out["shortlisted"] == 1 and any("IV rank is unknown" in n for n in out["data_notes"])


def test_naming_stocks_bypasses_the_ranking_and_reports_names_it_cannot_use():
    out, client, _ = run({"symbols": ["BBB", "NOPE"]})
    assert set(client.seen) == {"BBB"} and any("NOPE" in n for n in out["data_notes"])


# ---------------------------------------------------------------- job manager
def test_only_one_scan_runs_at_a_time_and_it_can_be_cancelled():
    ctx, today = setup()
    client = FakeBreeze(slow=0.15, today=today)
    mgr = live_scan.LiveScanManager()

    def work(job):
        return live_scan.run_live_scan(ctx, {"shortlist": 6, "min_score": 0}, client, job, service, live_scan.LiveScanConfig(reserve_calls=0))

    job, started = mgr.start(work)
    again, started2 = mgr.start(work)
    assert started and not started2 and again.id == job.id
    time.sleep(0.5)
    job.cancel.set()
    for _ in range(100):
        if job.state not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert job.state == "cancelled" and job.result["stopped"] == "cancelled" and job.result["not_reached"]
    nxt, started3 = mgr.start(lambda j: {"ok": True})
    assert started3 and nxt.id != job.id


def test_a_failed_scan_reports_the_reason_and_frees_the_slot():
    ctx, today = setup()
    mgr = live_scan.LiveScanManager()
    job, _ = mgr.start(lambda j: live_scan.run_live_scan(ctx, {"shortlist": 2}, FakeBreeze(fatal="checksum mismatch", today=today), j, service))
    for _ in range(100):
        if job.state not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job.state == "error" and "checksum" in job.error
    assert mgr.running() is None


def test_service_endpoints_start_poll_and_never_keep_credentials():
    ctx, today = setup()
    fake = FakeBreeze(today=today)
    original = service._breeze_client_from_request
    service._breeze_client_from_request = lambda req: fake
    try:
        r = service.live_scan_start(ctx, {"api_key": "K", "api_secret": "S", "session_token": "T", "shortlist": 2, "min_score": 0})
        assert r["started"] and r["job"]
        for _ in range(200):
            st = service.live_scan_status(ctx, {"id": r["job"]})
            if st["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert st["state"] == "done" and st["result"]["shortlisted"] == 2
        text = repr(st)
        assert "api_secret" not in text and "session_token" not in text and "'K'" not in text and "'T'" not in text
    finally:
        service._breeze_client_from_request = original
    try:
        service.live_scan_status(ctx, {"id": "nope"})
        raise AssertionError("expected an error")
    except service.ServiceError:
        pass
