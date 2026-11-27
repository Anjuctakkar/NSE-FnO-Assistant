import dataclasses
import datetime as dt
import math
import random
import tempfile

from fno import greeks as bs
from fno import probability as P
from fno.backtest import BacktestResult, edge_vs_random, summarize
from fno.config import CostModel, MarketConfig, ProbConfig, StrategyConfig
from fno.costs import fill_price
from fno.data import SyntheticProvider
from fno.demo_data import make_synthetic_bars
from fno.models import Chain, Quote, Suggestion
from fno.selector import make_exit_plan, suggest
from fno.signals import adx, compute_regime
from fno.strategies import BUILDERS

MKT, CM, CFG, PC = MarketConfig(), CostModel(), StrategyConfig(), ProbConfig()


def env(n=500, seed=11):
    bars = make_synthetic_bars(n, seed=seed)
    return bars, SyntheticProvider("TEST", bars, MKT)


def chain_and_regime(idx=-60):
    bars, prov = env()
    asof = bars[idx]["date"]
    bb = [b for b in bars if b["date"] <= asof]
    exp = [e for e in prov.expiries("TEST", asof, 4) if 12 <= (e - asof).days <= 45][0]
    ch = prov.get_chain("TEST", asof, exp)
    return bars, bb, prov, ch, compute_regime(bb, prov.atm_iv_history("TEST", asof, 252), ch.atm_iv())


# ---------------------------------------------------------------- smile and skew
def _smile_chain(skew):
    S, T = 100.0, 30 / 365
    quotes = []
    for k in range(70, 131, 2):
        iv = 0.25 * (1 + skew * math.log(k / S))
        for kind in ("CE", "PE"):
            quotes.append(Quote(kind, float(k), dt.date(2026, 12, 26), bs.bs_price(S, k, T, 0.065, 0, iv, kind), iv=iv))
    return Chain("X", dt.date(2026, 11, 26), S, dt.date(2026, 12, 26), 100, quotes), S, T


def test_a_flat_smile_gives_the_plain_black_scholes_probability():
    ch, S, T = _smile_chain(0.0)
    sm = P.smile(ch)
    for K in (90.0, 100.0, 110.0):
        d2 = bs._d1_d2(S, K, T, 0.065, 0, 0.25)[1]
        assert abs(P.rn_prob_above(S, K, T, 0.065, 0, sm) - bs.norm_cdf(d2)) < 0.01


def test_put_skew_lowers_the_probability_of_staying_above_a_low_strike_compared_with_flat_atm_iv():
    """The old selector priced every probability from ATM IV. With put skew the market puts more weight on big falls, so the chance of
    staying above a low strike (a bull put spread's short strike) is LOWER than ATM IV says. The skew term is positive relative to the
    strike's own flat vol, because that vol is inflated by the fatter left tail."""
    skew, S, T = _smile_chain(-1.5)
    sm = P.smile(skew)
    K = 90.0
    at_atm = bs.norm_cdf(bs._d1_d2(S, K, T, 0.065, 0, P.iv_at(sm, S))[1])
    at_strike = bs.norm_cdf(bs._d1_d2(S, K, T, 0.065, 0, P.iv_at(sm, K))[1])
    corrected = P.rn_prob_above(S, K, T, 0.065, 0, sm)
    assert corrected < at_atm - 0.005 and corrected > at_strike + 0.005
    assert 0.0 <= P.rn_prob_above(S, 1.0, T, 0.065, 0, sm) <= 1.0


# ---------------------------------------------------------------- payoff geometry
def test_profit_intervals_match_the_known_shape_of_each_structure():
    _, _, _, ch, reg = chain_and_regime()
    bps = BUILDERS["Bull put spread"](ch, CFG, MKT, CM, 1)
    (lo, hi), = P.profit_intervals(bps)
    assert hi == math.inf and abs(bps.payoff(lo)) < 1e-6 and bps.payoff(lo + 1) > 0
    ic = BUILDERS["Iron condor"](ch, CFG, MKT, CM, 1)
    (lo, hi), = P.profit_intervals(ic)
    assert hi < math.inf and lo < ch.spot < hi and abs(ic.payoff(lo)) < 1e-6 and abs(ic.payoff(hi)) < 1e-6
    net = P.profit_intervals(ic, 500.0)[0]
    assert net[0] > lo and net[1] < hi                                            # costs shrink the winning zone


def test_net_of_cost_probability_is_never_above_gross():
    _, _, _, ch, reg = chain_and_regime()
    iv = ch.atm_iv()
    for name in ("Bull put spread", "Bear call spread", "Bull call spread", "Iron condor", "Long call"):
        s = BUILDERS[name](ch, CFG, MKT, CM, 1)
        a = P.analytic_probs(s, ch, make_exit_plan(s, s.payoff_stats(), reg, CFG), iv, reg.rv20, reg.rv60, MKT, CM, PC)
        assert a["pop_rn_net"] <= a["pop_rn"] + 1e-9 and 0 <= a["pop_rw_net"] <= 1


# ---------------------------------------------------------------- simulation
def test_simulation_with_every_exit_disabled_matches_the_closed_form():
    _, _, _, ch, reg = chain_and_regime()
    iv = ch.atm_iv()
    s = BUILDERS["Bull put spread"](ch, CFG, MKT, CM, 1)
    plan = dataclasses.replace(make_exit_plan(s, s.payoff_stats(), reg, CFG), profit_target=1e12, stop_loss=-1e12, exit_dte=-1, invalidation=None)
    pc = dataclasses.replace(PC, t_df=0, ivol_sd=0.0, paths=4000)
    sig = P.rw_sigma(iv, reg.rv20, reg.rv60, pc)
    mc = P.managed_mc(s, plan, ch, iv, sig, MKT, CM, pc)
    T = (ch.expiry - ch.asof).days / 365.0
    exact = P.lognormal_pop_ev(s, ch.spot, T, pc.drift, 0.0, sig, P.roundtrip_estimate(s, CM))[0]
    assert mc["p_expiry"] == 1.0 and abs(mc["p_win"] - exact) < 0.03


def test_simulation_is_deterministic_and_its_outcomes_add_up():
    _, _, _, ch, reg = chain_and_regime()
    iv = ch.atm_iv()
    s = BUILDERS["Bear call spread"](ch, CFG, MKT, CM, 1)
    plan = make_exit_plan(s, s.payoff_stats(), reg, CFG)
    a = P.managed_mc(s, plan, ch, iv, iv, MKT, CM, PC)
    b = P.managed_mc(s, plan, ch, iv, iv, MKT, CM, PC)
    assert a == b
    assert abs(a["p_target"] + a["p_stop"] + a["p_invalidation"] + a["p_time"] + a["p_expiry"] - 1.0) < 1e-3
    assert a["p05_net"] <= a["median_net"] <= a["p95_net"] and a["se"] > 0 and a["paths"] % 2 == 0


def test_a_tighter_invalidation_level_lowers_the_managed_win_rate_of_a_credit_spread():
    _, _, _, ch, reg = chain_and_regime()
    iv = ch.atm_iv()
    s = BUILDERS["Bull put spread"](ch, CFG, MKT, CM, 1)
    base = make_exit_plan(s, s.payoff_stats(), reg, CFG)
    tight = dataclasses.replace(base, invalidation=("below", ch.spot - 0.3 * reg.atr))
    none = dataclasses.replace(base, invalidation=None)
    w_tight = P.managed_mc(s, tight, ch, iv, iv, MKT, CM, PC)["p_win"]
    w_none = P.managed_mc(s, none, ch, iv, iv, MKT, CM, PC)["p_win"]
    assert w_tight < w_none - 0.05


def test_the_invalidation_level_is_never_inside_the_minimum_atr_distance():
    _, _, _, ch, reg = chain_and_regime()
    reg = dataclasses.replace(reg, swing_low=reg.spot - 0.1 * reg.atr, swing_high=reg.spot + 0.1 * reg.atr)
    bull = BUILDERS["Bull put spread"](ch, CFG, MKT, CM, 1)
    bear = BUILDERS["Bear call spread"](ch, CFG, MKT, CM, 1)
    pb = make_exit_plan(bull, bull.payoff_stats(), reg, CFG)
    pr = make_exit_plan(bear, bear.payoff_stats(), reg, CFG)
    assert pb.invalidation[0] == "below" and reg.spot - pb.invalidation[1] >= CFG.invalidation_min_atr * reg.atr - 0.01
    assert pr.invalidation[0] == "above" and pr.invalidation[1] - reg.spot >= CFG.invalidation_min_atr * reg.atr - 0.01


# ---------------------------------------------------------------- calibration
def test_with_no_record_the_probability_is_the_models_own_and_the_interval_is_wide():
    r = P.Calibration().apply(0.7, "credit", se_mc=0.02, pc=PC)
    assert r["p"] == 0.7 and r["grade"] == "low" and r["hi"] - r["lo"] > 0.2 and r["n_calibration"] == 0


def test_a_long_record_pulls_the_probability_toward_what_happened_and_narrows_the_interval():
    rng = random.Random(3)
    cal = P.Calibration()
    for _ in range(400):                                                           # the model says 70%, reality delivers 55%
        cal.add(0.70 + rng.uniform(-0.02, 0.02), rng.random() < 0.55, "credit")
    r = cal.apply(0.70, "credit", se_mc=0.02, pc=PC)
    assert 0.52 < r["p"] < 0.62 and r["lo"] < r["p"] < r["hi"] and r["hi"] - r["lo"] < 0.15
    assert r["grade"] in ("medium", "high") and r["n_calibration"] == 400
    honest = P.Calibration()
    for _ in range(400):
        honest.add(0.70 + rng.uniform(-0.02, 0.02), rng.random() < 0.70, "credit")
    assert abs(honest.apply(0.70, "credit", 0.0, PC)["p"] - 0.70) < 0.04              # a well-calibrated model is left alone


def test_calibration_report_scores_a_perfect_and_a_useless_model():
    rng = random.Random(5)
    good, bad = P.Calibration(), P.Calibration()
    for _ in range(600):
        p = rng.uniform(0.2, 0.8)
        won = rng.random() < p
        good.add(p, won)
        bad.add(0.5, won)
    assert good.report()["brier_skill"] > 0.05 and good.report()["ece"] < 0.08
    assert abs(bad.report()["brier_skill"]) < 0.02                                    # always saying 50% is no better than the base rate


def test_calibration_round_trips_through_a_file_and_ignores_bad_points():
    cal = P.Calibration()
    cal.add(0.6, True, "credit")
    cal.add(None, True)
    cal.add(1.7, True)
    path = tempfile.mkdtemp() + "/c.json"
    cal.save(path)
    again = P.Calibration.load(path)
    assert again.n == 1 and again.points[0][0] == 0.6 and again.updated
    assert P.Calibration.load(path + ".missing").n == 0


def test_walk_forward_check_uses_only_earlier_trades():
    rng = random.Random(9)
    trades = []
    d = dt.date(2026, 1, 1)
    for i in range(120):
        p = rng.uniform(0.4, 0.8)
        trades.append({"entry_date": (d + dt.timedelta(days=i)).isoformat(), "symbol": "X", "strategy": "Bull put spread", "p_win": p,
                       "net_pnl": 1.0 if rng.random() < p - 0.15 else -1.0, "exit_reason": "target"})
    wf = P.walk_forward_check(trades)
    assert wf["n"] == 100 and wf["calibration_helps"] is True and wf["brier_calibrated"] < wf["brier_raw"]      # the model is 15 points over-confident
    assert "need more" in P.walk_forward_check(trades[:10])["note"]


def test_wilson_and_bootstrap_intervals():
    lo, hi = P.wilson(8, 14)
    assert 0.33 < lo < 0.42 and 0.72 < hi < 0.80 and P.wilson(0, 0) == (0.0, 1.0)
    xs = [1.0] * 30 + [-1.0] * 10
    a, b = P.bootstrap_mean_ci(xs)
    assert a > 0 and a < 0.5 < b and (a, b) == P.bootstrap_mean_ci(xs)


# ---------------------------------------------------------------- integration with the selector, models, backtest
def test_suggestions_carry_the_win_block_and_survive_a_json_round_trip():
    bars, prov = env(500, seed=11)
    relaxed = StrategyConfig(min_score=0, futures_min_score=0, min_ev_r=-9.0)
    found = None
    for i in range(120, len(bars), 7):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], relaxed, MKT, CM, override_trend="bullish")
        if res.suggestions:
            found = res.suggestions[0]
            break
    assert found and found.win["managed"]["paths"] >= 100
    m, c = found.win["managed"], found.win["calibrated"]
    assert 0 <= m["p_win"] <= 1 and c["lo"] <= c["p"] <= c["hi"] and c["grade"] == "low"
    assert any("Win probability" in r for r in found.reasons) and any("calibrate" in w for w in found.warnings)
    assert abs(sum(found.risk["score_parts"].values()) - found.score) < 0.3 or found.score >= 99.9
    again = Suggestion.from_dict(found.to_dict())
    assert again.win == found.win
    old = found.to_dict()
    old.pop("win")
    assert Suggestion.from_dict(old).win == {}                                        # logs written before this field existed still load


def test_disabling_the_simulation_skips_it_without_breaking_anything():
    bars, prov = env(500, seed=11)
    cfg = StrategyConfig(min_score=0, futures_min_score=0, managed_paths=0, min_ev_r=-9.0)
    res = None
    for i in range(120, len(bars), 7):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], cfg, MKT, CM, override_trend="bullish")
        if res.suggestions:
            break
    assert res.suggestions and res.suggestions[0].win == {}


def test_a_calibration_record_changes_the_stated_probability_but_not_the_model_one():
    bars, prov = env(500, seed=11)
    cfg = StrategyConfig(min_score=0, futures_min_score=0, min_ev_r=-9.0)
    cal = P.Calibration()
    for _ in range(300):
        cal.add(0.5, False, "credit")
        cal.add(0.5, False, "debit")
        cal.add(0.5, False, "futures")
    for i in range(120, len(bars), 7):
        a = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], cfg, MKT, CM, override_trend="bullish")
        if a.suggestions:
            b = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], cfg, MKT, CM, override_trend="bullish", calibration=cal)
            x, y = a.suggestions[0].win, b.suggestions[0].win
            assert x["managed"]["p_win"] == y["managed"]["p_win"]
            assert y["calibrated"]["p"] < x["calibrated"]["p"] and y["calibrated"]["grade"] != "low"
            return
    raise AssertionError("no suggestion found")


def test_the_backtest_summary_reports_intervals_and_the_managed_calibration():
    trades = []
    rng = random.Random(1)
    for i in range(60):
        p = rng.uniform(0.4, 0.7)
        w = rng.random() < p
        trades.append({"strategy": "Bull put spread", "exit_reason": "target" if w else "stop", "net_pnl": 100.0 if w else -150.0, "gross_pnl": 120.0 if w else -130.0,
                       "costs": 20.0, "r_multiple": 0.6 if w else -1.0, "days_held": 9, "attribution": {}, "pop": 0.8, "p_win": p,
                       "iv_rank": 55, "entry_date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "symbol": "X", "exit_date": "2026-12-01"})
    m = summarize(trades)
    lo, hi = m["win_rate_ci90"]
    assert lo < m["win_rate"] < hi and m["expectancy_r_ci90"][0] <= m["expectancy_r"] <= m["expectancy_r_ci90"][1]
    assert m["calibration"]["managed"]["n"] == 60 and m["calibration"]["expiry"]["mean_predicted"] == 80.0
    assert m["calibration"]["walk_forward"]["n"] == 40 and "buckets" in m["calibration"] and "brier" in m["calibration"]


def _result(exp_r, n=40):
    return BacktestResult("X", {}, [], {"trades": n, "expectancy_r": exp_r, "expectancy_r_ci90": [exp_r - 0.1, exp_r + 0.1]})


def test_signal_must_beat_the_spread_of_random_runs_not_one_draw():
    noisy = [_result(x) for x in (-0.4, 0.3, -0.2, 0.2)]                           # random runs scatter widely: mean 0, sd ~0.33
    e = edge_vs_random(_result(0.2), noisy)
    assert e["random_runs"] == 4 and e["random_sd_r"] > 0.25 and "did not clearly beat" in e["verdict"]
    tight = [_result(x) for x in (-0.02, 0.0, 0.02, 0.0)]
    assert "beat the random baseline" in edge_vs_random(_result(0.3), tight)["verdict"]
    assert "includes zero" in edge_vs_random(BacktestResult("X", {}, [], {"trades": 40, "expectancy_r": 0.3, "expectancy_r_ci90": [-0.1, 0.7]}), tight)["verdict"]
    assert "did not clearly beat" in edge_vs_random(_result(0.3), _result(0.28))["verdict"]       # a single result still works


# ---------------------------------------------------------------- costs and signals
def test_fills_snap_to_the_tick_against_the_trader_and_never_assume_a_sub_tick_spread():
    cheap = Quote("PE", 90.0, dt.date(2026, 12, 29), ltp=1.00)
    buy, sell = fill_price(cheap, +1, CM), fill_price(cheap, -1, CM)
    assert buy >= 1.05 and sell <= 0.95 and abs(buy / 0.05 - round(buy / 0.05)) < 1e-6 and abs(sell / 0.05 - round(sell / 0.05)) < 1e-6
    real = Quote("CE", 100.0, dt.date(2026, 12, 29), ltp=10.0, bid=9.95, ask=10.05)
    assert fill_price(real, +1, CM) == 10.05 and fill_price(real, -1, CM) == 9.95            # a real quote is taken as it is
    assert fill_price(Quote("CE", 1.0, dt.date(2026, 12, 29), ltp=0.05), -1, CM) == 0.05       # never below one tick


def test_adx_separates_a_trend_from_a_range():
    rng = random.Random(1)

    def make(drift, vol):
        p, out = 100.0, []
        for i in range(120):
            o = p
            p *= 1 + drift + rng.gauss(0, vol)
            out.append({"date": dt.date(2026, 1, 1) + dt.timedelta(days=i), "open": o, "high": max(o, p) * 1.003, "low": min(o, p) * 0.997, "close": p})
        return out
    trend, rangey = make(0.006, 0.004), make(0.0, 0.012)
    assert adx(trend) > 40 > 30 > adx(rangey) and adx(trend[:20]) is None
    g = compute_regime(trend)
    assert g.adx == adx(trend) and g.mom_z > 2 and g.range_pos > 0.8
