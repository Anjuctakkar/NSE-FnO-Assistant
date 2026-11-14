"""Expiry calendar helpers. NSE stock derivatives expire monthly; the weekday is configurable (verify against
the current NSE circular). Exchange holidays are not known here: pass them in MarketConfig.holidays."""
from __future__ import annotations

import datetime as dt
import math

from .config import MarketConfig


def last_weekday_of_month(year: int, month: int, weekday: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


def _roll_back_for_holiday(d: dt.date, holidays: frozenset) -> dt.date:
    while d.weekday() >= 5 or d in holidays:
        d -= dt.timedelta(days=1)
    return d


def monthly_expiries(asof: dt.date, n: int = 3, mkt: MarketConfig = MarketConfig()) -> list[dt.date]:
    """The next `n` monthly expiries on or after `asof`."""
    out: list[dt.date] = []
    y, m = asof.year, asof.month
    while len(out) < n:
        e = _roll_back_for_holiday(last_weekday_of_month(y, m, mkt.expiry_weekday), mkt.holidays)
        if e >= asof:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def dte(asof: dt.date, expiry: dt.date) -> int:
    """Calendar days to expiry."""
    return (expiry - asof).days


def years(asof: dt.date, expiry: dt.date) -> float:
    return max(0.0, (expiry - asof).days / 365.0)


_NICE = [0.5, 1, 2.5, 5, 10, 20, 25, 50, 100, 250, 500, 1000]


def nice_strike_step(spot: float) -> float:
    """A round strike interval of roughly 1% of spot. (NSE publishes the real intervals; a provider with real
    chains uses those and never calls this.)"""
    target = spot * 0.01
    return min(_NICE, key=lambda s: abs(math.log(s / target)))


def default_lot_size(spot: float, target_value: float = 1_000_000) -> int:
    """Fallback only: a lot whose contract value is near `target_value`, rounded to 5 units."""
    return max(1, int(round(target_value / spot / 5.0)) * 5) if spot > 0 else 1
