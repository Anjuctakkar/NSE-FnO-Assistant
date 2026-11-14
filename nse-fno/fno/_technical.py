"""
Bundled copy of the five indicator functions the F&O framework uses (sma, ema, rsi, macd, latest_value), taken
unchanged from the NSE dashboard's technical.py, so `fno` runs on its own.

signals.py prefers the dashboard's technical.py when it is importable (so both always agree) and falls back to this
copy otherwise. tests/test_indicators.py checks the two give identical results; if you change RSI or MACD in the
dashboard, the F&O signals follow automatically when installed beside it. Standalone, they use these.
"""

from typing import Optional


def sma(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < window:
            continue
        chunk = values[i + 1 - window : i + 1]
        if any(v is None for v in chunk):
            continue
        out[i] = sum(chunk) / window
    return out


def ema(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    k = 2 / (window + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            out[i] = None
            continue
        prev = v if prev is None else (v * k + prev * (1 - k))
        out[i] = prev
    return out


def rsi(values: list[Optional[float]], window: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    gains: list[Optional[float]] = [None] * len(values)
    losses: list[Optional[float]] = [None] * len(values)

    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        if a is None or b is None:
            continue
        change = b - a
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    for i in range(len(values)):
        if i + 1 < window + 1:
            continue
        g_chunk = gains[i + 1 - window : i + 1]
        l_chunk = losses[i + 1 - window : i + 1]
        if any(g is None for g in g_chunk) or any(l is None for l in l_chunk):
            continue
        avg_gain = sum(g_chunk) / window
        avg_loss = sum(l_chunk) / window
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def macd(
    values: list[Optional[float]], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def latest_value(values: list[Optional[float]]) -> Optional[float]:
    for v in reversed(values):
        if v is not None:
            return v
    return None

