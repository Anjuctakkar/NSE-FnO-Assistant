import datetime as dt

from fno.models import Chain, Leg, Quote, Structure
from fno.oi import OiContext, classify_buildup, max_pain, oi_adjust, oi_profile

D, EX = dt.date(2026, 10, 1), dt.date(2026, 10, 27)
CE_OI = {85: 100, 90: 200, 95: 500, 100: 2500, 105: 3000, 110: 8000, 115: 1000}
PE_OI = {85: 500, 90: 2000, 95: 9000, 100: 2600, 105: 300, 110: 100, 115: 50}


def chain(ce=CE_OI, pe=PE_OI, spot=100.0):
    qs = [Quote("CE", float(k), EX, 5.0, None, None, 0.3, o, 10) for k, o in ce.items()] + [Quote("PE", float(k), EX, 5.0, None, None, 0.3, o, 10) for k, o in pe.items()]
    return Chain("T", D, spot, EX, 100, qs, 100.4)


def st(name, legs, lot=1):
    return Structure(name, "T", [Leg(k, s, None if K is None else float(K), EX, p, 0.3, 1) for k, s, K, p in legs], lot, D)


def test_buildup_quadrants_and_noise_thresholds():
    assert classify_buildup(0.02, 0.08) == ("long_buildup", "strong")
    assert classify_buildup(0.005, 0.02) == ("long_buildup", "moderate")
    assert classify_buildup(-0.02, 0.08) == ("short_buildup", "strong")
    assert classify_buildup(0.02, -0.08) == ("short_covering", "strong")
    assert classify_buildup(-0.005, -0.02) == ("long_unwinding", "moderate")
    assert classify_buildup(0.001, 0.2) == (None, None)            # price barely moved: noise
    assert classify_buildup(0.05, 0.005) == (None, None)           # OI barely moved: noise
    assert classify_buildup(None, 0.1) == (None, None)


def test_profile_totals_pcr_walls_and_change_against_yesterday():
    prev = {("PE", 95.0): 7500, ("CE", 110.0): 8000}
    p = oi_profile(chain(), OiContext(100.4, 1_060_000, 99.0, 1_000_000, prev))
    assert p.call_oi == sum(CE_OI.values()) and p.put_oi == sum(PE_OI.values())
    assert abs(p.pcr - sum(PE_OI.values()) / sum(CE_OI.values())) < 1e-9
    assert p.call_wall["strike"] == 110.0 and p.call_wall["oi"] == 8000 and abs(p.call_wall["share"] - 8000 / sum(CE_OI.values())) < 1e-3
    assert p.put_wall["strike"] == 95.0 and abs(p.put_wall["change_pct"] - (9000 - 7500) / 7500) < 1e-3          # support was added today
    assert p.call_wall["change_pct"] == 0.0
    assert p.buildup == "long_buildup" and p.buildup_strength == "strong" and abs(p.fut_oi_chg - 0.06) < 1e-9      # +1.4% price, +6% OI
    d = p.to_dict()
    assert d["buildup_label"] == "long build-up" and d["put_wall"]["strike"] == 95.0


def test_walls_only_count_on_the_correct_side_of_spot():
    only_far = chain(ce={115: 9999, 100: 10}, pe={85: 9999, 100: 10})
    p = oi_profile(only_far)
    assert p.call_wall["strike"] == 115.0 and p.put_wall["strike"] == 85.0
    below_spot_calls = chain(ce={85: 9999}, pe={95: 5})                # all call OI is below spot: no resistance wall
    assert oi_profile(below_spot_calls).call_wall is None


def test_max_pain_matches_a_brute_force_calculation():
    c = chain()
    strikes = sorted(CE_OI)
    def pain(K):
        return sum(max(K - k, 0) * o for k, o in CE_OI.items()) + sum(max(k - K, 0) * o for k, o in PE_OI.items())
    best = min(strikes, key=pain)
    assert max_pain(c) == float(best) and 90 <= best <= 110
    assert pain(best) <= min(pain(k) for k in strikes)


def test_no_oi_means_no_profile_and_no_adjustment():
    none = chain(ce={100: 0}, pe={100: 0})
    assert oi_profile(none) is None
    s = st("Bull call spread", [("CE", 1, 100, 6.0), ("CE", -1, 110, 2.0)])
    assert oi_adjust(s, chain(), None) == (0.0, [], {})


def prof(**kw):
    p = oi_profile(chain())
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_buildup_supports_matching_direction_and_contradicts_the_opposite():
    p = prof(buildup="long_buildup", buildup_strength="strong", fut_price_chg=0.02, fut_oi_chg=0.08)
    bull = st("Bull put spread", [("PE", -1, 95, 3.0), ("PE", 1, 90, 1.0)])
    bear = st("Bear call spread", [("CE", -1, 110, 3.0), ("CE", 1, 115, 1.0)])
    condor = st("Iron condor", [("PE", -1, 95, 2.0), ("PE", 1, 90, 1.0), ("CE", -1, 110, 2.0), ("CE", 1, 115, 1.0)])
    for s, expect in ((bull, 6.0), (bear, -6.0), (condor, -3.0)):
        _, why, parts = oi_adjust(s, chain(), p)
        assert parts["buildup"] == expect, (s.name, parts)
        assert any("long build-up" in w for w in why)
    weaker = prof(buildup="short_covering", buildup_strength="moderate", fut_price_chg=0.005, fut_oi_chg=-0.02)
    assert oi_adjust(bull, chain(), weaker)[2]["buildup"] == 2.0         # covering counts for less than a build-up


def test_pcr_tilts_directional_trades_and_prefers_balance_for_range_trades():
    bull = st("Bull put spread", [("PE", -1, 95, 3.0), ("PE", 1, 90, 1.0)])
    bear = st("Bear call spread", [("CE", -1, 110, 3.0), ("CE", 1, 115, 1.0)])
    condor = st("Iron condor", [("PE", -1, 95, 2.0), ("PE", 1, 90, 1.0), ("CE", -1, 110, 2.0), ("CE", 1, 115, 1.0)])
    assert oi_adjust(bull, chain(), prof(pcr=1.31))[2]["pcr"] == 3.0 and oi_adjust(bear, chain(), prof(pcr=1.31))[2]["pcr"] == -3.0
    assert oi_adjust(bull, chain(), prof(pcr=0.6))[2]["pcr"] == -3.0 and oi_adjust(bear, chain(), prof(pcr=0.6))[2]["pcr"] == 3.0
    assert oi_adjust(condor, chain(), prof(pcr=1.0))[2]["pcr"] == 2.0 and oi_adjust(condor, chain(), prof(pcr=1.6))[2]["pcr"] == -1.0


def test_credit_spreads_want_a_wall_between_the_stock_and_the_short_strike():
    p = prof()                                                           # put wall 95, call wall 110
    protected = st("Bull put spread", [("PE", -1, 95, 3.0), ("PE", 1, 90, 1.0)])
    exposed = st("Bull put spread", [("PE", -1, 100, 5.0), ("PE", 1, 95, 3.0)])
    assert oi_adjust(protected, chain(), p)[2]["wall"] == 4.0 and oi_adjust(exposed, chain(), p)[2]["wall"] == -2.0
    call_ok = st("Bear call spread", [("CE", -1, 110, 3.0), ("CE", 1, 115, 1.0)])
    call_bad = st("Bear call spread", [("CE", -1, 105, 4.0), ("CE", 1, 110, 3.0)])
    assert oi_adjust(call_ok, chain(), p)[2]["wall"] == 4.0 and oi_adjust(call_bad, chain(), p)[2]["wall"] == -2.0


def test_condor_scores_by_how_many_short_strikes_sit_beyond_a_wall():
    p = prof()
    both = st("Iron condor", [("PE", -1, 95, 2.0), ("PE", 1, 90, 1.0), ("CE", -1, 110, 2.0), ("CE", 1, 115, 1.0)])
    one = st("Iron condor", [("PE", -1, 95, 2.0), ("PE", 1, 90, 1.0), ("CE", -1, 105, 3.0), ("CE", 1, 110, 2.0)])
    none = st("Iron condor", [("PE", -1, 100, 4.0), ("PE", 1, 95, 2.0), ("CE", -1, 105, 3.0), ("CE", 1, 110, 2.0)])
    assert [oi_adjust(s, chain(), p)[2]["wall"] for s in (both, one, none)] == [6.0, 2.0, -3.0]
    assert "max_pain" in oi_adjust(both, chain(), p)[2]


def test_debit_trades_are_penalised_when_a_wall_sits_before_the_breakeven():
    bcs = st("Bull call spread", [("CE", 1, 100, 6.0), ("CE", -1, 110, 2.0)])          # breakeven 104
    assert oi_adjust(bcs, chain(), prof(call_wall={"strike": 102.0, "oi": 9000, "share": 0.5, "change_pct": None}))[2]["wall"] == -4.0
    assert oi_adjust(bcs, chain(), prof())[2]["wall"] == 2.0                           # short call 110 is at the wall 110
    bpp = st("Bear put spread", [("PE", 1, 100, 6.0), ("PE", -1, 90, 2.0)])           # breakeven 96
    assert oi_adjust(bpp, chain(), prof(put_wall={"strike": 98.0, "oi": 9000, "share": 0.5, "change_pct": None}))[2]["wall"] == -4.0


def test_futures_use_atr_to_decide_whether_a_wall_is_in_the_way():
    long_f = st("Long future", [("FUT", 1, None, 100.4)])
    near_wall = prof(call_wall={"strike": 105.0, "oi": 9000, "share": 0.5, "change_pct": None})
    assert oi_adjust(long_f, chain(), near_wall, atr=2.0)[2]["wall"] == -3.0           # target = 100 + 3 x 2 = 106, wall at 105
    assert "wall" not in oi_adjust(long_f, chain(), near_wall, atr=0.0)[2]


def test_total_is_the_sum_of_its_parts_and_capped():
    p = prof(buildup="long_buildup", buildup_strength="strong", fut_price_chg=0.02, fut_oi_chg=0.08, pcr=1.5)
    s = st("Bull put spread", [("PE", -1, 95, 3.0), ("PE", 1, 90, 1.0)])
    pts, why, parts = oi_adjust(s, chain(), p)
    assert pts == sum(parts.values()) == 13.0 and len(why) >= 3 and all(isinstance(w, str) and w for w in why)
    assert all(abs(v) <= 20 for v in (pts,))


def test_a_strike_exactly_at_the_edge_counts_and_a_sliver_of_oi_is_not_a_wall():
    """Regression: 100 * 1.15 == 114.99999999999999 once dropped a strike sitting exactly 15% away."""
    edge = oi_profile(chain(ce={115: 9999, 100: 10}, pe={85: 9999, 100: 10}))
    assert edge.call_wall["strike"] == 115.0 and edge.put_wall["strike"] == 85.0
    spread_thin = chain(ce={101: 100, 105: 100, 108: 100, 110: 100, 112: 100, 114: 100, 100: 100, 106: 100, 107: 100, 109: 100, 111: 100, 113: 100}, pe={95: 5000})
    assert oi_profile(spread_thin).call_wall is None                       # 12 strikes with 1/12 each: no strike dominates
