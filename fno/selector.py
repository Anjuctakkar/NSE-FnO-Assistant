"""
The selector turns "what is the stock doing, and is option premium cheap or rich" into ranked, explained F&O
trade suggestions.

    1. Regime         trend score (-4..+4) from technicals; IV rank, IV percentile, IV minus realised vol.
    2. Candidates     a fixed rule matrix (below) names the structures worth considering.
    3. Build + price  strikes by delta, fills after slippage, sized by a rupee risk budget if one is set.
    4. Hard filters   liquidity, days to expiry, credit/width, probability of profit, theta burn, reward:risk.
    5. Score (0-100)  signal fit 25, IV fit 25, variance premium 15, Greek fit 20, trade quality 15,
                      minus a cost-drag penalty (up to 15) and 5 for chasing a stretched move (RSI > 75 / < 25), plus or minus up to 20
                      for open interest (futures build-up, put/call ratio, OI walls, max pain; 0 when there is no OI data).
    6. Exit plan      profit target, stop, time exit, and a technical invalidation level.

    Rule matrix                    IV rank low (<= iv_low)       IV middle            IV high (>= iv_high)
    bullish                        bull call spread, long call   bull call / bull put bull put spread
    bearish                        bear put spread, long put     bear put / bear call bear call spread
    neutral                        no trade                      no trade             iron condor
    strong trend (|score| >= 3)    + futures, any IV

The Greeks do real work here, not decoration:
  * theta is what a long-premium trade pays each day: reject if it burns more than max_theta_decay_pct_day of the debit.
  * vega says which side of IV the trade is on: credit structures are short vega and want high IV rank, debit
    structures are long vega and want low IV rank. The IV-fit score encodes this.
  * gamma and theta are two sides of one trade. The position's break-even daily move is sqrt(2 |theta| / |gamma|):
    a short-gamma trade is paid theta to absorb moves smaller than that, a long-gamma trade needs bigger ones.
    Greek fit compares that figure with the stock's recent daily move (20-day realised volatility).
  * delta checks that the structure expresses the view (and how many underlying units it is worth).

The probability of profit is MODEL-IMPLIED (risk-neutral, from ATM IV). It is not a forecast, and a structure priced
fairly has about zero expected value before costs: any edge has to come from the signal or the volatility view.
"""
from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import greeks as bs
from .config import CostModel, MarketConfig, ProbConfig, StrategyConfig
from .costs import roundtrip_estimate
from .models import Chain, ExitPlan, Structure, Suggestion
from .oi import OiProfile, oi_adjust, oi_profile
from . import probability as prob
from .signals import Regime, compute_regime
from .strategies import BUILDERS, CREDIT, DEBIT, FUTURES

SINGLE_LONG = {"Long call", "Long put"}     # profit is open-ended in practice (a put's cap is the stock going to zero)
BULLISH = {"Long call", "Bull call spread", "Bull put spread", "Long future"}
BEARISH = {"Long put", "Bear put spread", "Bear call spread", "Short future"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ probability model
def structure_pop(s: Structure, spot: float, T: float, r: float, q: float, sigma: float, n: int = 801) -> tuple[float, float]:
    """(probability of profit, model expected P&L) if held to expiry, integrating the structure's payoff over a
    lognormal distribution with ONE volatility `sigma`. Risk-neutral drift; gross of costs. Kept for the risk-neutral
    expected value; the probability the selector reports comes from probability.analytic_probs (skew-aware, net of costs,
    real-world) and probability.managed_mc (the trade as managed)."""
    sd = sigma * math.sqrt(T)
    F = spot * math.exp((r - q) * T)
    dz = 10.0 / (n - 1)
    tw = pop = ev = 0.0
    for i in range(n):
        z = -5.0 + i * dz
        w = bs.norm_pdf(z) * dz
        pnl = s.payoff(F * math.exp(-0.5 * sd * sd + sd * z))
        tw += w
        if pnl > 0:
            pop += w
        ev += pnl * w
    return pop / tw, ev / tw


# ------------------------------------------------------------------ candidates
def candidate_names(regime: Regime, cfg: StrategyConfig) -> list[str]:
    ivr = 50.0 if regime.iv_rank is None else regime.iv_rank
    strong = abs(regime.trend_score) >= cfg.futures_min_trend_score
    names: list[str] = []
    if regime.trend == "bullish":
        if ivr <= cfg.iv_low:
            names += ["Bull call spread"] + (["Long call"] if strong else [])
        elif ivr >= cfg.iv_high:
            names += ["Bull put spread"]
        else:
            names += ["Bull call spread", "Bull put spread"]
        if cfg.allow_futures and strong:
            names.append("Long future")
    elif regime.trend == "bearish":
        if ivr <= cfg.iv_low:
            names += ["Bear put spread"] + (["Long put"] if strong else [])
        elif ivr >= cfg.iv_high:
            names += ["Bear call spread"]
        else:
            names += ["Bear put spread", "Bear call spread"]
        if cfg.allow_futures and strong:
            names.append("Short future")
    elif ivr >= cfg.iv_high:
        names.append("Iron condor")
    return names


def make_exit_plan(s: Structure, stats: dict[str, Any], regime: Regime, cfg: StrategyConfig) -> ExitPlan:
    mp, ml, prem = stats["max_profit"], stats["max_loss"], s.net_premium()
    inv: Optional[tuple[str, float]] = None
    if s.name in FUTURES:
        leg = s.legs[0]
        units = s.units(leg)
        pt, sl = cfg.futures_target_atr * regime.atr * units, -cfg.futures_stop_atr * regime.atr * units
        inv = ("below", leg.entry_price - cfg.futures_stop_atr * regime.atr) if leg.side > 0 else \
              ("above", leg.entry_price + cfg.futures_stop_atr * regime.atr)
        return ExitPlan(round(pt, 2), round(sl, 2), cfg.exit_dte, inv)
    if s.name in CREDIT:
        pt, sl = cfg.credit_profit_frac * mp, -min(-ml, cfg.credit_stop_mult * prem)
    else:
        debit = -prem
        pt = cfg.long_option_profit_frac * debit if s.name in SINGLE_LONG else cfg.debit_profit_frac * mp
        sl = -cfg.debit_stop_frac * debit
    # The invalidation level is the nearest swing extreme, but never inside cfg.invalidation_min_atr ATRs of the spot: a swing
    # low a fraction of an ATR below the price is within one day's noise and turns the rule into a coin-flip stop.
    if s.name in BULLISH:
        inv = ("below", round(min(regime.swing_low, regime.spot - cfg.invalidation_min_atr * regime.atr), 2))
    elif s.name in BEARISH:
        inv = ("above", round(max(regime.swing_high, regime.spot + cfg.invalidation_min_atr * regime.atr), 2))
    return ExitPlan(round(pt, 2), round(sl, 2), cfg.exit_dte, inv)


# ------------------------------------------------------------------ evaluation of one structure
def evaluate(s1: Structure, chain: Chain, regime: Regime, cfg: StrategyConfig, mkt: MarketConfig, cm: CostModel,
             provider_name: str = "", oi: Optional[OiProfile] = None,
             periods_per_year: int = 252, pc: ProbConfig = ProbConfig()) -> tuple[Optional[Suggestion], Optional[str]]:
    """Filter, size, score and explain one candidate (built for 1 lot). Returns (suggestion, None) or (None, reason)."""
    name = s1.name
    stats1 = s1.payoff_stats()
    plan1 = make_exit_plan(s1, stats1, regime, cfg)

    lots = 1
    if cfg.risk_budget:
        risk1 = -stats1["max_loss"] if stats1["defined_risk"] else abs(plan1.stop_loss)
        lots = int(cfg.risk_budget // risk1) if risk1 > 0 else 0
        if lots < 1:
            return None, f"one lot risks Rs {risk1:,.0f}, above the Rs {cfg.risk_budget:,.0f} risk budget"
    s = s1.scaled(lots) if lots != 1 else s1
    stats = s.payoff_stats() if lots != 1 else stats1
    plan = make_exit_plan(s, stats, regime, cfg) if lots != 1 else plan1

    dte = (chain.expiry - chain.asof).days
    T = dte / 365.0
    r, q = mkt.risk_free, mkt.div_yield
    iv = chain.atm_iv() or regime.iv or 0.30
    prem, mp, ml = s.net_premium(), stats["max_profit"], stats["max_loss"]
    g = s.net_greeks(chain.spot, chain.asof, r, q)
    is_fut = name in FUTURES
    cost = roundtrip_estimate(s, cm)
    reasons: list[str] = []

    # ---- hard filters
    if not is_fut:
        for leg in s.legs:
            qt = chain.get(leg.kind, leg.strike)
            if qt is None:
                return None, f"no quote for {leg.kind} {leg.strike:g}"
            if qt.spread_pct is not None and qt.spread_pct > cfg.max_spread_pct:
                return None, f"{leg.kind} {leg.strike:g} bid-ask spread {qt.spread_pct:.1f}% > {cfg.max_spread_pct:g}%"
            if qt.oi is not None and qt.oi < cfg.min_oi:
                return None, f"{leg.kind} {leg.strike:g} open interest {qt.oi} < {cfg.min_oi}"
            if qt.volume is not None and qt.volume < cfg.min_volume:
                return None, f"{leg.kind} {leg.strike:g} traded {qt.volume} contracts, below {cfg.min_volume} (stale settlement price)"
    if name in CREDIT:
        if dte < cfg.min_dte_credit:
            return None, f"{dte} days to expiry is inside the {cfg.min_dte_credit}-day short-gamma window"
        if prem <= 0:
            return None, "no net credit after slippage"
        width = prem - ml
        if prem / width < cfg.min_credit_to_width:
            return None, f"credit is {prem / width:.0%} of width, below {cfg.min_credit_to_width:.0%}"
    if name in DEBIT:
        debit = -prem
        if debit <= 0:
            return None, "no net debit (pricing inconsistency)"
        decay = abs(g["theta"]) / debit * 100.0
        if decay > cfg.max_theta_decay_pct_day:
            return None, f"theta burns {decay:.1f}% of the premium a day, above {cfg.max_theta_decay_pct_day:g}%"
        if name not in SINGLE_LONG and mp / -ml < cfg.min_rr_debit:
            return None, f"reward:risk {mp / -ml:.2f} below {cfg.min_rr_debit:g}"

    ivr = 50.0 if regime.iv_rank is None else regime.iv_rank
    pop = model_ev = an = None
    if not is_fut:
        _, model_ev = structure_pop(s, chain.spot, T, r, q, iv)
        an = prob.analytic_probs(s, chain, plan, iv, regime.rv20, regime.rv60, mkt, cm, pc)
        pop = an["pop_rn"]                    # risk-neutral but skew-aware: priced from the IV at the break-evens, not ATM IV
        if name in CREDIT and pop < cfg.min_pop_credit:
            return None, f"model probability of profit {pop:.0%} below {cfg.min_pop_credit:.0%}"

    # ---- regime gates (trend strength and the variance premium; the trend score alone knows neither)
    if name == "Iron condor" and regime.adx is not None and regime.adx > cfg.adx_condor_block:
        return None, f"ADX {regime.adx:.0f} says the stock is trending hard: a condor sells both sides against the move"
    if regime.vrp is not None:
        if name in CREDIT and regime.vrp < cfg.min_vrp_credit:
            return None, f"IV is {-regime.vrp * 100:.1f} vol points below realised volatility: this sells premium that is cheap"
        if name in DEBIT and regime.vrp > cfg.max_vrp_debit and ivr > cfg.max_vrp_debit_ivr:
            return None, f"IV is {regime.vrp * 100:.1f} vol points above realised volatility: this buys premium that is rich"
    if an is not None and an["ev_rw_r"] < cfg.min_ev_r:
        return None, (f"model expected value after costs is {an['ev_rw_r']:+.2f} risk units (real-world drift, volatility blended "
                      f"from IV and realised), worse than {cfg.min_ev_r:+.2f}")

    # ---- scoring
    # trend quality: ADX says whether the move is a trend (directional trades want it) or a range (condors want that)
    if regime.adx is None:
        tq = 0.5
    elif name == "Iron condor":
        tq = _clamp((cfg.adx_condor_block - regime.adx) / max(cfg.adx_condor_block - cfg.adx_range_max, 1.0))
    else:
        tq = _clamp((regime.adx - 10.0) / max(cfg.adx_trend_ok + 5.0 - 10.0, 1.0))
    sc_signal = 25 * (1 - abs(regime.trend_score) / 4) if name == "Iron condor" else 25 * abs(regime.trend_score) / 4
    sc_signal *= (0.5 + 0.5 * tq) if is_fut else (0.7 + 0.3 * tq)
    sc_iv = 12.5 if is_fut else 25 * _clamp((ivr - 30) / 50) if name in CREDIT else 25 * _clamp((70 - ivr) / 50)
    x = _clamp((regime.vrp or 0.0) / 0.05, -1, 1)
    sc_vrp = 7.5 if (is_fut or regime.vrp is None) else (x + 1) / 2 * 15 if name in CREDIT else (1 - x) / 2 * 15

    be_move = ratio = None
    sc_greek = 5.0 if is_fut else 10.0      # futures: nothing for the Greeks to confirm
    if not is_fut and abs(g["gamma"]) > 1e-12 and abs(g["theta"]) > 1e-12 and regime.rv20:
        be_move = math.sqrt(2 * abs(g["theta"]) / abs(g["gamma"]))
        ratio = (regime.rv20 / math.sqrt(periods_per_year) * chain.spot) / be_move
        sc_greek = 20 * (_clamp((1.3 - ratio) / 0.8) if g["theta"] > 0 else _clamp((ratio - 0.7) / 0.8))
    if name in CREDIT:
        sc_quality = 15 * _clamp((pop - 0.5) / 0.3)
    elif name in DEBIT:
        sc_quality = 10.0 if name in SINGLE_LONG else 15 * _clamp((mp / -ml) / 2)
    else:
        sc_quality = 6.0
    # cost drag is measured against the profit the exit plan actually aims for (futures and long options have no capped max)
    drag = cost / max(abs(plan.profit_target), 1.0)
    penalty = min(15.0, 50 * drag)
    # chasing: a bullish structure after a very stretched rally (or bearish after a rout) is a worse entry than the trend score alone says
    extended = (name in BULLISH and regime.rsi > 75) or (name in BEARISH and regime.rsi < 25)
    ext_penalty = 5.0 if extended else 0.0
    # open interest confirms or contradicts the trade (futures build-up, put/call ratio, walls, max pain); 0 when no OI data
    oi_pts, oi_why, oi_parts = oi_adjust(s, chain, oi, regime.atr, cfg.futures_target_atr, cfg.futures_stop_atr)
    # edge: model expected net P&L in risk units under real-world assumptions, after costs. Replaced by the simulated value in suggest().
    sc_edge = 15.0 * _clamp((an["ev_rw_r"] if an else 0.0) / 0.25, -1.0, 1.0)
    score = max(0.0, min(100.0, sc_signal + sc_iv + sc_vrp + sc_greek + sc_quality - penalty - ext_penalty + oi_pts + sc_edge))

    # ---- explanation
    ivtxt = (f"IV rank {regime.iv_rank:.0f}" + (f", percentile {regime.iv_percentile:.0f}" if regime.iv_percentile is not None else "")) \
        if regime.iv_rank is not None else "IV rank unavailable (treated as 50)"
    reasons.append(f"Trend {regime.trend} (score {regime.trend_score:+d}/4, RSI {regime.rsi:.0f}); {ivtxt}.")
    if regime.vrp is not None:
        reasons.append(f"ATM IV {iv:.0%} vs 20-day realised {regime.rv20:.0%}: variance premium {regime.vrp * 100:+.1f} vol points.")
    if not is_fut:
        side = "short vega: wins if IV falls" if g["vega"] < 0 else "long vega: wins if IV rises"
        reasons.append(f"Net delta {g['delta']:+.0f} units, gamma {g['gamma']:+.3f}, theta Rs {g['theta']:+,.0f}/day, "
                       f"vega Rs {g['vega']:+,.0f} per IV point ({side}).")
        if be_move:
            reasons.append(f"Gamma vs theta: break-even move about Rs {be_move:.1f}/day vs recent typical move "
                           f"Rs {ratio * be_move:.1f}/day ({'theta is paid for taking this risk' if g['theta'] > 0 else 'moves must exceed this to beat theta'}).")
        if name in CREDIT:
            reasons.append(f"Credit Rs {prem:,.0f} against max loss Rs {-ml:,.0f}; model probability of profit {pop:.0%}.")
        else:
            reasons.append(f"Debit Rs {-prem:,.0f}; max profit {'open-ended' if (name in SINGLE_LONG or math.isinf(mp)) else f'Rs {mp:,.0f}'}, max loss Rs {-ml:,.0f}; "
                           f"model probability of profit {pop:.0%}.")
    else:
        reasons.append(f"Futures: no premium decay and full delta. Risk is the stop at {cfg.futures_stop_atr:g} x ATR (Rs {-plan.stop_loss:,.0f}).")
    if an is not None:
        reasons.append(f"Probability of profit at expiry: {an['pop_rn']:.0%} risk-neutral (priced from the smile at the break-evens), "
                       f"{an['pop_rn_net']:.0%} after costs, {an['pop_rw_net']:.0%} real-world (volatility {an['sigma_rw']:.0%} vs IV {an['sigma_iv']:.0%}). "
                       f"Expected net P&L {an['ev_rw_r']:+.2f} risk units.")
    if regime.adx is not None:
        reasons.append(f"ADX {regime.adx:.0f} ({'trending' if regime.adx >= cfg.adx_trend_ok else 'range-like'}); 20-bar momentum {regime.mom_z:+.1f} sd.")
    reasons.extend(oi_why)
    reasons.append(f"Round-trip costs about Rs {cost:,.0f} ({drag:.0%} of the target profit)." if drag < 0.5 else
                   f"Round-trip costs about Rs {cost:,.0f}, large against the profit available.")
    warnings = list(regime.notes)
    if extended:
        warnings.append("The move is stretched (RSI beyond 75 / below 25): this entry chases it, so 5 points were deducted from the score.")
    warnings.append(f"Stock options settle physically at expiry: the plan closes with {cfg.exit_dte} days left to avoid that.")
    if provider_name == "synthetic":
        warnings.append("Synthetic mode: IV is a realised-volatility proxy and prices are model values, not market quotes.")
    if regime.iv_rank is None:
        warnings.append("No IV history: IV rank is unknown and was treated as neutral (50). Supply iv_history for a real IV view.")
    if stats["defined_risk"] is False and not is_fut:
        warnings.append("Risk is not defined: not expected from the built-in structures.")

    risk = {"net_premium": round(prem, 2), "max_profit": mp, "max_loss": ml, "breakevens": stats["breakevens"],
            "defined_risk": stats["defined_risk"], "lots": lots, "cost_roundtrip": cost, "cost_drag": round(drag, 4),
            "model_ev": None if model_ev is None else round(model_ev, 2), "iv_atm": round(iv, 4), "analytic": an,
            "gamma_theta_breakeven_move": None if be_move is None else round(be_move, 2),
            "score_parts": {"signal": round(sc_signal, 1), "iv_fit": round(sc_iv, 1), "variance_premium": round(sc_vrp, 1),
                            "greek_fit": round(sc_greek, 1), "quality": round(sc_quality, 1), "cost_penalty": round(-penalty, 1),
                            "extension_penalty": round(-ext_penalty, 1), "oi": round(oi_pts, 1), "edge": round(sc_edge, 1)},
            "trend_quality": round(tq, 2),
            "oi": None if oi is None else {**oi.to_dict(), "points": round(oi_pts, 1), "parts": {k: round(v, 1) for k, v in oi_parts.items()}}}
    sug = Suggestion(id=uuid.uuid4().hex[:12], symbol=chain.symbol, asof=chain.asof, structure=s, exit_plan=plan,
                     score=round(score, 1), reasons=reasons, risk=risk, greeks=g, pop=None if pop is None else round(pop, 4),
                     regime=regime.snapshot(), dte=dte, warnings=warnings)
    return sug, None


# ------------------------------------------------------------------ top level
@dataclass
class SuggestResult:
    symbol: str
    asof: dt.date
    regime: Optional[Regime]
    expiry: Optional[dt.date]
    suggestions: list[Suggestion] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (structure, reason)
    note: str = ""
    # Why there is no trade. DATA problems ("no_expiry", "no_chain", "insufficient_history") are not trading decisions and
    # must never be presented as "stay out". "no_edge" and "filtered" are genuine no-trade answers. "ok" means suggestions exist.
    code: str = "ok"
    have_bars: int = 0
    need_bars: int = 0
    oi: Optional[OiProfile] = None


def pick_expiry(provider, symbol: str, asof: dt.date, cfg: StrategyConfig,
                selected_expiry: Optional[dt.date] = None) -> Optional[dt.date]:
    if selected_expiry is not None:
        return selected_expiry if selected_expiry > asof else None
    for e in provider.expiries(symbol, asof, 4):
        if cfg.min_dte <= (e - asof).days <= cfg.max_dte:
            return e
    return None


def _daily_returns(bars: list[dict[str, Any]], periods_per_year: int) -> Optional[list[float]]:
    """Log returns of the last ~year of DAILY closes, used to give the simulation the stock's own tail shape. None for
    intraday bars (their returns are not daily shocks) or when there is too little history."""
    if periods_per_year != 252 or len(bars) < 101:
        return None
    cl = [b["close"] for b in bars[-251:]]
    return [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl)) if cl[i - 1] > 0 and cl[i] > 0]


def attach_win(sug: Suggestion, chain: Chain, regime: Regime, bars: list[dict[str, Any]], cfg: StrategyConfig,
               mkt: MarketConfig, cm: CostModel, pc: ProbConfig, calibration: Optional[prob.Calibration],
               periods_per_year: int) -> Optional[str]:
    """Run the managed-trade simulation for one suggestion, store the win block, swap the analytic edge points in the score for
    the simulated ones. Returns a rejection reason when the simulated expected value is below cfg.min_ev_r, else None."""
    iv = chain.atm_iv() or regime.iv or 0.30
    win = prob.refine(sug.structure, sug.exit_plan, chain, iv, regime.rv20, regime.rv60, mkt, cm, pc, calibration,
                      _daily_returns(bars, periods_per_year), cfg.managed_paths or None)
    mc, cal = win["managed"], win["calibrated"]
    sug.win = {**win, "analytic": sug.risk.get("analytic")}
    parts = sug.risk["score_parts"]
    new_edge = 15.0 * _clamp(mc["mean_r"] / 0.25, -1.0, 1.0)
    sug.score = round(max(0.0, min(100.0, sug.score - parts.get("edge", 0.0) + new_edge)), 1)
    parts["edge"] = round(new_edge, 1)
    if mc["mean_r"] < cfg.min_ev_r:
        return f"simulated expected net P&L {mc['mean_r']:+.2f} risk units is below {cfg.min_ev_r:+.2f}"
    sug.reasons.append(
        f"Win probability {cal['p']:.0%} (90% range {cal['lo']:.0%} to {cal['hi']:.0%}, confidence {cal['grade']}): simulated trade as managed, "
        f"{mc['paths']} paths, {mc['tails']} tails. Target reached in {mc['p_target']:.0%}, stop in {mc['p_stop']:.0%}, invalidation in "
        f"{mc['p_invalidation']:.0%}, time exit in {mc['p_time']:.0%}; expected net {mc['mean_r']:+.2f} risk units, typical hold {mc['mean_days']:.0f} days. "
        f"Hold-to-expiry probability of profit is a different number ({sug.pop:.0%})." if sug.pop is not None else
        f"Win probability {cal['p']:.0%} (90% range {cal['lo']:.0%} to {cal['hi']:.0%}, confidence {cal['grade']}): simulated trade as managed.")
    if cal["grade"] == "low":
        sug.warnings.append("Win probability is the model's own number: no (or too few) past trades have been used to calibrate it. "
                            "Run `python -m fno calibrate` on your bhavcopy history, or let the forward log accumulate.")
    return None


def suggest(symbol: str, bars: list[dict[str, Any]], provider, asof: dt.date, cfg: StrategyConfig = StrategyConfig(),
            mkt: MarketConfig = MarketConfig(), cm: CostModel = CostModel(), top_n: int = 3,
            override_trend: Optional[str] = None, selected_expiry: Optional[dt.date] = None,
            pc: ProbConfig = ProbConfig(), calibration: Optional[prob.Calibration] = None) -> SuggestResult:
    """Ranked F&O suggestions for `symbol` as of `asof`, using only bars on or before that date.
    override_trend ('bullish'|'bearish'|'neutral') replaces the technical trend: used for random-signal baselines."""
    bars = [b for b in bars if b["date"] <= asof] if bars and bars[-1]["date"] > asof else bars
    res = SuggestResult(symbol, asof, None, None)
    expiry = pick_expiry(provider, symbol, asof, cfg, selected_expiry)
    if expiry is None:
        res.code, res.note = "no_expiry", f"no expiry between {cfg.min_dte} and {cfg.max_dte} days away"
        return res
    chain = provider.get_chain(symbol, asof, expiry)
    if chain is None or not chain.quotes:
        res.code, res.note = "no_chain", "no option chain available"
        return res
    periods_per_year = getattr(provider, "periods_per_year", 252)
    regime = compute_regime(bars, provider.atm_iv_history(symbol, asof, 252), chain.atm_iv(), periods_per_year)
    if regime is None:
        res.code, res.have_bars, res.need_bars = "insufficient_history", len(bars), 60
        unit = "5-minute candles" if getattr(provider, "name", "") == "breeze-live" else "trading days"
        res.note = f"only {len(bars)} {unit} of price history: the trend rules need at least 60"
        if getattr(provider, "name", "") != "breeze-live":
            res.note += " (about three months) and IV rank needs about a year"
        return res
    if override_trend:
        regime.trend, regime.trend_score = override_trend, {"bullish": 2, "bearish": -2}.get(override_trend, 0)
    res.regime, res.expiry = regime, expiry
    ctx_fn = getattr(provider, "oi_context", None)
    res.oi = oi_profile(chain, ctx_fn(symbol, asof, expiry) if ctx_fn else None)

    names = candidate_names(regime, cfg)
    if not names:
        res.code = "no_edge"
        res.note = f"no edge: {regime.trend} trend with IV rank {'n/a' if regime.iv_rank is None else f'{regime.iv_rank:.0f}'} calls for no trade"
        return res
    found: list[Suggestion] = []
    for n in names:
        s = BUILDERS[n](chain, cfg, mkt, cm, 1)
        if s is None:
            res.rejected.append((n, "could not build from this chain"))
            continue
        sug, why = evaluate(s, chain, regime, cfg, mkt, cm, getattr(provider, "name", ""), res.oi,
                            periods_per_year, pc)
        floor = cfg.futures_min_score if n in FUTURES else cfg.min_score
        if sug is None:
            res.rejected.append((n, why or "rejected"))
        elif sug.score < floor:
            res.rejected.append((n, f"score {sug.score:.1f} below minimum {floor:g}"))
        else:
            if cfg.managed_paths:                               # the simulation replaces the analytic edge estimate in the score
                bad = attach_win(sug, chain, regime, bars, cfg, mkt, cm, pc, calibration, periods_per_year)
                if bad:
                    res.rejected.append((n, bad))
                    continue
                if sug.score < floor:
                    res.rejected.append((n, f"score {sug.score:.1f} below minimum {floor:g} once the simulated edge is included"))
                    continue
            found.append(sug)
    res.suggestions = sorted(found, key=lambda x: -x.score)[:top_n]
    if not res.suggestions and not res.note:
        res.code, res.note = "filtered", "candidates were considered but none passed the filters"
    if not res.suggestions and res.code == "ok":
        res.code = "filtered"
    return res
