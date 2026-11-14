"""
Black-Scholes-Merton pricing, Greeks and implied volatility (pure Python, no scipy).

Conventions (chosen so numbers read the way a trader reads them):
  * T is in years (calendar days / 365), sigma is annual volatility as a fraction (0.25 = 25%).
  * delta   per 1 unit of underlying price.
  * gamma   change in delta per 1 unit of underlying price.
  * theta   rupees of value lost per CALENDAR DAY (negative for long options).
  * vega    rupees of value gained per 1 volatility POINT (1 percentage point of IV).
  * rho     rupees per 1 percentage point of interest rate.
NSE stock options are American-style in principle but European-style on the exchange for index options and
cash/physically settled at expiry for stocks; early exercise is rare, so the European model is the standard
working approximation here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

SQRT2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1 vol point
    rho: float     # per 1 rate point


def _intrinsic(S: float, K: float, kind: str) -> float:
    return max(S - K, 0.0) if kind == "CE" else max(K - S, 0.0)


def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def bs_price(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Option price. kind is 'CE' (call) or 'PE' (put). At or past expiry (or zero vol) it is intrinsic value."""
    if T <= 0 or sigma <= 0:
        return _intrinsic(S, K, kind)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    if kind == "CE":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> Greeks:
    """Price and Greeks for one option. At expiry the Greeks collapse to the limiting values."""
    if T <= 0 or sigma <= 0:
        itm = S > K if kind == "CE" else S < K
        return Greeks(_intrinsic(S, K, kind), (1.0 if kind == "CE" else -1.0) if itm else 0.0, 0.0, 0.0, 0.0, 0.0)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    sq = math.sqrt(T)
    eq, er = math.exp(-q * T), math.exp(-r * T)
    pdf = norm_pdf(d1)
    gamma = eq * pdf / (S * sigma * sq)
    vega = S * eq * pdf * sq / 100.0
    decay = -(S * eq * pdf * sigma) / (2.0 * sq)
    if kind == "CE":
        price = S * eq * norm_cdf(d1) - K * er * norm_cdf(d2)
        delta = eq * norm_cdf(d1)
        theta = decay - r * K * er * norm_cdf(d2) + q * S * eq * norm_cdf(d1)
        rho = K * T * er * norm_cdf(d2) / 100.0
    else:
        price = K * er * norm_cdf(-d2) - S * eq * norm_cdf(-d1)
        delta = -eq * norm_cdf(-d1)
        theta = decay + r * K * er * norm_cdf(-d2) - q * S * eq * norm_cdf(-d1)
        rho = -K * T * er * norm_cdf(-d2) / 100.0
    return Greeks(price, delta, gamma, theta / 365.0, vega, rho)


def implied_vol(price: float, S: float, K: float, T: float, r: float, q: float, kind: str,
                lo: float = 1e-3, hi: float = 5.0, tol: float = 1e-6) -> Optional[float]:
    """Implied volatility by bisection (price is monotonic in sigma). None when the price is at or below
    intrinsic value (no volatility explains it) or above the model's maximum."""
    if T <= 0 or price <= 0 or S <= 0 or K <= 0:
        return None
    forward_intrinsic = _intrinsic(S * math.exp(-q * T), K * math.exp(-r * T), kind)
    if price <= forward_intrinsic + 1e-9:
        return None
    if bs_price(S, K, T, r, q, hi, kind) < price:
        return None
    a, b = lo, hi
    for _ in range(100):
        mid = 0.5 * (a + b)
        if bs_price(S, K, T, r, q, mid, kind) < price:
            a = mid
        else:
            b = mid
        if b - a < tol:
            break
    return 0.5 * (a + b)


def prob_above(S: float, level: float, T: float, r: float, q: float, sigma: float) -> float:
    """Model-implied (risk-neutral) probability that the underlying finishes above `level` at expiry."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > level else 0.0
    _, d2 = _d1_d2(S, level, T, r, q, sigma)
    return norm_cdf(d2)
