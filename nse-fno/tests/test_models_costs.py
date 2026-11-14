import datetime as dt
import math

from fno.config import CostModel
from fno.costs import entry_costs, exit_costs, fill_price, order_cost, roundtrip_estimate
from fno.expiry import last_weekday_of_month, monthly_expiries
from fno.config import MarketConfig
from fno.models import ExitPlan, Leg, Quote, Structure, Suggestion

EXP = dt.date(2026, 11, 24)
OPEN = dt.date(2026, 10, 24)


def struct(name, legs, lot=100):
    return Structure(name, "TEST", legs, lot, OPEN)


def test_bull_call_spread_payoff():
    s = struct("Bull call spread", [Leg("CE", +1, 100, EXP, 6.0, 0.3), Leg("CE", -1, 110, EXP, 2.0, 0.3)])
    st = s.payoff_stats()
    assert st["max_profit"] == 600 and st["max_loss"] == -400 and st["breakevens"] == [104.0] and st["defined_risk"]
    assert s.net_premium() == -400


def test_iron_condor_payoff():
    legs = [Leg("PE", -1, 95, EXP, 2.0, 0.3), Leg("PE", +1, 90, EXP, 0.8, 0.3),
            Leg("CE", -1, 105, EXP, 2.0, 0.3), Leg("CE", +1, 110, EXP, 0.8, 0.3)]
    s = struct("Iron condor", legs)
    st = s.payoff_stats()
    credit = (2.0 - 0.8) * 2 * 100
    assert abs(s.net_premium() - credit) < 1e-9
    assert abs(st["max_profit"] - credit) < 1e-9
    assert abs(st["max_loss"] - (credit - 5 * 100)) < 1e-9
    assert st["breakevens"] == [92.6, 107.4]


def test_long_call_unbounded_profit_short_future_unbounded_loss():
    lc = struct("Long call", [Leg("CE", +1, 100, EXP, 5.0, 0.3)])
    st = lc.payoff_stats()
    assert math.isinf(st["max_profit"]) and st["max_loss"] == -500
    sf = struct("Short future", [Leg("FUT", -1, None, EXP, 100.0)])
    st = sf.payoff_stats()
    assert math.isinf(st["max_loss"]) and not st["defined_risk"]


def test_net_greeks_signs():
    spot = 100.0
    lc = struct("Long call", [Leg("CE", +1, 100, EXP, 5.0, 0.3)], lot=1)
    g = lc.net_greeks(spot, OPEN, 0.065, 0.0)
    assert g["delta"] > 0 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0
    sc = struct("Short call", [Leg("CE", -1, 100, EXP, 5.0, 0.3)], lot=1)
    g2 = sc.net_greeks(spot, OPEN, 0.065, 0.0)
    assert abs(g2["delta"] + g["delta"]) < 1e-12 and g2["theta"] > 0 and g2["vega"] < 0


def test_structure_round_trip_and_scaling():
    s = struct("Bull call spread", [Leg("CE", +1, 100, EXP, 6.0, 0.3), Leg("CE", -1, 110, EXP, 2.0, 0.3)])
    again = Structure.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()
    big = s.scaled(3)
    assert big.payoff_stats()["max_profit"] == 3 * s.payoff_stats()["max_profit"]


def test_suggestion_round_trip_keeps_unbounded_flags():
    s = struct("Long call", [Leg("CE", +1, 100, EXP, 5.0, 0.3)])
    st = s.payoff_stats()
    sug = Suggestion("abc", "TEST", OPEN, s, ExitPlan(1, -1, 5, ("below", 90.0)), 70.0, ["r"],
                     {"max_profit": st["max_profit"], "max_loss": st["max_loss"], "net_premium": -500.0}, {"delta": 1.0}, None,
                     {"trend": "bullish"}, 31)
    back = Suggestion.from_dict(sug.to_dict())
    assert math.isinf(back.risk["max_profit"]) and back.risk["max_loss"] == -500 and back.exit_plan.invalidation == ("below", 90.0)


def test_option_order_costs_match_hand_calculation():
    cm = CostModel()
    # sell 1000 units at Rs 100: turnover 100,000
    sell = order_cost("CE", -1, 100.0, 1000, cm)
    expected = 20 + 150 + 35.03 + 0.1 + 0.18 * (20 + 35.03 + 0.1)        # brokerage + STT 0.15% + exch + SEBI + GST
    assert abs(sell - expected) < 0.05, (sell, expected)
    buy = order_cost("CE", +1, 100.0, 1000, cm)
    expected_buy = 20 + 35.03 + 0.1 + 3.0 + 0.18 * (20 + 35.03 + 0.1)    # no STT on buys, stamp 0.003%
    assert abs(buy - expected_buy) < 0.05


def test_futures_and_exercise_costs():
    cm = CostModel()
    fut_sell = order_cost("FUT", -1, 1000.0, 100, cm)                    # turnover 100,000, STT 0.05% = 50
    assert 50 < fut_sell < 120
    assert order_cost("CE", -1, 20.0, 1000, cm, exercised_intrinsic=20.0) == 30.0    # 20,000 x 0.15%


def test_fill_price_pays_the_spread():
    cm = CostModel()
    q = Quote("CE", 100, EXP, 10.0, 9.8, 10.2)
    assert fill_price(q, +1, cm) == 10.2 and fill_price(q, -1, cm) == 9.8
    half = CostModel(slippage_half_spread_frac=0.5)
    assert fill_price(q, +1, half) == 10.1


def test_roundtrip_estimate_is_entry_plus_exit():
    cm = CostModel()
    s = struct("Bull call spread", [Leg("CE", +1, 100, EXP, 6.0, 0.3), Leg("CE", -1, 110, EXP, 2.0, 0.3)])
    assert abs(roundtrip_estimate(s, cm) - (entry_costs(s, cm) + exit_costs(s, [6.0, 2.0], cm))) < 0.01


def test_monthly_expiry_is_last_tuesday_and_rolls_back_for_holidays():
    assert last_weekday_of_month(2026, 10, 1) == dt.date(2026, 10, 27)
    assert monthly_expiries(dt.date(2026, 10, 24), 2)[:2] == [dt.date(2026, 10, 27), dt.date(2026, 11, 24)]
    m = MarketConfig(holidays=frozenset({dt.date(2026, 10, 27)}))
    assert monthly_expiries(dt.date(2026, 10, 24), 1, m) == [dt.date(2026, 10, 26)]
    assert monthly_expiries(dt.date(2026, 10, 28), 1)[0] == dt.date(2026, 11, 24)
