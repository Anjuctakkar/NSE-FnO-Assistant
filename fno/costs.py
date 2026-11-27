"""Transaction costs and fills. Every order pays slippage (a fraction of the half bid-ask spread) and statutory
charges. Costs matter far more in options than in cash: a 4-leg condor pays 8 orders' brokerage and the
spread on every leg, so costs are always included in backtests."""
from __future__ import annotations

import math
from typing import Optional

from .config import CostModel
from .models import Leg, Quote, Structure


def fill_price(quote: Optional[Quote], side: int, cm: CostModel, fallback_mid: Optional[float] = None) -> float:
    """Price actually obtained when trading at `quote`. Buys pay above mid, sells receive below it. Uses the real
    bid/ask when present, else cm.assumed_spread_pct of mid but never less than cm.min_assumed_spread_ticks ticks wide
    (a 3% spread on a Rs 1 wing is under one tick, which does not exist). The fill snaps to the tick in the adverse
    direction: buys round up, sells round down. Never below one tick."""
    mid = quote.mid if quote is not None else (fallback_mid or 0.0)
    if quote is not None and quote.bid and quote.ask and quote.ask >= quote.bid > 0:
        half = (quote.ask - quote.bid) / 2.0
    else:
        half = max(mid * cm.assumed_spread_pct / 200.0, cm.min_assumed_spread_ticks * cm.tick / 2.0)
    px = mid + side * half * cm.slippage_half_spread_frac
    t = cm.tick
    px = (math.ceil(px / t - 1e-9) if side > 0 else math.floor(px / t + 1e-9)) * t
    return round(max(px, t), 2)


def future_fill(price: float, side: int, cm: CostModel) -> float:
    return round(price * (1 + side * cm.futures_slippage_pct / 100.0), 2)


def order_cost(kind: str, side: int, price: float, units: int, cm: CostModel, exercised_intrinsic: float = 0.0) -> float:
    """All statutory and broker charges for one executed order, in rupees.
    exercised_intrinsic > 0 charges the exercise STT instead (a long ITM option settled at expiry: no order, no
    brokerage, STT on intrinsic value)."""
    if exercised_intrinsic > 0:
        return round(exercised_intrinsic * units * cm.stt_option_exercise_pct / 100.0, 2)
    turnover = price * units
    fut = kind == "FUT"
    brokerage = cm.brokerage_per_order
    stt = turnover * ((cm.stt_futures_sell_pct if fut else cm.stt_option_sell_pct) / 100.0) if side < 0 else 0.0
    exch = turnover * (cm.exch_futures_pct if fut else cm.exch_option_pct) / 100.0
    sebi = turnover * cm.sebi_per_crore / 1e7
    stamp = turnover * (cm.stamp_buy_futures_pct if fut else cm.stamp_buy_option_pct) / 100.0 if side > 0 else 0.0
    gst = (brokerage + exch + sebi) * cm.gst_pct / 100.0
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)


def entry_costs(s: Structure, cm: CostModel) -> float:
    return round(sum(order_cost(l.kind, l.side, l.entry_price, s.units(l), cm) for l in s.legs), 2)


def exit_costs(s: Structure, exit_prices: list[float], cm: CostModel) -> float:
    """Closing every leg (the opposite side of its entry) at the given prices."""
    return round(sum(order_cost(l.kind, -l.side, p, s.units(l), cm) for l, p in zip(s.legs, exit_prices)), 2)


def roundtrip_estimate(s: Structure, cm: CostModel) -> float:
    """Entry costs plus exit costs assuming every leg closes at its entry price. A planning figure."""
    return round(entry_costs(s, cm) + exit_costs(s, [l.entry_price for l in s.legs], cm), 2)
