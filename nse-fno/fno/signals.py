"""
Underlying and volatility regime, computed from the provider's bars (daily in bhavcopy mode; five-minute in Breeze-live mode).

Trend uses the same building blocks as the cash-market dashboard (SMA, RSI, MACD from technical.py, or the bundled
copy in _technical.py when it is not installed beside the dashboard), scored on a
-4..+4 scale so strength is visible, not just direction:

    +1 / -1   20-bar SMA above / below the 50-bar SMA
    +1 / -1   close above / below the 20-bar SMA
    +1 / -1   MACD above / below its signal line
    +1 / -1 / 0   RSI 50-70 / 30-50 / outside (overbought or oversold: no vote)

    score >= +2 bullish, <= -2 bearish, otherwise neutral.

Volatility regime: IV rank and IV percentile over the available ATM-IV history, and the variance risk premium
(ATM IV minus 20-bar realised volatility). In synthetic mode "IV" is a realised-volatility proxy (see data.py).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

def _load_indicators():
    """The dashboard's technical.py when it is on the path (so both agree), otherwise the bundled copy."""
    try:
        import technical as mod
        if all(callable(getattr(mod, f, None)) for f in ("sma", "ema", "rsi", "macd", "latest_value")):
            return mod, "dashboard technical.py"
    except ImportError:
        pass
    from . import _technical as mod
    return mod, "bundled"


ta, INDICATOR_SOURCE = _load_indicators()

MIN_BARS = 60


@dataclass
class Regime:
    asof: dt.date
    spot: float
    trend: str                      # "bullish" | "bearish" | "neutral"
    trend_score: int
    rsi: float
    macd_hist: float
    sma20: float
    sma50: float
    atr: float
    atr_pct: float
    rv20: Optional[float]           # annualised
    rv60: Optional[float]
    swing_low: float                # lowest low of the last 10 bars
    swing_high: float
    iv: Optional[float] = None
    iv_rank: Optional[float] = None         # 0-100
    iv_percentile: Optional[float] = None   # 0-100
    vrp: Optional[float] = None             # iv - rv20, in vol points as a fraction
    notes: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["asof"] = self.asof.isoformat()
        d.pop("notes")
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def realised_vol(closes: list[float], window: int, periods_per_year: int = 252,
                 sessions: Optional[list[dt.date]] = None) -> Optional[float]:
    if len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - 1, 0, -1):
        if sessions is not None and sessions[i] != sessions[i - 1]:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
        if len(rets) == window:
            break
    if len(rets) < window:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(periods_per_year)


def atr(bars: list[dict[str, Any]], window: int = 14) -> float:
    trs = []
    for i in range(len(bars) - window, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def iv_rank(history: list[float], current: float) -> Optional[float]:
    """Where current IV sits between the lowest and highest ATM IV of the history, 0-100."""
    if len(history) < 30:
        return None
    lo, hi = min(history), max(history)
    if hi - lo < 1e-9:
        return 50.0
    return round(max(0.0, min(100.0, 100.0 * (current - lo) / (hi - lo))), 1)     # today can sit outside the old range


def iv_percentile(history: list[float], current: float) -> Optional[float]:
    """Share of days in the history on which IV was below the current level, 0-100."""
    if len(history) < 30:
        return None
    return round(100.0 * sum(1 for x in history if x < current) / len(history), 1)


def compute_regime(bars: list[dict[str, Any]], iv_history: Optional[list[float]] = None,
                   iv_now: Optional[float] = None, periods_per_year: int = 252) -> Optional[Regime]:
    """Regime as of the last bar. Uses only the bars given: pass bars[:i+1] in a backtest. None if too little data."""
    if len(bars) < MIN_BARS:
        return None
    closes = [b["close"] for b in bars]
    s20, s50 = ta.sma(closes, 20), ta.sma(closes, 50)
    rsi = ta.rsi(closes, 14)
    macd_line, macd_sig = ta.macd(closes)
    last = lambda x: ta.latest_value(x)           # noqa: E731
    c, a20, a50, r, ml, ms = closes[-1], last(s20), last(s50), last(rsi), last(macd_line), last(macd_sig)
    if None in (a20, a50, r, ml, ms):
        return None

    score = (1 if a20 > a50 else -1) + (1 if c > a20 else -1) + (1 if ml > ms else -1)
    score += 1 if 50 <= r <= 70 else -1 if 30 <= r < 50 else 0
    trend = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"

    notes = []
    if r > 70:
        notes.append(f"RSI {r:.0f} is overbought: trend-following longs are extended.")
    elif r < 30:
        notes.append(f"RSI {r:.0f} is oversold: trend-following shorts are extended.")

    a = atr(bars)
    sessions = [b["date"] for b in bars] if periods_per_year > 252 else None
    reg = Regime(asof=bars[-1]["date"], spot=c, trend=trend, trend_score=score, rsi=r, macd_hist=ml - ms, sma20=a20,
                 sma50=a50, atr=a, atr_pct=a / c * 100.0, rv20=realised_vol(closes, 20, periods_per_year, sessions),
                 rv60=realised_vol(closes, 60, periods_per_year, sessions),
                 swing_low=min(b["low"] for b in bars[-10:]), swing_high=max(b["high"] for b in bars[-10:]), notes=notes)
    if iv_now:
        reg.iv = iv_now
        if iv_history:
            reg.iv_rank, reg.iv_percentile = iv_rank(iv_history, iv_now), iv_percentile(iv_history, iv_now)
        if reg.rv20:
            reg.vrp = iv_now - reg.rv20
    return reg
