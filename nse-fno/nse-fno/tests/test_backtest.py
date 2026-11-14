import datetime as dt
import json
import os
import tempfile

from fno.backtest import edge_vs_random, run_backtest, simulate_position, summarize
from fno.config import CostModel, MarketConfig, StrategyConfig
from fno.data import SyntheticProvider
from fno.demo_data import make_synthetic_bars
from fno.evaluate import evaluate_log, load_log, log_suggestions, set_taken
from fno.models import ExitPlan
from fno.selector import suggest

MKT, CM = MarketConfig(), CostModel()
CFG = StrategyConfig(min_score=0, futures_min_score=0, min_credit_to_width=0, min_pop_credit=0,
                     max_theta_decay_pct_day=100, min_rr_debit=0, max_spread_pct=100)


def one_suggestion(seed=11, trend="bullish"):
    bars = make_synthetic_bars(500, seed=seed)
    prov = SyntheticProvider("TEST", bars, MKT)
    for i in range(120, 400, 5):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], CFG, MKT, CM, override_trend=trend)
        opts = [s for s in res.suggestions if not s.structure.has_future]
        if opts:
            return bars, prov, i, opts[0]
    raise AssertionError("no suggestion found")


def forced(sug, **plan):
    """Same position, with the exit plan replaced so one rule can be tested in isolation."""
    p = dict(profit_target=1e12, stop_loss=-1e12, exit_dte=-1, invalidation=None)
    p.update(plan)
    sug.exit_plan = ExitPlan(p["profit_target"], p["stop_loss"], p["exit_dte"], p["invalidation"])
    return sug


def test_exit_rules_fire_and_have_the_documented_priority():
    bars, prov, i, sug = one_suggestion()
    dates = prov.trading_dates("TEST", sug.asof)
    assert simulate_position(forced(sug, profit_target=-1e12), prov, dates, CM, MKT)["status"] == "target"
    assert simulate_position(forced(sug, stop_loss=1e12), prov, dates, CM, MKT)["status"] == "stop"
    assert simulate_position(forced(sug, exit_dte=1000), prov, dates, CM, MKT)["status"] == "time"
    inv = simulate_position(forced(sug, stop_loss=1e12, invalidation=("below", 1e9)), prov, dates, CM, MKT)
    assert inv["status"] == "invalidation"                    # invalidation is checked before the stop
    first_day = dates[0]
    assert simulate_position(forced(sug, profit_target=-1e12), prov, dates, CM, MKT)["exit_date"] == first_day


def test_expiry_settlement_and_attribution_reconcile():
    bars, prov, i, sug = one_suggestion()
    r = simulate_position(forced(sug), prov, prov.trading_dates("TEST", sug.asof), CM, MKT)
    assert r["status"] == "expiry" and r["exit_date"] == sug.structure.expiry
    a = r["attribution"]
    explained = a["delta"] + a["gamma"] + a["theta"] + a["vega"] + a["unexplained"] + a["entry_slippage"] + a["exit_slippage"]
    assert abs(explained - r["gross_pnl"]) < 0.5, (explained, r["gross_pnl"], a)      # attribution accounts for every rupee
    assert abs(r["net_pnl"] - (r["gross_pnl"] - r["costs"])) < 0.01
    assert abs(a["unexplained"]) < 0.2 * sum(abs(a[k]) for k in ("delta", "gamma", "theta", "vega"))   # the Greeks carry most of it


def test_single_day_attribution_predicts_repricing():
    """The real test of the Greek attribution: for a small one-day move, delta + gamma + theta + vega should land
    close to the true change from repricing the option (not just reconcile by construction)."""
    from fno import greeks as bs
    from fno.backtest import _attribute
    from fno.models import Leg, Structure
    d0, d1, ex = dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 11, 24)
    r = MKT.risk_free
    for kind, K in (("CE", 100.0), ("PE", 95.0), ("CE", 108.0)):
        s = Structure("x", "T", [Leg(kind, +1, K, ex, 1.0, 0.30)], 100, d0)
        parts = _attribute(s, 100.0, 100.8, [0.30], [0.306], d0, d1, MKT)
        before = bs.bs_price(100.0, K, (ex - d0).days / 365, r, 0.0, 0.30, kind)
        after = bs.bs_price(100.8, K, (ex - d1).days / 365, r, 0.0, 0.306, kind)
        true = (after - before) * 100
        assert abs(sum(parts.values()) - true) < 0.06 * abs(true) + 1.0, (kind, K, parts, true)


def test_position_with_no_later_data_stays_open():
    bars, prov, i, sug = one_suggestion()
    r = simulate_position(forced(sug), prov, prov.trading_dates("TEST", sug.asof, sug.asof + dt.timedelta(days=3)), CM, MKT)
    assert r["status"] == "open" and r["exit_date"] is None


def test_no_lookahead_changing_the_future_does_not_change_the_past():
    bars = make_synthetic_bars(520, seed=21)
    k = 380
    cut = bars[k]["date"]
    changed = [dict(b) for b in bars]
    for b in changed[k + 1:]:                                  # rewrite everything after day k
        f = 0.6 if b["date"].toordinal() % 2 else 1.5
        for key in ("open", "high", "low", "close"):
            b[key] = round(b[key] * f, 2)
    a = run_backtest("TEST", bars, SyntheticProvider("TEST", bars, MKT), CFG, MKT, CM)
    b = run_backtest("TEST", changed, SyntheticProvider("TEST", changed, MKT), CFG, MKT, CM)
    done_a = [t for t in a.trades if t["exit_date"] and t["exit_date"] < cut.isoformat()]
    done_b = [t for t in b.trades if t["exit_date"] and t["exit_date"] < cut.isoformat()]
    assert done_a and done_a == done_b
    first_after = [t for t in a.trades if t["entry_date"] <= cut.isoformat()]
    assert [(t["entry_date"], t["strategy"], t["legs"]) for t in first_after] == \
           [(t["entry_date"], t["strategy"], t["legs"]) for t in b.trades if t["entry_date"] <= cut.isoformat()]


def test_backtest_is_reproducible_and_random_mode_differs():
    bars = make_synthetic_bars(500, seed=4)
    prov = SyntheticProvider("TEST", bars, MKT)
    a, b = run_backtest("TEST", bars, prov, CFG, MKT, CM), run_backtest("TEST", bars, prov, CFG, MKT, CM)
    assert a.trades == b.trades and a.metrics["trades"] > 5
    r1 = run_backtest("TEST", bars, prov, CFG, MKT, CM, entry_mode="random", seed=1)
    r2 = run_backtest("TEST", bars, prov, CFG, MKT, CM, entry_mode="random", seed=1)
    r3 = run_backtest("TEST", bars, prov, CFG, MKT, CM, entry_mode="random", seed=2)
    assert r1.trades == r2.trades and r1.trades != r3.trades
    assert edge_vs_random(a, r1)["verdict"]
    for t in a.trades:                                         # no overlapping positions
        pass
    spans = sorted((t["entry_date"], t["exit_date"] or "9999") for t in a.trades)
    assert all(x[1] < y[0] or x[1] <= y[0] for x, y in zip(spans, spans[1:]))


def test_costs_are_always_charged_and_slippage_hurts():
    bars = make_synthetic_bars(500, seed=4)
    r = run_backtest("TEST", bars, SyntheticProvider("TEST", bars, MKT), CFG, MKT, CM)
    assert r.trades and all(t["costs"] > 0 for t in r.trades)
    # the very same position, closed with and without frictions
    _, prov, _, sug = one_suggestion()
    dates = prov.trading_dates("TEST", sug.asof)
    sug = forced(sug, profit_target=-1e12)                      # exits on day one: no randomness in the comparison
    real = simulate_position(sug, prov, dates, CM, MKT)
    free = CostModel(brokerage_per_order=0, stt_futures_sell_pct=0, stt_option_sell_pct=0, stt_option_exercise_pct=0, exch_futures_pct=0,
                     exch_option_pct=0, sebi_per_crore=0, stamp_buy_futures_pct=0, stamp_buy_option_pct=0, gst_pct=0,
                     slippage_half_spread_frac=0, futures_slippage_pct=0)
    frictionless = simulate_position(sug, prov, dates, free, MKT)
    assert frictionless["costs"] == 0 and frictionless["net_pnl"] > real["net_pnl"]
    assert real["attribution"]["exit_slippage"] < 0 == frictionless["attribution"]["exit_slippage"]


def test_summarize_hand_checked():
    def t(net, risk=100.0, reason="target", strat="A", pop=0.6, ivr=60):
        return {"net_pnl": net, "gross_pnl": net + 10, "costs": 10.0, "r_multiple": net / risk, "exit_reason": reason, "strategy": strat,
                "days_held": 5, "pop": pop, "iv_rank": ivr,
                "attribution": {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "unexplained": 0.0, "entry_slippage": 0.0, "exit_slippage": 0.0}}
    m = summarize([t(100), t(-50), t(-50), t(-50), t(200), {**t(0), "exit_reason": "open"}])
    assert m["trades"] == 5 and m["open_trades"] == 1 and m["small_sample"]
    assert m["win_rate"] == 40.0 and m["total_net_pnl"] == 150 and m["avg_net_pnl"] == 30
    assert m["profit_factor"] == 2.0 and m["longest_losing_streak"] == 3
    assert m["max_drawdown"] == 150 and abs(m["expectancy_r"] - 0.3) < 1e-9
    assert m["calibration"]["n"] == 5 and abs(m["calibration"]["brier"] - ((0.4 ** 2) * 2 + (0.6 ** 2) * 3) / 5) < 1e-3
    assert summarize([])["trades"] == 0


def test_forward_evaluation_log_dedupes_and_replays():
    bars, prov, i, sug = one_suggestion()
    path = os.path.join(tempfile.mkdtemp(), "log.jsonl")
    assert log_suggestions(path, [sug])["added"] == 1
    assert log_suggestions(path, [sug]) == {"added": 0, "skipped": 1, "total": 1}
    assert set_taken(path, sug.id) and load_log(path)[0]["taken"] is True and not set_taken(path, "nope")
    ev = evaluate_log(path, prov, CM, MKT)
    row = ev["rows"][0]
    assert row["taken"] and row["status"] in ("target", "stop", "invalidation", "time", "expiry", "open")
    direct = simulate_position(sug, prov, prov.trading_dates("TEST", sug.asof), CM, MKT)
    assert row["net_pnl"] == direct["net_pnl"]                 # evaluation uses the very same simulator as the backtest
    early = evaluate_log(path, prov, CM, MKT, upto=sug.asof)         # no data after the suggestion yet
    assert early["open_positions"] == 1 and early["rows"][0]["status"] == "open"
    json.dumps(ev, default=str)
