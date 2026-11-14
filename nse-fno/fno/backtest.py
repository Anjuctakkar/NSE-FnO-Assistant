"""
Walk-forward backtest of the F&O selector, and the position simulator it shares with forward evaluation.

How a backtest runs
  * One position per symbol at a time. On each flat day the selector sees ONLY data up to that day's close and,
    if its best suggestion scores above the minimum, the trade is opened at that close at realistic fills
    (mid +/- a fraction of the half spread), plus costs.
  * After entry the position is marked to market every day from that day's chain. Exits are checked on daily
    closes in this order: technical invalidation, stop, profit target, time exit (exit_dte days left), expiry
    settlement at intrinsic value. Intraday stop/target touches are NOT modelled.
  * Costs: STT, exchange, SEBI, stamp, GST, brokerage on every order, plus slippage. Long ITM options held to
    expiry pay exercise STT. Stock options are physically settled in reality; the backtest cash-settles, which is
    why the plan exits before expiry.
  * Every closed trade carries a Greek attribution: how much of its P&L came from delta, gamma, theta and vega
    (second-order Taylor terms on daily moves). Slippage at entry and exit is reported separately; "unexplained" is
    what the Taylor terms miss (higher-order terms, vanna/volga, futures cost-of-carry drift).

Two modes: entry_mode="signal" (the real selector) and entry_mode="random" (same structures, strike rules, exits and
costs, but the trend is drawn at random). If the signal does not beat random on the same data, the technical
view is adding nothing and any profit is coming from structure, volatility assumptions or luck.

Backtest limits (do not skip): with SyntheticProvider the option prices are model values driven by realised vol, so
results show how the RULES behave, not what a real market would have paid. Few trades prove little (<30 flagged).
Choosing stocks that look good today flatters any backtest.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from . import greeks as bs
from .config import CostModel, MarketConfig, StrategyConfig
from .costs import entry_costs, exit_costs, fill_price, future_fill, order_cost
from .models import Chain, Quote, Structure, Suggestion
from .selector import suggest

ZERO_ATTR = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "unexplained": 0.0, "entry_slippage": 0.0, "exit_slippage": 0.0}


# ------------------------------------------------------------------ marks
def _leg_mark(leg, chain: Optional[Chain], spot: float, asof: dt.date, mkt: MarketConfig,
              last_iv: Optional[float]) -> tuple[float, Optional[float], Optional[Quote]]:
    """(mid price, implied vol, quote) for one leg today. Falls back to the model at the last known IV when the
    chain has no quote for the strike."""
    T = max(0.0, (leg.expiry - asof).days / 365.0)
    if leg.kind == "FUT":
        px = (chain.futures_price if chain and chain.futures_price else spot * math.exp((mkt.risk_free - mkt.div_yield) * T))
        return px, None, None
    q = chain.get(leg.kind, leg.strike) if chain else None
    if q is not None:
        return q.mid, q.iv or last_iv, q
    iv = last_iv or leg.iv or 0.30
    return bs.bs_price(spot, leg.strike, T, mkt.risk_free, mkt.div_yield, iv, leg.kind), iv, None


def _attribute(s: Structure, prev_spot: float, spot: float, prev_ivs: list[Optional[float]], ivs: list[Optional[float]],
               prev_date: dt.date, d: dt.date, mkt: MarketConfig) -> dict[str, float]:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    dS, days = spot - prev_spot, (d - prev_date).days
    for i, l in enumerate(s.legs):
        u = s.units(l) * l.side
        if l.kind == "FUT":
            out["delta"] += u * dS
            continue
        T = max(0.0, (l.expiry - prev_date).days / 365.0)
        iv0 = prev_ivs[i] or l.iv or 0.30
        g = bs.greeks(prev_spot, l.strike, T, mkt.risk_free, mkt.div_yield, iv0, l.kind)
        iv1 = ivs[i] if ivs[i] else iv0
        out["delta"] += u * g.delta * dS
        out["gamma"] += u * 0.5 * g.gamma * dS * dS
        out["theta"] += u * g.theta * days
        out["vega"] += u * g.vega * (iv1 - iv0) * 100.0
    return out


# ------------------------------------------------------------------ position simulation
def simulate_position(sug: Suggestion, provider, dates: list[dt.date], cm: CostModel = CostModel(),
                      mkt: MarketConfig = MarketConfig()) -> dict[str, Any]:
    """Run a suggestion forward over `dates` (trading days after entry, ascending).
    Returns status ('target'|'stop'|'invalidation'|'time'|'expiry'|'open'), exit_date, gross_pnl, costs, net_pnl,
    attribution, days_held and, for open positions, the current gross mark."""
    s, plan, sym = sug.structure, sug.exit_plan, sug.symbol
    expiry, opened = s.expiry, s.opened
    c0 = provider.get_chain(sym, opened, expiry)
    prev_spot = provider.spot(sym, opened) or (c0.spot if c0 else 0.0)
    mids0, ivs0 = [], []
    for l in s.legs:
        m, iv, _ = _leg_mark(l, c0, prev_spot, opened, mkt, l.iv)
        mids0.append(m)
        ivs0.append(iv)
    slip = s.mark_pnl(mids0)                                   # negative: what the fills gave up versus mid
    attr = dict(ZERO_ATTR, entry_slippage=round(slip, 2))
    prev_pnl, prev_ivs, prev_date = slip, ivs0, opened
    e_cost = entry_costs(s, cm)
    last_pnl = slip

    for d in dates:
        if d <= opened:
            continue
        if d >= expiry:                                            # settle at intrinsic on the expiry date's close
            S = provider.spot(sym, expiry) or prev_spot
            prices = [(max(S - l.strike, 0.0) if l.kind == "CE" else max(l.strike - S, 0.0)) if l.kind != "FUT" else S for l in s.legs]
            gross = s.mark_pnl(prices)
            x_cost = 0.0
            for l, p in zip(s.legs, prices):
                if l.kind == "FUT":
                    x_cost += order_cost("FUT", -l.side, p, s.units(l), cm)
                elif l.side > 0 and p > 0:
                    x_cost += order_cost(l.kind, -1, p, s.units(l), cm, exercised_intrinsic=p)
            _add(attr, _attribute(s, prev_spot, S, prev_ivs, [None] * len(s.legs), prev_date, expiry, mkt), gross - prev_pnl)
            return _result("expiry", expiry, gross, e_cost + x_cost, attr, opened, expiry, prices)
        spot = provider.spot(sym, d)
        chain = provider.get_chain(sym, d, expiry)
        if spot is None:
            continue
        marks = [_leg_mark(l, chain, spot, d, mkt, prev_ivs[i]) for i, l in enumerate(s.legs)]
        prices, ivs = [m[0] for m in marks], [m[1] for m in marks]
        pnl = s.mark_pnl(prices)
        _add(attr, _attribute(s, prev_spot, spot, prev_ivs, ivs, prev_date, d, mkt), pnl - prev_pnl)
        prev_pnl, prev_ivs, prev_date, prev_spot, last_pnl = pnl, ivs, d, spot, pnl

        reason = None
        inv = plan.invalidation
        if inv and ((inv[0] == "below" and spot <= inv[1]) or (inv[0] == "above" and spot >= inv[1])):
            reason = "invalidation"
        elif pnl <= plan.stop_loss:
            reason = "stop"
        elif pnl >= plan.profit_target:
            reason = "target"
        elif (expiry - d).days <= plan.exit_dte:
            reason = "time"
        if reason:
            fills = []
            for l, m in zip(s.legs, marks):
                fills.append(future_fill(m[0], -l.side, cm) if l.kind == "FUT" else fill_price(m[2], -l.side, cm, fallback_mid=m[0]))
            gross = s.mark_pnl(fills)
            attr["exit_slippage"] = gross - pnl                      # what the closing fills gave up versus the day's mid marks
            return _result(reason, d, gross, e_cost + exit_costs(s, fills, cm), attr, opened, d, fills)
    return {"status": "open", "exit_date": None, "gross_pnl": round(last_pnl, 2), "costs": e_cost, "net_pnl": round(last_pnl - e_cost, 2),
            "attribution": {k: round(v, 2) for k, v in attr.items()}, "days_held": ((dates[-1] if dates else opened) - opened).days,
            "exit_prices": None}


def _add(attr: dict[str, float], parts: dict[str, float], actual: float) -> None:
    for k, v in parts.items():
        attr[k] += v
    attr["unexplained"] += actual - sum(parts.values())


def _result(status: str, d: dt.date, gross: float, costs: float, attr: dict[str, float], opened: dt.date,
            exit_date: dt.date, prices: list[float]) -> dict[str, Any]:
    return {"status": status, "exit_date": exit_date, "gross_pnl": round(gross, 2), "costs": round(costs, 2),
            "net_pnl": round(gross - costs, 2), "attribution": {k: round(v, 2) for k, v in attr.items()},
            "days_held": (exit_date - opened).days, "exit_prices": [round(p, 2) for p in prices]}


def trade_record(sug: Suggestion, sim: dict[str, Any]) -> dict[str, Any]:
    """One flat dict per trade: what was suggested, why, and what happened."""
    risk_unit = abs(sug.exit_plan.stop_loss) or 1.0
    reg = sug.regime
    return {
        "symbol": sug.symbol, "strategy": sug.structure.name, "entry_date": sug.asof.isoformat(),
        "exit_date": sim["exit_date"].isoformat() if sim["exit_date"] else None, "expiry": sug.structure.expiry.isoformat(),
        "exit_reason": sim["status"], "lots": sug.structure.legs[0].lots, "days_held": sim["days_held"],
        "gross_pnl": sim["gross_pnl"], "costs": sim.get("costs", 0.0), "net_pnl": sim["net_pnl"],
        "risk_unit": round(risk_unit, 2), "r_multiple": round(sim["net_pnl"] / risk_unit, 3),
        "score": sug.score, "pop": sug.pop, "iv_rank": reg.get("iv_rank"), "trend": reg.get("trend"),
        "trend_score": reg.get("trend_score"), "net_premium": sug.risk.get("net_premium"),
        "attribution": sim["attribution"], "legs": [l.to_dict() for l in sug.structure.legs],
    }


# ------------------------------------------------------------------ driver
@dataclass
class BacktestResult:
    symbol: str
    params: dict[str, Any]
    trades: list[dict[str, Any]]
    metrics: dict[str, Any]
    skipped: dict[str, int] = field(default_factory=dict)


def run_backtest(symbol: str, bars: list[dict[str, Any]], provider, cfg: StrategyConfig = StrategyConfig(),
                 mkt: MarketConfig = MarketConfig(), cm: CostModel = CostModel(), warmup: int = 60,
                 entry_mode: str = "signal", seed: int = 7, start: Optional[dt.date] = None,
                 end: Optional[dt.date] = None) -> BacktestResult:
    dates = [b["date"] for b in bars]
    i = max(warmup, bisect.bisect_left(dates, start) if start else 0)
    rng = random.Random(seed)
    trades: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    while i < len(bars) - 1:
        d = dates[i]
        if end and d > end:
            break
        override = rng.choice(["bullish", "bearish", "neutral"]) if entry_mode == "random" else None
        res = suggest(symbol, bars[:i + 1], provider, d, cfg, mkt, cm, top_n=1, override_trend=override)
        if not res.suggestions:
            key = (res.note or "filtered")[:60]
            skipped[key] = skipped.get(key, 0) + 1
            i += 1
            continue
        sug = res.suggestions[0]
        sim = simulate_position(sug, provider, dates[i + 1:], cm, mkt)
        trades.append(trade_record(sug, sim))
        if sim["status"] == "open":
            break
        i = bisect.bisect_right(dates, sim["exit_date"])
    return BacktestResult(symbol, {"entry_mode": entry_mode, "warmup": warmup, "seed": seed, "provider": getattr(provider, "name", "?"),
                                   "min_score": cfg.min_score, "bars": len(bars)},
                          trades, summarize(trades), skipped)


# ------------------------------------------------------------------ metrics
def _group(closed: list[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in closed:
        out.setdefault(str(key(t)), []).append(t)
    return {k: {"trades": len(v), "win_rate": round(100 * sum(1 for t in v if t["net_pnl"] > 0) / len(v), 1),
                "net_pnl": round(sum(t["net_pnl"] for t in v), 2), "avg_r": round(sum(t["r_multiple"] for t in v) / len(v), 3)}
            for k, v in sorted(out.items())}


def _iv_bucket(t: dict[str, Any]) -> str:
    r = t.get("iv_rank")
    return "n/a" if r is None else "low (<30)" if r < 30 else "mid (30-50)" if r < 50 else "high (50+)"


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [t for t in trades if t["exit_reason"] != "open"]
    n = len(closed)
    base = {"trades": n, "open_trades": len(trades) - n, "small_sample": n < 30}
    if n == 0:
        return base
    nets = [t["net_pnl"] for t in closed]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    rs = [t["r_multiple"] for t in closed]
    eq = peak = dd = streak = worst = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    attr = {k: round(sum(t["attribution"].get(k, 0.0) for t in closed), 2) for k in ZERO_ATTR}
    gross = sum(t["gross_pnl"] for t in closed)
    costs = sum(t["costs"] for t in closed)
    cal = _calibration(closed)
    return {**base,
            "win_rate": round(100 * len(wins) / n, 1),
            "total_net_pnl": round(sum(nets), 2), "avg_net_pnl": round(sum(nets) / n, 2),
            "expectancy_r": round(sum(rs) / n, 3),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
            "max_drawdown": round(dd, 2), "longest_losing_streak": int(worst),
            "avg_days_held": round(sum(t["days_held"] for t in closed) / n, 1),
            "gross_pnl": round(gross, 2), "total_costs": round(costs, 2),
            "costs_pct_of_gross_profit": round(100 * costs / sum(t["gross_pnl"] for t in closed if t["gross_pnl"] > 0), 1)
            if any(t["gross_pnl"] > 0 for t in closed) else None,
            "by_strategy": _group(closed, lambda t: t["strategy"]),
            "by_exit": _group(closed, lambda t: t["exit_reason"]),
            "by_iv_rank": _group(closed, _iv_bucket),
            "attribution": attr, "calibration": cal}


def _calibration(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Model probability of profit against what happened. POP assumes hold-to-expiry, while the trades exit early
    on rules, so this is indicative, not exact."""
    rows = [t for t in closed if t.get("pop") is not None]
    if not rows:
        return {}
    brier = sum((t["pop"] - (1.0 if t["net_pnl"] > 0 else 0.0)) ** 2 for t in rows) / len(rows)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for t in rows:
        b = "<50%" if t["pop"] < 0.5 else "50-60%" if t["pop"] < 0.6 else "60-70%" if t["pop"] < 0.7 else "70%+"
        buckets.setdefault(b, []).append(t)
    return {"brier": round(brier, 3), "n": len(rows),
            "buckets": {b: {"trades": len(v), "predicted": round(100 * sum(t["pop"] for t in v) / len(v), 1),
                            "realised": round(100 * sum(1 for t in v if t["net_pnl"] > 0) / len(v), 1)} for b, v in sorted(buckets.items())}}


def edge_vs_random(signal: BacktestResult, random_: BacktestResult) -> dict[str, Any]:
    """Compare the real selector with the random-trend baseline on the same data."""
    a, b = signal.metrics, random_.metrics
    if not a.get("trades") or not b.get("trades"):
        return {"verdict": "not enough trades in one of the runs to compare"}
    d = round(a["expectancy_r"] - b["expectancy_r"], 3)
    return {"signal_expectancy_r": a["expectancy_r"], "random_expectancy_r": b["expectancy_r"], "difference_r": d,
            "signal_trades": a["trades"], "random_trades": b["trades"],
            "verdict": ("signal beat the random baseline, but check the sample size" if d > 0.05 else
                        "signal did not beat the random baseline: the technical view is not adding edge here")}
