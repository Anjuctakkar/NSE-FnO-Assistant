"""Price data helpers. make_synthetic_bars produces a regime-switching random walk for demos and tests only:
results on it say nothing about real markets. bars_from_csv loads real daily bars (date,open,high,low,close)."""
from __future__ import annotations

import csv
import datetime as dt
import math
import random
from pathlib import Path
from typing import Any


def make_synthetic_bars(n: int = 700, seed: int = 1, start: dt.date = dt.date(2023, 1, 2), s0: float = 1000.0,
                        annual_vol: float = 0.28) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    bars: list[dict[str, Any]] = []
    price, d = s0, start
    while len(bars) < n:
        seg_len = rng.randint(40, 120)
        mu = rng.choice([0.35, 0.0, -0.30, 0.15])          # annual drift of this regime
        vol = annual_vol * rng.choice([0.8, 1.0, 1.3])
        sd = vol / math.sqrt(252)
        for _ in range(seg_len):
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            o = price * (1 + rng.gauss(0, 0.3 * sd))
            c = price * math.exp(mu / 252 - 0.5 * sd * sd + sd * rng.gauss(0, 1))
            hi = max(o, c) * (1 + abs(rng.gauss(0, 0.5 * sd)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.5 * sd)))
            bars.append({"date": d, "open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2), "close": round(c, 2)})
            price = c
            d += dt.timedelta(days=1)
            if len(bars) >= n:
                break
    return bars


def bars_from_csv(path: str | Path) -> list[dict[str, Any]]:
    """Load daily bars from a CSV with a header containing date, open, high, low, close (any case)."""
    out: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = {k.strip().lower(): v for k, v in row.items()}
            try:
                out.append({"date": dt.date.fromisoformat(r["date"][:10]), "open": float(r["open"]), "high": float(r["high"]),
                            "low": float(r["low"]), "close": float(r["close"])})
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda b: b["date"])
    return out
