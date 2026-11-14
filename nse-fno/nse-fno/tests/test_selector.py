import datetime as dt
import math

from fno import greeks as bs
from fno.config import CostModel, MarketConfig, StrategyConfig
from fno.data import SyntheticProvider
from fno.demo_data import make_synthetic_bars
from fno.models import Leg, Structure
from fno.selector import candidate_names, evaluate, make_exit_plan, pick_expiries, pick_expiry, structure_pop, suggest
from fno.signals import Regime, compute_regime, iv_percentile, iv_rank
from fno.strategies import bear_call_spread, bull_call_spread, bull_put_spread, iron_condor, pick_by_delta

MKT, CM = MarketConfig(), CostModel()
RELAXED = StrategyConfig(min_score=0, futures_min_score=0, min_credit_to_width=0, min_pop_credit=0,
                         max_theta_decay_pct_day=100, min_rr_debit=0, max_spread_pct=100)


def env(n=400, seed=5):
    bars = make_synthetic_bars(n, seed=seed)
    return bars, SyntheticProvider("TEST", bars, MKT)


def regime(trend, score, ivr, **kw):
    base = dict(asof=dt.date(2026, 1, 1), spot=100.0, trend=trend, trend_score=score, rsi=55.0, macd_hist=1.0, sma20=100.0,
                sma50=98.0, atr=2.0, atr_pct=2.0, rv20=0.30, rv60=0.30, swing_low=95.0, swing_high=105.0, iv=0.30,
                iv_rank=ivr, iv_percentile=ivr, vrp=0.0)
    base.update(kw)
    return Regime(**base)


def test_candidate_matrix():
    cfg = StrategyConfig()
    assert candidate_names(regime("bullish", 2, 10), cfg) == ["Bull call spread"]
    assert candidate_names(regime("bullish", 3, 10), cfg) == ["Bull call spread", "Long call", "Long future"]
    assert candidate_names(regime("bullish", 2, 40), cfg) == ["Bull call spread", "Bull put spread"]
    assert candidate_names(regime("bullish", 2, 80), cfg) == ["Bull put spread"]
    assert candidate_names(regime("bearish", -2, 80), cfg) == ["Bear call spread"]
    assert candidate_names(regime("bearish", -4, 10), cfg) == ["Bear put spread", "Long put", "Short future"]
    assert candidate_names(regime("neutral", 0, 80), cfg) == ["Iron condor"]
    assert candidate_names(regime("neutral", 0, 40), cfg) == []
    assert candidate_names(regime("neutral", 0, 10), cfg) == []
    assert "Long future" not in candidate_names(regime("bullish", 4, 80), StrategyConfig(allow_futures=False))


def test_iv_rank_and_percentile():
    hist = [0.20 + 0.001 * i for i in range(100)]          # 0.20 .. 0.299
    assert iv_rank(hist, 0.2495) == 50.0 and iv_rank(hist, 0.30) == 100.0
    assert iv_rank(hist, 0.50) == 100.0 and iv_rank(hist, 0.05) == 0.0       # outside the historical range: clamped
    assert iv_percentile(hist, 0.2495) == 50.0
    assert iv_rank(hist[:10], 0.25) is None                # too little history


def test_trend_scoring_on_a_clean_up_and_down_trend():
    def bars_for(closes):
        d = dt.date(2025, 1, 1)
        return [{"date": d + dt.timedelta(days=i), "open": c, "high": c * 1.005, "low": c * 0.995, "close": c} for i, c in enumerate(closes)]
    up = compute_regime(bars_for([100 + 0.012 * i * i + (i % 3) * 0.2 for i in range(120)]))      # accelerating rise
    down = compute_regime(bars_for([200 - 0.012 * i * i + (i % 3) * 0.2 for i in range(120)]))    # accelerating fall
    assert up.trend == "bullish" and up.trend_score >= 2
    assert down.trend == "bearish" and down.trend_score <= -2
    assert compute_regime(bars_for([100.0] * 30)) is None


def test_delta_picking_and_leg_geometry():
    bars, prov = env()
    chain = prov.get_chain("TEST", bars[-1]["date"], prov.expiries("TEST", bars[-1]["date"], 2)[1])
    call30, put30 = pick_by_delta(chain, "CE", 0.30, MKT), pick_by_delta(chain, "PE", 0.30, MKT)
    assert call30.strike > chain.spot > put30.strike
    T = (chain.expiry - chain.asof).days / 365
    d = bs.greeks(chain.spot, call30.strike, T, MKT.risk_free, 0.0, call30.iv, "CE").delta
    assert abs(d - 0.30) < 0.06
    cfg = StrategyConfig()
    bcs = bull_call_spread(chain, cfg, MKT, CM)
    assert bcs.legs[0].strike < bcs.legs[1].strike and bcs.net_premium() < 0
    bps = bull_put_spread(chain, cfg, MKT, CM)
    assert bps.legs[1].strike < bps.legs[0].strike and bps.net_premium() > 0
    bear = bear_call_spread(chain, cfg, MKT, CM)
    assert bear.legs[1].strike > bear.legs[0].strike and bear.net_premium() > 0
    ic = iron_condor(chain, cfg, MKT, CM)
    k = [l.strike for l in ic.legs]            # short put, long put, short call, long call
    assert k[1] < k[0] < chain.spot < k[2] < k[3]


def test_probability_model_is_consistent_with_black_scholes():
    S, K, T, r, q, sig = 100.0, 100.0, 0.25, 0.065, 0.0, 0.30
    exp = dt.date(2026, 1, 1) + dt.timedelta(days=91)
    price = bs.bs_price(S, K, T, r, q, sig, "CE")
    s = Structure("Long call", "T", [Leg("CE", +1, K, exp, price, sig)], 1, dt.date(2026, 1, 1))
    pop, ev = structure_pop(s, S, T, r, q, sig)
    assert abs(pop - bs.prob_above(S, K + price, T, r, q, sig)) < 0.005
    assert abs(ev) < 0.02 * price                         # priced at the model: expected P&L is ~0 (only carry remains)


def test_hard_filters_reject_with_a_reason():
    bars, prov = env()
    asof = bars[-1]["date"]
    chain = prov.get_chain("TEST", asof, prov.expiries("TEST", asof, 2)[1])
    reg = regime("bullish", 2, 40, spot=chain.spot, swing_low=chain.spot * 0.95, swing_high=chain.spot * 1.05)
    s = bull_put_spread(chain, StrategyConfig(), MKT, CM)
    sug, why = evaluate(s, chain, reg, StrategyConfig(max_spread_pct=0.01), MKT, CM)
    assert sug is None and "bid-ask" in why
    sug, why = evaluate(s, chain, reg, StrategyConfig(min_pop_credit=0.999, max_spread_pct=100, min_credit_to_width=0), MKT, CM)
    assert sug is None and "probability of profit" in why
    sug, why = evaluate(s, chain, reg, StrategyConfig(min_dte_credit=999, max_spread_pct=100), MKT, CM)
    assert sug is None and "short-gamma window" in why


def test_risk_budget_sizes_lots_and_rejects_when_one_lot_is_too_big():
    bars, prov = env()
    asof = bars[-1]["date"]
    chain = prov.get_chain("TEST", asof, prov.expiries("TEST", asof, 2)[1])
    reg = regime("bullish", 2, 40, spot=chain.spot, swing_low=chain.spot * 0.95)
    s = bull_call_spread(chain, RELAXED, MKT, CM)
    one, _ = evaluate(s, chain, reg, RELAXED, MKT, CM)
    max_loss_1 = -one.risk["max_loss"]
    cfg = StrategyConfig(**{**RELAXED.__dict__, "risk_budget": max_loss_1 * 3.5})
    three, _ = evaluate(s, chain, reg, cfg, MKT, CM)
    assert three.risk["lots"] == 3 and three.structure.legs[0].lots == 3
    assert abs(-three.risk["max_loss"] - 3 * max_loss_1) < 1e-6
    tiny = StrategyConfig(**{**RELAXED.__dict__, "risk_budget": max_loss_1 * 0.5})
    none, why = evaluate(s, chain, reg, tiny, MKT, CM)
    assert none is None and "risk budget" in why


def test_exit_plans_follow_the_structure():
    bars, prov = env()
    asof = bars[-1]["date"]
    chain = prov.get_chain("TEST", asof, prov.expiries("TEST", asof, 2)[1])
    reg = regime("bullish", 2, 40, spot=chain.spot, swing_low=chain.spot * 0.95)
    cfg = StrategyConfig()
    credit = bull_put_spread(chain, cfg, MKT, CM)
    st = credit.payoff_stats()
    p = make_exit_plan(credit, st, reg, cfg)
    assert abs(p.profit_target - 0.5 * st["max_profit"]) < 0.01
    assert p.stop_loss >= st["max_loss"] and abs(p.stop_loss + min(-st["max_loss"], 1.5 * credit.net_premium())) < 0.01
    assert p.invalidation == ("below", round(reg.swing_low, 2))
    debit = bull_call_spread(chain, cfg, MKT, CM)
    sd = debit.payoff_stats()
    pd_ = make_exit_plan(debit, sd, reg, cfg)
    assert abs(pd_.stop_loss + 0.5 * -debit.net_premium()) < 0.01 and abs(pd_.profit_target - 0.6 * sd["max_profit"]) < 0.01


def test_suggest_end_to_end_is_explained_and_logged_fields_are_complete():
    bars, prov = env(500, seed=11)
    found = None
    for i in range(120, len(bars), 7):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], RELAXED, MKT, CM, override_trend="bullish")
        if res.suggestions:
            found = res
            break
    assert found, "expected at least one suggestion somewhere in 500 synthetic bars"
    s = found.suggestions[0]
    assert s.reasons and s.exit_plan.stop_loss < 0 < s.exit_plan.profit_target
    assert {"signal", "iv_fit", "variance_premium", "greek_fit", "quality", "cost_penalty", "extension_penalty"} <= set(s.risk["score_parts"])
    assert abs(sum(s.risk["score_parts"].values()) - s.score) < 0.2
    assert any("Gamma vs theta" in r for r in s.reasons) or s.structure.has_future


def test_suggest_degrades_gracefully():
    bars, prov = env(400)
    res = suggest("TEST", bars[:30], prov, bars[29]["date"], StrategyConfig(), MKT, CM)
    assert not res.suggestions and res.note


def test_pick_expiry_uses_nearest_future_expiry_without_a_day_window():
    asof = dt.date(2026, 1, 1)

    class Provider:
        def expiries(self, symbol, date, n):
            return [date, date + dt.timedelta(days=5), date + dt.timedelta(days=90)]

    assert pick_expiry(Provider(), "TEST", asof) == asof + dt.timedelta(days=5)

    class NoUpcomingExpiryProvider:
        def expiries(self, symbol, date, n):
            return [date]

    assert pick_expiry(NoUpcomingExpiryProvider(), "TEST", asof) is None


def test_suggest_can_return_ideas_for_the_nearest_two_expiries():
    bars, prov = env(500, seed=11)
    found = None
    for i in range(120, len(bars), 7):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], RELAXED, MKT, CM,
                      override_trend="bullish", expiry_count=2)
        if len({s.structure.expiry for s in res.suggestions}) == 2:
            found = res
            break
    assert found is not None, "expected suggestions for both selected expiries"
    assert len(found.expiries) == 2
    assert {s.structure.expiry for s in found.suggestions} == set(found.expiries)


def test_stretched_move_is_penalised_and_warned_about():
    bars, prov = env()
    asof = bars[-1]["date"]
    chain = prov.get_chain("TEST", asof, prov.expiries("TEST", asof, 2)[1])
    s = bull_call_spread(chain, RELAXED, MKT, CM)
    base = dict(spot=chain.spot, swing_low=chain.spot * 0.95)
    calm, _ = evaluate(s, chain, regime("bullish", 3, 25, rsi=60.0, **base), RELAXED, MKT, CM)
    hot, _ = evaluate(s, chain, regime("bullish", 3, 25, rsi=82.0, notes=["RSI 82 is overbought: trend-following longs are extended."], **base), RELAXED, MKT, CM)
    assert abs((calm.score - hot.score) - 5.0) < 0.2
    assert hot.risk["score_parts"]["extension_penalty"] == -5.0 and calm.risk["score_parts"]["extension_penalty"] == 0.0
    assert any("overbought" in w for w in hot.warnings) and any("stretched" in w for w in hot.warnings)
    assert not any("stretched" in w for w in calm.warnings)


def test_single_long_option_targets_a_multiple_of_premium_not_the_theoretical_cap():
    """Regression: a long put's max profit is finite (stock to zero), which once produced an unreachable target."""
    from fno.strategies import long_call, long_put
    bars, prov = env()
    asof = bars[-1]["date"]
    chain = prov.get_chain("TEST", asof, prov.expiries("TEST", asof, 2)[1])
    reg = regime("bearish", -3, 20, spot=chain.spot, swing_high=chain.spot * 1.05)
    cfg = StrategyConfig()
    for build in (long_put, long_call):
        s = build(chain, cfg, MKT, CM)
        debit = -s.net_premium()
        p = make_exit_plan(s, s.payoff_stats(), reg, cfg)
        assert abs(p.profit_target - cfg.long_option_profit_frac * debit) < 0.01, (s.name, p.profit_target, debit)
        assert abs(p.stop_loss + cfg.debit_stop_frac * debit) < 0.01
        sug, why = evaluate(s, chain, reg, RELAXED, MKT, CM)
        assert sug is not None, why
        assert sug.exit_plan.profit_target < 5 * debit             # reachable: nowhere near the theoretical cap


def test_every_outcome_has_a_reason_and_only_real_suggestions_are_ok():
    bars, prov = env(450, seed=9)
    seen = set()
    for i in range(70, len(bars), 9):
        res = suggest("TEST", bars[:i + 1], prov, bars[i]["date"], StrategyConfig(), MKT, CM)
        seen.add(res.code)
        assert (res.code == "ok") == bool(res.suggestions), (res.code, res.note)
        if res.code == "insufficient_history":
            assert res.have_bars == i + 1 and res.need_bars == 60
    short = suggest("TEST", bars[:30], prov, bars[29]["date"], StrategyConfig(), MKT, CM)
    assert short.code in ("insufficient_history", "no_chain") and not short.suggestions
    assert "ok" in seen or "filtered" in seen or "no_edge" in seen
