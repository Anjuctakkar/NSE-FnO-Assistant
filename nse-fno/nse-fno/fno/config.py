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


@dataclass(frozen=True)
class StrategyConfig:
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
    futures_stop_atr: float = 2.0
    futures_target_atr: float = 3.0
    allow_futures: bool = True
    futures_min_trend_score: int = 3
    # Selection
    min_score: float = 55.0
    futures_min_score: float = 50.0   # futures have undefined risk (gaps can pass the stop) and no Greek confirmation;
                                      # they score lower by construction, so this is their own bar
