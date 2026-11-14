"""
Strategy builders. Each takes one expiry's Chain and returns a Structure (legs filled at realistic prices after
slippage) or None when the chain cannot support it. Strikes are chosen by absolute delta, so the same rule works
for a Rs 300 stock and a Rs 4,000 stock and adapts to the current IV level:

    long call / put        long leg at ~0.60 delta (some intrinsic value, less theta per rupee of premium)
    bull call / bear put   long ~0.60, short ~0.30  (debit spread: cheaper, caps profit, cuts vega and theta)
    bull put / bear call   short ~0.25, long ~0.10  (credit spread: defined risk, positive theta, short vega)
    iron condor            short ~0.20 strangle with ~0.08 wings (credit, range-bound view)
    futures                +1 / -1 lot at the futures price (risk is the stop, not the structure)

Only defined-risk option structures are built. Naked short options are deliberately not offered.
"""
from __future__ import annotations

from typing import Callable, Optional

from . import greeks as bs
from .config import CostModel, MarketConfig, StrategyConfig
from .costs import fill_price, future_fill
from .models import Chain, Leg, Quote, Structure


def pick_by_delta(chain: Chain, kind: str, target: float, mkt: MarketConfig, otm_only: bool = False) -> Optional[Quote]:
    """The quote of `kind` whose absolute delta is closest to `target`."""
    T = max((chain.expiry - chain.asof).days, 0) / 365.0
    best, best_gap = None, 9.0
    for q in chain.quotes:
        if q.kind != kind or not q.iv:
            continue
        if otm_only and ((kind == "CE" and q.strike < chain.spot) or (kind == "PE" and q.strike > chain.spot)):
            continue
        d = abs(bs.greeks(chain.spot, q.strike, T, mkt.risk_free, mkt.div_yield, q.iv, kind).delta)
        if abs(d - target) < best_gap:
            best, best_gap = q, abs(d - target)
    return best


def _leg(chain: Chain, q: Quote, side: int, cm: CostModel, lots: int) -> Leg:
    return Leg(q.kind, side, q.strike, chain.expiry, fill_price(q, side, cm), q.iv, lots)


def _build(name: str, chain: Chain, legs: list[Leg]) -> Structure:
    return Structure(name, chain.symbol, legs, chain.lot_size, chain.asof)


def long_call(chain, cfg: StrategyConfig, mkt: MarketConfig, cm: CostModel, lots: int = 1):
    q = pick_by_delta(chain, "CE", cfg.long_leg_delta, mkt)
    return _build("Long call", chain, [_leg(chain, q, +1, cm, lots)]) if q else None


def long_put(chain, cfg, mkt, cm, lots: int = 1):
    q = pick_by_delta(chain, "PE", cfg.long_leg_delta, mkt)
    return _build("Long put", chain, [_leg(chain, q, +1, cm, lots)]) if q else None


def bull_call_spread(chain, cfg, mkt, cm, lots: int = 1):
    lo, sh = pick_by_delta(chain, "CE", cfg.long_leg_delta, mkt), pick_by_delta(chain, "CE", cfg.short_leg_delta, mkt)
    if not lo or not sh or sh.strike <= lo.strike:
        return None
    return _build("Bull call spread", chain, [_leg(chain, lo, +1, cm, lots), _leg(chain, sh, -1, cm, lots)])


def bear_put_spread(chain, cfg, mkt, cm, lots: int = 1):
    lo, sh = pick_by_delta(chain, "PE", cfg.long_leg_delta, mkt), pick_by_delta(chain, "PE", cfg.short_leg_delta, mkt)
    if not lo or not sh or sh.strike >= lo.strike:
        return None
    return _build("Bear put spread", chain, [_leg(chain, lo, +1, cm, lots), _leg(chain, sh, -1, cm, lots)])


def bull_put_spread(chain, cfg, mkt, cm, lots: int = 1):
    sh, wing = pick_by_delta(chain, "PE", cfg.credit_short_delta, mkt, True), pick_by_delta(chain, "PE", cfg.credit_long_delta, mkt, True)
    if not sh or not wing or wing.strike >= sh.strike:
        return None
    return _build("Bull put spread", chain, [_leg(chain, sh, -1, cm, lots), _leg(chain, wing, +1, cm, lots)])


def bear_call_spread(chain, cfg, mkt, cm, lots: int = 1):
    sh, wing = pick_by_delta(chain, "CE", cfg.credit_short_delta, mkt, True), pick_by_delta(chain, "CE", cfg.credit_long_delta, mkt, True)
    if not sh or not wing or wing.strike <= sh.strike:
        return None
    return _build("Bear call spread", chain, [_leg(chain, sh, -1, cm, lots), _leg(chain, wing, +1, cm, lots)])


def iron_condor(chain, cfg, mkt, cm, lots: int = 1):
    sp, wp = pick_by_delta(chain, "PE", cfg.condor_short_delta, mkt, True), pick_by_delta(chain, "PE", cfg.condor_long_delta, mkt, True)
    sc, wc = pick_by_delta(chain, "CE", cfg.condor_short_delta, mkt, True), pick_by_delta(chain, "CE", cfg.condor_long_delta, mkt, True)
    if not (sp and wp and sc and wc) or not (wp.strike < sp.strike < sc.strike < wc.strike):
        return None
    return _build("Iron condor", chain, [_leg(chain, sp, -1, cm, lots), _leg(chain, wp, +1, cm, lots),
                                         _leg(chain, sc, -1, cm, lots), _leg(chain, wc, +1, cm, lots)])


def _future(name: str, side: int, chain, cm: CostModel, lots: int):
    px = chain.futures_price or chain.spot
    return _build(name, chain, [Leg("FUT", side, None, chain.expiry, future_fill(px, side, cm), None, lots)])


def long_future(chain, cfg, mkt, cm, lots: int = 1):
    return _future("Long future", +1, chain, cm, lots)


def short_future(chain, cfg, mkt, cm, lots: int = 1):
    return _future("Short future", -1, chain, cm, lots)


BUILDERS: dict[str, Callable] = {
    "Long call": long_call, "Long put": long_put, "Bull call spread": bull_call_spread, "Bear put spread": bear_put_spread,
    "Bull put spread": bull_put_spread, "Bear call spread": bear_call_spread, "Iron condor": iron_condor,
    "Long future": long_future, "Short future": short_future,
}

CREDIT = {"Bull put spread", "Bear call spread", "Iron condor"}
DEBIT = {"Long call", "Long put", "Bull call spread", "Bear put spread"}
FUTURES = {"Long future", "Short future"}
