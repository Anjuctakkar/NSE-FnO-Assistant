"""
Win-probability model.

The selector used to report one number, `pop`: the risk-neutral probability that a structure finishes in profit if HELD
TO EXPIRY, from a lognormal with ONE volatility (ATM IV), before costs. That number answers a question nobody is asking:

    risk-neutral   a fairly priced structure has ~zero expected value in that world, so it says nothing about edge, and a
                   real stock drifts at its own rate, not the risk-free rate.
    one volatility the smile is ignored: with put skew, a bull put spread's short strike sits where IV is HIGHER than ATM,
                   so ATM-IV flatters the probability of staying above it.
    held to expiry the plan never does that: it exits on a target, a stop, an invalidation level or a time rule, so the
                   chance the TRADE wins differs from the chance the PAYOFF is positive.
    gross of costs a credit spread whose credit is a few rupees can have a 70% gross POP and a 55% net one.

This module answers the real question in layers, cheapest first. Each layer is reported, so the user can see how much
each correction moved the number:

    pop_rn          risk-neutral, skew-aware (digital price from the IV smile, Breeden-Litzenberger), gross
    pop_rn_net      the same after round-trip costs
    pop_rw_net      real-world: own drift, volatility = blend of IV and a realised-vol forecast, after costs
    managed         Monte Carlo of the trade AS MANAGED: fat-tailed daily shocks, mean-reverting stochastic IV correlated
                    with the stock, the real exit plan (invalidation, stop, target, time), exit slippage and costs.
                    Gives P(net profit), P(target), P(stop), expected net P&L in risk units, and a percentile spread.
    calibrated      the managed probability shrunk toward what the model's past probabilities actually realised
                    (backtests and forward logs), with a credible interval and a confidence grade.

What this does NOT do: predict the stock. The drift is a constant and the technical view does not tilt the distribution.
If the signal has edge, the calibration table is where it shows up (predicted vs realised by bucket); it is not assumed.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import greeks as bs
from .config import CostModel, MarketConfig, ProbConfig
from .costs import entry_costs, exit_costs, fill_price, future_fill, order_cost, roundtrip_estimate
from .models import Chain, ExitPlan, Structure

INF = math.inf


# ====================================================================== volatility smile
def smile(chain: Chain) -> list[tuple[float, float]]:
    """(strike, IV) using the out-of-the-money option at each strike (puts below spot, calls above), which is the
    liquid and well-priced side. Strikes with no IV are skipped."""
    by_k: dict[float, list[float]] = {}
    for q in chain.quotes:
        if not q.iv:
            continue
        otm = (q.kind == "PE" and q.strike <= chain.spot) or (q.kind == "CE" and q.strike >= chain.spot)
        if otm:
            by_k.setdefault(q.strike, []).append(q.iv)
    if len(by_k) < 3:                                   # a thin chain: take whatever IVs exist
        by_k = {}
        for q in chain.quotes:
            if q.iv:
                by_k.setdefault(q.strike, []).append(q.iv)
    return sorted((k, sum(v) / len(v)) for k, v in by_k.items())


def iv_at(sm: list[tuple[float, float]], K: float) -> float:
    """Linear interpolation of the smile, flat beyond the ends."""
    if not sm:
        return 0.30
    if K <= sm[0][0]:
        return sm[0][1]
    if K >= sm[-1][0]:
        return sm[-1][1]
    lo, hi = 0, len(sm) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if sm[mid][0] <= K:
            lo = mid
        else:
            hi = mid
    (k0, v0), (k1, v1) = sm[lo], sm[hi]
    return v0 + (v1 - v0) * (K - k0) / (k1 - k0)


def rn_prob_above(S: float, K: float, T: float, r: float, q: float, sm: list[tuple[float, float]]) -> float:
    """Risk-neutral P(S_T > K) from the smile. A digital call is -dC/dK, and with a smile that is
    N(d2) - e^{rT} * vega_raw * dSigma/dK: the first term alone is the flat-vol answer, the second is the skew correction."""
    if K <= 0:
        return 1.0
    if T <= 0:
        return 1.0 if S > K else 0.0
    sig = max(iv_at(sm, K), 0.02)
    d1, d2 = bs._d1_d2(S, K, T, r, q, sig)
    h = 0.03 * S
    k_lo, k_hi = max(K - h, 1e-6), K + h
    slope = (iv_at(sm, k_hi) - iv_at(sm, k_lo)) / (k_hi - k_lo)
    vega_raw = S * math.exp(-q * T) * bs.norm_pdf(d1) * math.sqrt(T)
    return min(1.0, max(0.0, bs.norm_cdf(d2) - math.exp(r * T) * vega_raw * slope))


def rw_prob_above(S: float, K: float, T: float, mu: float, q: float, sigma: float) -> float:
    """Real-world P(S_T > K): lognormal with drift mu."""
    if K <= 0:
        return 1.0
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    return bs.norm_cdf((math.log(S / K) + (mu - q - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T)))


# ====================================================================== payoff geometry
def profit_intervals(s: Structure, hurdle: float = 0.0) -> list[tuple[float, float]]:
    """Underlying prices at expiry where payoff - hurdle > 0, as (low, high) intervals; high = inf when open-ended.
    The payoff is piecewise linear with knots at the strikes, so the roots are exact (no sampling)."""
    strikes = sorted({l.strike for l in s.legs if l.strike})
    ref = max(strikes) if strikes else max(l.entry_price for l in s.legs)
    knots = [0.0] + strikes + [ref * 4.0]
    vals = [s.payoff(k) - hurdle for k in knots]
    segs: list[tuple[float, float, bool]] = []
    for (x0, v0), (x1, v1) in zip(zip(knots, vals), zip(knots[1:], vals[1:])):
        if v0 * v1 < 0:
            xr = x0 + (-v0) * (x1 - x0) / (v1 - v0)
            segs += [(x0, xr, v0 > 0), (xr, x1, v1 > 0)]
        else:
            segs.append((x0, x1, (v0 + v1) > 1e-12))
    out: list[tuple[float, float]] = []
    for lo, hi, ok in segs:
        if not ok:
            continue
        if out and abs(out[-1][1] - lo) < 1e-9:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    if out and abs(out[-1][1] - knots[-1]) < 1e-9 and s.payoff(knots[-1] + 1.0) - hurdle >= s.payoff(knots[-1]) - hurdle:
        out[-1] = (out[-1][0], INF)
    return out


def _prob_in(intervals: list[tuple[float, float]], above) -> float:
    total = 0.0
    for lo, hi in intervals:
        total += above(lo) - (0.0 if hi == INF else above(hi))
    return min(1.0, max(0.0, total))


def lognormal_pop_ev(s: Structure, spot: float, T: float, drift: float, q: float, sigma: float, hurdle: float = 0.0,
                     n: int = 601) -> tuple[float, float]:
    """(P(payoff - hurdle > 0), E[payoff - hurdle]) at expiry under a lognormal with the given drift and volatility."""
    sd = sigma * math.sqrt(T)
    F = spot * math.exp((drift - q) * T)
    dz = 10.0 / (n - 1)
    tw = pop = ev = 0.0
    for i in range(n):
        z = -5.0 + i * dz
        w = bs.norm_pdf(z) * dz
        pnl = s.payoff(F * math.exp(-0.5 * sd * sd + sd * z)) - hurdle
        tw += w
        if pnl > 0:
            pop += w
        ev += pnl * w
    return pop / tw, ev / tw


def realised_forecast(rv20: Optional[float], rv60: Optional[float], pc: ProbConfig) -> Optional[float]:
    if rv20 and rv60:
        return pc.rv_blend20 * rv20 + (1 - pc.rv_blend20) * rv60
    return rv20 or rv60


def rw_sigma(iv_atm: float, rv20: Optional[float], rv60: Optional[float], pc: ProbConfig) -> float:
    """Real-world volatility for the horizon: a blend of what the market charges (IV) and what the stock has been doing."""
    rv = realised_forecast(rv20, rv60, pc)
    return max(0.05, iv_atm if rv is None else pc.iv_weight * iv_atm + (1 - pc.iv_weight) * rv)


# ====================================================================== analytic layer
def analytic_probs(s: Structure, chain: Chain, plan: ExitPlan, iv_atm: float, rv20: Optional[float], rv60: Optional[float],
                   mkt: MarketConfig, cm: CostModel, pc: ProbConfig = ProbConfig()) -> dict[str, Any]:
    """The closed-form layers. All are held-to-expiry; the managed layer (below) adds the exit plan."""
    T = max((chain.expiry - chain.asof).days, 0) / 365.0
    r, q, S = mkt.risk_free, mkt.div_yield, chain.spot
    cost = roundtrip_estimate(s, cm)
    sm = smile(chain)
    above_rn = lambda k: rn_prob_above(S, k, T, r, q, sm)        # noqa: E731
    sig_rw = rw_sigma(iv_atm, rv20, rv60, pc)
    above_rw = lambda k: rw_prob_above(S, k, T, pc.drift, q, sig_rw)   # noqa: E731
    iv_gross, iv_net = profit_intervals(s, 0.0), profit_intervals(s, cost)
    risk_unit = abs(plan.stop_loss) or 1.0
    pop_rw_net, ev_rw = lognormal_pop_ev(s, S, T, pc.drift, q, sig_rw, cost)
    return {"pop_rn": round(_prob_in(iv_gross, above_rn), 4), "pop_rn_net": round(_prob_in(iv_net, above_rn), 4),
            "pop_rw_net": round(_prob_in(iv_net, above_rw), 4), "ev_rw_net": round(ev_rw, 2),
            "ev_rw_r": round(ev_rw / risk_unit, 4), "sigma_rw": round(sig_rw, 4), "sigma_iv": round(iv_atm, 4), "cost": cost}


# ====================================================================== managed-trade Monte Carlo
def _standardised(hist_returns: Optional[list[float]]) -> Optional[list[float]]:
    if not hist_returns or len(hist_returns) < 100:
        return None
    m = sum(hist_returns) / len(hist_returns)
    sd = math.sqrt(sum((x - m) ** 2 for x in hist_returns) / (len(hist_returns) - 1))
    return [(x - m) / sd for x in hist_returns] if sd > 0 else None


def managed_mc(s: Structure, plan: ExitPlan, chain: Chain, iv_atm: float, sig_rw: float, mkt: MarketConfig, cm: CostModel,
               pc: ProbConfig = ProbConfig(), hist_returns: Optional[list[float]] = None,
               paths: Optional[int] = None) -> dict[str, Any]:
    """Simulate the trade the way backtest.simulate_position runs it: daily closes, exits checked in the same order
    (invalidation, stop, target, time), exit fills with slippage, entry and exit costs. Returns outcome probabilities
    and the distribution of NET P&L. Deterministic for a given seed. Close-to-close daily steps: intraday touches are not
    modelled, exactly as in the backtest, so the two are comparable.

    Spot:  daily shocks from the stock's own standardised history when >= 100 daily returns are supplied (keeps its real tail
           shape), else Student-t scaled to unit variance; antithetic pairs; drift pc.drift; volatility sig_rw.
    IV:    log IV of every leg moves together, mean-reverts toward the real-world level (entry IV x sig_rw / iv_atm, so a
           seller earns the variance premium the model believes in) and is correlated with the stock (leverage effect).
    """
    n = int(paths or pc.paths)
    n += n % 2
    dte = (chain.expiry - chain.asof).days
    steps = max(1, round(dte * 252 / 365))
    dt_y = 1.0 / 252
    r, q, S0 = mkt.risk_free, mkt.div_yield, chain.spot
    rng = random.Random(pc.seed)
    std_hist = _standardised(hist_returns)
    nu = pc.t_df
    t_scale = math.sqrt((nu - 2) / nu) if nu and nu > 2 else 1.0
    legs = s.legs
    units = [s.units(l) for l in legs]
    e_cost = entry_costs(s, cm)
    risk_unit = abs(plan.stop_loss) or 1.0
    inv = plan.invalidation
    target, stop = plan.profit_target, plan.stop_loss
    iv0 = [l.iv or iv_atm for l in legs]
    lvl = math.log(max(sig_rw / iv_atm, 0.3)) if iv_atm > 0 else 0.0           # long-run log-IV shift vs entry
    rho, rho2 = pc.spot_iv_corr, math.sqrt(max(0.0, 1 - pc.spot_iv_corr ** 2))
    drift_step = (pc.drift - q) * dt_y - 0.5 * sig_rw * sig_rw * dt_y
    sd_step = sig_rw * math.sqrt(dt_y)
    iv_sd = pc.ivol_sd * math.sqrt(dt_y)
    kappa = pc.ivol_kappa * dt_y

    def shock() -> float:
        if std_hist:
            return std_hist[rng.randrange(len(std_hist))]
        z = rng.gauss(0.0, 1.0)
        if nu and nu > 2:
            z = z / math.sqrt(rng.gammavariate(nu / 2.0, 2.0) / nu) * t_scale
        return z

    def leg_price(i: int, S: float, rem_days: float, x: float) -> float:
        l = legs[i]
        if l.kind == "FUT":
            return S * math.exp((r - q) * max(rem_days, 0.0) / 365.0)
        return bs.bs_price(S, l.strike, max(rem_days, 0.0) / 365.0, r, q, max(iv0[i] * math.exp(x), 0.03), l.kind)

    nets: list[float] = []
    days_held: list[float] = []
    out = {"target": 0, "stop": 0, "invalidation": 0, "time": 0, "expiry": 0}
    for p in range(n // 2):
        zs, ws = [], []
        for _ in range(steps):
            zs.append(shock())
            ws.append(rng.gauss(0.0, 1.0))
        for sign in (1.0, -1.0):                                   # antithetic pair
            S, x = S0, 0.0
            reason, exit_day, fills = None, 0.0, None
            for k in range(1, steps + 1):
                z, w = sign * zs[k - 1], sign * ws[k - 1]
                S *= math.exp(drift_step + sd_step * z)
                x += kappa * (lvl - x) + iv_sd * (rho * z + rho2 * w)
                elapsed = k * dte / steps
                rem = dte - elapsed
                if k == steps or rem <= 0:
                    prices = [(max(S - l.strike, 0.0) if l.kind == "CE" else max(l.strike - S, 0.0)) if l.kind != "FUT" else S
                              for l in legs]
                    gross = sum(l.side * (px - l.entry_price) * u for l, px, u in zip(legs, prices, units))
                    x_cost = 0.0
                    for l, px, u in zip(legs, prices, units):
                        if l.kind == "FUT":
                            x_cost += _order(l, px, u, cm)
                        elif l.side > 0 and px > 0:
                            x_cost += order_cost(l.kind, -1, px, u, cm, exercised_intrinsic=px)
                    reason, exit_day, net = "expiry", float(dte), gross - e_cost - x_cost
                    break
                mids = [leg_price(i, S, rem, x) for i in range(len(legs))]
                pnl = sum(l.side * (m - l.entry_price) * u for l, m, u in zip(legs, mids, units))
                if inv and ((inv[0] == "below" and S <= inv[1]) or (inv[0] == "above" and S >= inv[1])):
                    reason = "invalidation"
                elif pnl <= stop:
                    reason = "stop"
                elif pnl >= target:
                    reason = "target"
                elif rem <= plan.exit_dte:
                    reason = "time"
                if reason:
                    fills = [future_fill(m, -l.side, cm) if l.kind == "FUT" else fill_price(None, -l.side, cm, fallback_mid=m)
                             for l, m in zip(legs, mids)]
                    gross = sum(l.side * (f - l.entry_price) * u for l, f, u in zip(legs, fills, units))
                    net = gross - e_cost - exit_costs(s, fills, cm)
                    exit_day = elapsed
                    break
            out[reason] += 1
            nets.append(net)
            days_held.append(exit_day)
    m = len(nets)
    nets_sorted = sorted(nets)
    q_at = lambda f: nets_sorted[min(m - 1, max(0, int(f * m)))]           # noqa: E731
    p_win = sum(1 for x in nets if x > 0) / m
    mean_net = sum(nets) / m
    return {"p_win": round(p_win, 4), "se": round(math.sqrt(max(p_win * (1 - p_win), 1e-9) / m), 4), "paths": m,
            "p_target": round(out["target"] / m, 4), "p_stop": round(out["stop"] / m, 4),
            "p_invalidation": round(out["invalidation"] / m, 4), "p_time": round(out["time"] / m, 4),
            "p_expiry": round(out["expiry"] / m, 4), "mean_net": round(mean_net, 2), "mean_r": round(mean_net / risk_unit, 4),
            "median_net": round(q_at(0.5), 2), "p05_net": round(q_at(0.05), 2), "p95_net": round(q_at(0.95), 2),
            "mean_days": round(sum(days_held) / m, 1), "risk_unit": round(risk_unit, 2),
            "tails": "history" if std_hist else f"student-t({nu:g})" if nu and nu > 2 else "normal"}


def _order(leg, px: float, units: int, cm: CostModel) -> float:
    return order_cost("FUT", -leg.side, px, units, cm)


# ====================================================================== calibration
@dataclass
class Calibration:
    """What the model's stated probabilities actually realised. Points are (predicted probability, won 0/1, family).
    The calibrated probability for a new prediction p is a Beta-binomial posterior built from the nearest past predictions:
    the model's own p counts as `prior_strength` trades of evidence, the local record adds its wins and losses. With no
    record the answer is p itself and the interval is wide; with a long record it follows the record."""
    points: list[tuple[float, int, str]] = field(default_factory=list)
    updated: Optional[str] = None
    MAX_POINTS = 6000

    def add(self, p_pred: Optional[float], won: bool, family: str = "") -> None:
        if p_pred is None or not (0.0 <= p_pred <= 1.0):
            return
        self.points.append((round(float(p_pred), 4), 1 if won else 0, family))
        if len(self.points) > self.MAX_POINTS:
            self.points = self.points[-self.MAX_POINTS:]

    def add_trades(self, trades: list[dict[str, Any]], key: str = "p_win") -> int:
        k = 0
        for t in trades:
            if t.get("exit_reason") in (None, "open", "no_data") or t.get(key) is None:
                continue
            self.add(t[key], t["net_pnl"] > 0, _family(t.get("strategy", "")))
            k += 1
        return k

    @property
    def n(self) -> int:
        return len(self.points)

    def apply(self, p: float, family: str = "", se_mc: float = 0.0, pc: ProbConfig = ProbConfig()) -> dict[str, Any]:
        """Calibrated probability, a 90% interval and a confidence grade for model probability `p`."""
        pool = [pt for pt in self.points if pt[2] == family] if family else []
        if len(pool) < 25:
            pool = self.points                                     # too little in this family: use everything
        n_pool = len(pool)
        if n_pool < 20:
            half = 1.645 * se_mc + pc.model_uncertainty
            return {"p": round(p, 4), "lo": round(max(0.0, p - half), 4), "hi": round(min(1.0, p + half), 4), "n_calibration": n_pool,
                    "grade": "low", "basis": "model only: no (or too few) past trades to calibrate against",
                    "raw": round(p, 4)}
        # evidence = past predictions within 5 points of this one (up to 250, nearest first); fewer than 20 of them -> the 20 nearest
        by_dist = sorted(pool, key=lambda pt: abs(pt[0] - p))
        close = [pt for pt in by_dist if abs(pt[0] - p) <= 0.05]
        near = close[:250] if len(close) >= 20 else by_dist[:20]
        wins = sum(pt[1] for pt in near)
        m = pc.prior_strength
        a, b = m * p + wins, m * (1 - p) + (len(near) - wins)
        mean = a / (a + b)
        sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        sd = math.sqrt(sd * sd + se_mc * se_mc)                   # the simulation's own noise on top
        rep = self.report()
        ece = rep.get("ece")
        grade = "high" if n_pool >= 150 and ece is not None and ece <= 0.06 else "medium" if n_pool >= 40 else "low"
        return {"p": round(mean, 4), "lo": round(max(0.0, mean - 1.645 * sd), 4), "hi": round(min(1.0, mean + 1.645 * sd), 4),
                "n_calibration": n_pool, "grade": grade, "raw": round(p, 4),
                "basis": f"{len(near)} nearest of {n_pool} past predictions realised {100 * wins / len(near):.0f}% "
                         f"against a model average of {100 * sum(pt[0] for pt in near) / len(near):.0f}%"}

    def report(self, bins: int = 5) -> dict[str, Any]:
        """Reliability table, Brier score (and its skill against always predicting the base rate) and expected calibration error."""
        pts = self.points
        if not pts:
            return {"n": 0}
        base = sum(w for _, w, _ in pts) / len(pts)
        brier = sum((p - w) ** 2 for p, w, _ in pts) / len(pts)
        ref = sum((base - w) ** 2 for _, w, _ in pts) / len(pts)
        srt = sorted(pts)
        rows, ece = [], 0.0
        for i in range(bins):
            chunk = srt[i * len(srt) // bins:(i + 1) * len(srt) // bins]
            if not chunk:
                continue
            pred = sum(p for p, _, _ in chunk) / len(chunk)
            real = sum(w for _, w, _ in chunk) / len(chunk)
            ece += abs(pred - real) * len(chunk) / len(srt)
            rows.append({"predicted": round(100 * pred, 1), "realised": round(100 * real, 1), "trades": len(chunk)})
        return {"n": len(pts), "base_rate": round(100 * base, 1), "brier": round(brier, 4),
                "brier_skill": round(1 - brier / ref, 3) if ref > 0 else None, "ece": round(ece, 4), "buckets": rows}

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "updated": self.updated, "points": [list(p) for p in self.points]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Calibration":
        c = Calibration(updated=d.get("updated"))
        for p in d.get("points", []):
            try:
                c.points.append((float(p[0]), int(p[1]), str(p[2]) if len(p) > 2 else ""))
            except (TypeError, ValueError, IndexError):
                continue
        return c

    def save(self, path: str | Path) -> None:
        self.updated = dt.datetime.now().isoformat(timespec="seconds")
        Path(path).write_text(json.dumps(self.to_dict()), encoding="utf-8")

    @staticmethod
    def load(path: str | Path) -> "Calibration":
        p = Path(path)
        if not p.exists():
            return Calibration()
        try:
            return Calibration.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return Calibration()


def _family(strategy: str) -> str:
    """Calibration families: structures with the same payoff shape behave alike, so their records are pooled."""
    if strategy in ("Bull put spread", "Bear call spread", "Iron condor"):
        return "credit"
    if strategy in ("Long future", "Short future"):
        return "futures"
    return "debit"


def family_of(strategy: str) -> str:
    return _family(strategy)


# ====================================================================== top-level entry for the selector
def refine(s: Structure, plan: ExitPlan, chain: Chain, iv_atm: float, rv20: Optional[float], rv60: Optional[float],
           mkt: MarketConfig, cm: CostModel, pc: ProbConfig = ProbConfig(), calibration: Optional[Calibration] = None,
           hist_returns: Optional[list[float]] = None, paths: Optional[int] = None) -> dict[str, Any]:
    """The full `win` block for one suggestion: every layer, the managed simulation and the calibrated probability."""
    sig_rw = rw_sigma(iv_atm, rv20, rv60, pc)
    mc = managed_mc(s, plan, chain, iv_atm, sig_rw, mkt, cm, pc, hist_returns, paths)
    cal = (calibration or Calibration()).apply(mc["p_win"], family_of(s.name), mc["se"], pc)
    return {"managed": mc, "calibrated": cal, "sigma_rw": round(sig_rw, 4), "sigma_iv": round(iv_atm, 4)}


# ====================================================================== honest evaluation
def wilson(wins: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval (default 90%) for a win rate. A raw '62% over 14 trades' is 'somewhere from 40% to 80%'."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_mean_ci(xs: list[float], reps: int = 1000, seed: int = 5, level: float = 0.90) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean. Deterministic."""
    if len(xs) < 2:
        return (xs[0], xs[0]) if xs else (0.0, 0.0)
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    lo, hi = (1 - level) / 2, 1 - (1 - level) / 2
    return means[int(lo * reps)], means[min(reps - 1, int(hi * reps))]


def walk_forward_check(trades: list[dict[str, Any]], key: str = "p_win", warmup: int = 20,
                       pc: ProbConfig = ProbConfig()) -> dict[str, Any]:
    """Does calibration actually help OUT OF SAMPLE? Trades are taken in date order; each is predicted using a Calibration built
    only from the trades before it. Reports Brier scores for the raw model probability and the calibrated one over the trades
    after `warmup`. If calibrated is not lower than raw, the shrinkage is not earning its keep on this data."""
    rows = sorted((t for t in trades if t.get(key) is not None and t.get("exit_reason") not in (None, "open", "no_data")),
                  key=lambda t: (t.get("entry_date") or "", t.get("symbol") or ""))
    if len(rows) <= warmup:
        return {"n": len(rows), "note": f"need more than {warmup} closed trades with a model probability"}
    cal = Calibration()
    raw_b = cal_b = 0.0
    k = 0
    for i, t in enumerate(rows):
        won = t["net_pnl"] > 0
        if i >= warmup:
            p = t[key]
            pc_ = cal.apply(p, family_of(t.get("strategy", "")), 0.0, pc)["p"]
            raw_b += (p - won) ** 2
            cal_b += (pc_ - won) ** 2
            k += 1
        cal.add(t[key], won, family_of(t.get("strategy", "")))
    return {"n": k, "brier_raw": round(raw_b / k, 4), "brier_calibrated": round(cal_b / k, 4),
            "calibration_helps": cal_b < raw_b, "base_rate": round(100 * sum(1 for t in rows if t["net_pnl"] > 0) / len(rows), 1)}
