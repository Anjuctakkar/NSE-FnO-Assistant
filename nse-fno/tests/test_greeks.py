import math

from fno import greeks as bs

S, K, T, R, Q, SIG = 100.0, 100.0, 1.0, 0.05, 0.0, 0.20


def near(a, b, tol):
    assert abs(a - b) <= tol, f"{a} vs {b} (tol {tol})"


def test_reference_values_call():
    g = bs.greeks(S, K, T, R, Q, SIG, "CE")
    near(g.price, 10.4506, 1e-3)
    near(g.delta, 0.6368, 1e-3)
    near(g.gamma, 0.018762, 1e-5)
    near(g.vega, 0.37524, 1e-4)            # per vol point
    near(g.theta * 365, -6.4140, 2e-3)     # per day * 365 = annual


def test_reference_values_put():
    g = bs.greeks(S, K, T, R, Q, SIG, "PE")
    near(g.price, 5.5735, 1e-3)
    near(g.delta, -0.3632, 1e-3)
    near(g.theta * 365, -1.6579, 2e-3)


def test_put_call_parity_with_dividend():
    q = 0.02
    c, p = bs.bs_price(100, 95, 0.4, 0.06, q, 0.3, "CE"), bs.bs_price(100, 95, 0.4, 0.06, q, 0.3, "PE")
    near(c - p, 100 * math.exp(-q * 0.4) - 95 * math.exp(-0.06 * 0.4), 1e-9)


def test_greeks_match_finite_differences():
    h = 0.01
    up, dn = bs.bs_price(S + h, K, T, R, Q, SIG, "CE"), bs.bs_price(S - h, K, T, R, Q, SIG, "CE")
    mid = bs.bs_price(S, K, T, R, Q, SIG, "CE")
    g = bs.greeks(S, K, T, R, Q, SIG, "CE")
    near((up - dn) / (2 * h), g.delta, 1e-6)
    near((up - 2 * mid + dn) / (h * h), g.gamma, 1e-4)
    near(bs.bs_price(S, K, T, R, Q, SIG + 0.01, "CE") - mid, g.vega, 5e-3)                 # one vol point
    near(bs.bs_price(S, K, T - 1 / 365, R, Q, SIG, "CE") - mid, g.theta, 5e-4)             # one calendar day


def test_implied_vol_round_trip():
    for kind in ("CE", "PE"):
        for k in (80, 100, 125):
            p = bs.bs_price(100, k, 0.2, 0.065, 0.0, 0.37, kind)
            iv = bs.implied_vol(p, 100, k, 0.2, 0.065, 0.0, kind)
            near(iv, 0.37, 1e-4)


def test_implied_vol_none_below_intrinsic():
    assert bs.implied_vol(1.0, 100, 90, 0.1, 0.065, 0.0, "CE") is None      # intrinsic alone is ~10


def test_expiry_limits():
    g = bs.greeks(105, 100, 0.0, R, Q, SIG, "CE")
    assert g.price == 5 and g.delta == 1.0 and g.gamma == 0 and g.theta == 0
    assert bs.bs_price(95, 100, 0.0, R, Q, SIG, "PE") == 5


def test_prob_above_is_monotonic():
    ps = [bs.prob_above(100, lvl, 0.25, 0.065, 0.0, 0.3) for lvl in (80, 100, 120)]
    assert ps[0] > ps[1] > ps[2]
