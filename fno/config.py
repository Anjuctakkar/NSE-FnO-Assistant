"""
Configuration for the F&O framework. Everything a user may want to tune lives here, in frozen
dataclasses, so a backtest is reproducible from (data, MarketConfig, CostModel, StrategyConfig).

Values marked "verify" are defaults that change over time (lot sizes, expiry day, exchange fees).
Check them against NSE circulars and your broker's contract note before trusting a P&L figure.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketConfig:
    risk_free: float = 0.065          # annual, continuously compounded proxy; set to the current T-bill yield
    div_yield: float = 0.0            # annual; stock dividends are ignored by default
    expiry_weekday: int = 1           # Mon=0 ... Tue=1. NSE monthly expiry is the last Tuesday (verify)
    lot_sizes: dict = field(default_factory=dict)   # {"TCS": 175, ...}. Use NSE's current list (verify)
    lot_target_value: float = 1_000_000  # fallback lot sizing only, when a symbol is missing from lot_sizes
    holidays: frozenset = frozenset()    # dates that move an expiry back to the previous trading day


@dataclass(frozen=True)
class CostModel:
    """Per-order charges for NSE F&O. STT rates are from the Finance Act 2026 (effective 1 Apr 2026).
    The other rates are approximate: verify against your broker's contract note."""
    brokerage_per_order: float = 20.0         # flat Rs per executed order (per leg)
    stt_futures_sell_pct: float = 0.05        # % of traded value, sell side
    stt_option_sell_pct: float = 0.15         # % of premium, sell side
    stt_option_exercise_pct: float = 0.15     # % of intrinsic value, when a long ITM option is exercised at expiry
    exch_futures_pct: float = 0.00183         # % of traded value (verify)
    exch_option_pct: float = 0.03503          # % of premium (verify)
    sebi_per_crore: float = 10.0              # Rs per crore of turnover
    stamp_buy_futures_pct: float = 0.002      # buy side only (verify)
    stamp_buy_option_pct: float = 0.003       # buy side only, on premium (verify)
    gst_pct: float = 18.0                     # on brokerage + exchange + SEBI
    slippage_half_spread_frac: float = 1.0    # fraction of the half bid-ask spread paid on every fill
    assumed_spread_pct: float = 3.0           # bid-ask spread (% of mid) assumed when a quote has no bid/ask
    futures_slippage_pct: float = 0.03        # % of price, per fill, for futures
    tick: float = 0.05                        # NSE option price tick (Rs); fills snap to it in the adverse direction
    min_assumed_spread_ticks: float = 2.0     # when no bid/ask is known, never assume a spread tighter than this many ticks:
                                              # a percentage spread on a Rs 1 wing is below one tick, which does not exist


@dataclass(frozen=True)
class StrategyConfig:
    # Expiry selection (calendar days to expiry)
    min_dte: int = 12                 # skip expiries closer than this when picking the trade expiry
    max_dte: int = 45
    min_dte_credit: int = 10          # short-gamma structures are not opened inside this window
    exit_dte: int = 5                 # close positions with this many days (or fewer) left: avoids pin risk
                                      # and physical settlement of stock options
    # IV regime (IV rank, 0-100)
    iv_high: float = 50.0             # at or above: premium is rich, prefer selling spreads
    iv_low: float = 30.0              # at or below: premium is cheap, prefer buying
    # Strike selection by absolute delta
    long_leg_delta: float = 0.60      # debit spreads / long options: long leg
    short_leg_delta: float = 0.30     # debit spreads: short leg
    credit_short_delta: float = 0.25  # credit spreads: short strike
    credit_long_delta: float = 0.10   # credit spreads: protective wing
    condor_short_delta: float = 0.20
    condor_long_delta: float = 0.08
    # Hard filters
    max_spread_pct: float = 8.0       # reject if any leg's bid-ask spread exceeds this % of mid (when known)
    min_oi: int = 0                   # reject if any leg's open interest is below this (when known)
    min_volume: int = 0               # reject if any leg traded fewer contracts than this on the day (when known)
    min_pop_credit: float = 0.55      # model-implied probability of profit for credit structures
    min_credit_to_width: float = 0.20 # credit / spread width for credit spreads
    min_rr_debit: float = 0.8         # max profit / max loss for debit spreads
    max_theta_decay_pct_day: float = 3.0  # |theta per day| as % of the debit paid, for long-premium structures
    # Sizing
    risk_budget: float | None = None  # Rs max loss per trade. None = 1 lot. If one lot risks more, the trade is rejected
    # Exit rules (evaluated on daily closes)
    credit_profit_frac: float = 0.50  # close a credit structure at this fraction of max profit
    credit_stop_mult: float = 1.5     # ... or when the loss reaches this multiple of the credit received
    debit_profit_frac: float = 0.60   # debit spread: fraction of max profit
    long_option_profit_frac: float = 0.80  # long option: fraction of premium paid, as profit
    debit_stop_frac: float = 0.50     # debit structures: loss as a fraction of premium paid
    invalidation_min_atr: float = 2.0 # the technical invalidation level is never closer than this many ATRs to the entry price:
                                      # a 10-bar swing low can sit inside one day's noise, which turns the rule into a coin-flip stop
    futures_stop_atr: float = 2.0
    futures_target_atr: float = 3.0
    allow_futures: bool = True
    futures_min_trend_score: int = 3
    # Trend-quality and volatility gates (added: the 4 trend votes are all functions of price momentum, so they overstate
    # independent evidence; ADX and the variance premium add information the score did not have)
    adx_trend_ok: float = 20.0        # ADX at or above: a directional trade is entering a real trend
    adx_range_max: float = 25.0       # an iron condor wants a range: ADX at or below this scores full marks
    adx_condor_block: float = 32.0    # ... and is refused above this (a strangle against a strong trend)
    min_vrp_credit: float = -0.02     # credit structures: refuse when IV sits more than this far BELOW realised vol (selling cheap)
    max_vrp_debit: float = 0.10       # long-premium structures: refuse when IV is this far ABOVE realised vol (paying rich) ...
    max_vrp_debit_ivr: float = 40.0   # ... unless IV rank is at or below this
    managed_paths: int = 400          # Monte-Carlo paths used to refine the finalists' win probability (0 = skip the simulation)
    min_ev_r: float = -0.15           # reject when the model's expected net P&L is worse than this many risk units (after costs)
    # Selection
    min_score: float = 55.0
    futures_min_score: float = 50.0   # futures have undefined risk (gaps can pass the stop) and no Greek confirmation;
                                      # they score lower by construction, so this is their own bar


@dataclass(frozen=True)
class ProbConfig:
    """Assumptions of the win-probability model (probability.py). They are assumptions, not facts: the calibration step
    (calibration.py) exists to measure how well they hold on the user's own data and to shrink the stated probability toward
    what actually happened."""
    drift: float = 0.08               # real-world annual drift of the stock. NOT the risk-free rate: that is the risk-neutral drift
    iv_weight: float = 0.6            # real-world volatility = iv_weight * ATM IV + (1 - iv_weight) * realised-vol forecast
                                      # (implied vol exceeds the vol that follows it on average: the variance risk premium)
    rv_blend20: float = 0.6           # realised forecast = rv_blend20 * rv20 + (1 - rv_blend20) * rv60
    t_df: float = 5.0                 # Student-t degrees of freedom for daily shocks (fat tails); 0 = normal
    ivol_sd: float = 0.5              # annualised sd of log-IV changes in the managed-trade simulation
    ivol_kappa: float = 4.0           # mean reversion of log IV per year
    spot_iv_corr: float = -0.5        # leverage effect: stock falls come with IV rises
    paths: int = 400                  # Monte-Carlo paths for the managed-trade simulation (antithetic pairs)
    seed: int = 11
    model_uncertainty: float = 0.12   # 90% half-width of the interval until a record exists (uncalibrated buckets missed by 4 to 20 points)
    prior_strength: float = 30.0      # calibration: the model probability counts as this many trades of evidence
