"""
A compact daily summary of every stock, built from the stored bhavcopy, so the market scan can look at the whole
market without re-reading a year of full option files for ~200 stocks.

One row per stock per day:

    date, symbol, spot (underlying close), expiry (the option expiry summarised: nearest with at least 7 days left),
    atm_iv (average implied vol of the at-the-money call and put), call_oi, put_oi (that expiry), atm_oi, atm_volume,
    opt_volume (all option contracts traded that day, a liquidity measure), fut_expiry / fut_price / fut_oi (the
    near-month future), lot.

The ATM IV uses the same expiry rule as BhavcopyProvider.atm_iv_history, so IV rank from the index matches IV rank from
a full chain. Files are written one per day under <store>/summary/ and rebuilt only for days that lack one.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from . import greeks as bs
from .bhavcopy import BhavcopyStore, num
from .config import MarketConfig

COLUMNS = ["date", "symbol", "spot", "expiry", "atm_iv", "call_oi", "put_oi", "atm_oi", "atm_volume", "opt_volume", "fut_expiry", "fut_price", "fut_oi", "lot"]
MIN_DTE = 7


def build_day_summary(rows: list[dict[str, Any]], mkt: MarketConfig = MarketConfig()) -> list[dict[str, Any]]:
    """Canonical rows of ONE day (all stocks) -> one summary row per stock that has a usable chain."""
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    out = []
    for sym, rs in by_sym.items():
        d = rs[0]["date"]
        spot = next((r["under"] for r in rs if r["under"]), None)
        opts, futs = [r for r in rs if r["inst"] == "OPT"], [r for r in rs if r["inst"] == "FUT"]
        exps = sorted({r["expiry"] for r in opts if (r["expiry"] - d).days >= MIN_DTE})
        if not spot or not exps:
            continue
        ex = exps[0]
        chain = [r for r in opts if r["expiry"] == ex and r["strike"]]
        strikes = sorted({r["strike"] for r in chain if r["kind"] == "CE"} & {r["strike"] for r in chain if r["kind"] == "PE"})
        if not strikes:
            continue
        atm = min(strikes, key=lambda k: abs(k - spot))
        T = (ex - d).days / 365.0
        ivs = [bs.implied_vol(r["price"], spot, atm, T, mkt.risk_free, mkt.div_yield, r["kind"]) for r in chain if r["strike"] == atm]
        ivs = [v for v in ivs if v]
        fut = None
        live = sorted((r for r in futs if r["expiry"] >= d), key=lambda r: r["expiry"])
        if live:
            fut = live[0]
        out.append({"date": d, "symbol": sym, "spot": spot, "expiry": ex, "atm_iv": sum(ivs) / len(ivs) if ivs else None,
                    "call_oi": sum(r["oi"] for r in chain if r["kind"] == "CE"), "put_oi": sum(r["oi"] for r in chain if r["kind"] == "PE"),
                    "atm_oi": sum(r["oi"] for r in chain if r["strike"] == atm), "atm_volume": sum(r["volume"] for r in chain if r["strike"] == atm),
                    "opt_volume": sum(r["volume"] for r in opts), "fut_expiry": fut["expiry"] if fut else None,
                    "fut_price": fut["price"] if fut else None, "fut_oi": fut["oi"] if fut else None,
                    "lot": next((r["lot"] for r in rs if r.get("lot")), None)})
    return out


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for r in sorted(rows, key=lambda x: x["symbol"]):
                w.writerow(["" if r[c] is None else (r[c].isoformat() if isinstance(r[c], dt.date) else r[c]) for c in COLUMNS])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read(path: Path) -> list[dict[str, Any]]:
    out = []
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            day = lambda v: dt.date.fromisoformat(v) if v else None            # noqa: E731
            out.append({"date": day(r["date"]), "symbol": r["symbol"], "spot": num(r["spot"]), "expiry": day(r["expiry"]), "atm_iv": num(r["atm_iv"]),
                        "call_oi": int(num(r["call_oi"]) or 0), "put_oi": int(num(r["put_oi"]) or 0), "atm_oi": int(num(r["atm_oi"]) or 0),
                        "atm_volume": int(num(r["atm_volume"]) or 0), "opt_volume": int(num(r["opt_volume"]) or 0), "fut_expiry": day(r["fut_expiry"]),
                        "fut_price": num(r["fut_price"]), "fut_oi": int(num(r["fut_oi"])) if num(r["fut_oi"]) is not None else None,
                        "lot": int(num(r["lot"])) if num(r["lot"]) else None})
    return out


class SummaryIndex:
    def __init__(self, store: BhavcopyStore, mkt: MarketConfig = MarketConfig()):
        self.store, self.mkt = store, mkt
        self.root = store.root.parent / "summary"

    def path_for(self, d: dt.date) -> Path:
        return self.root / f"{d.isoformat()}.csv.gz"

    def missing_days(self) -> list[dt.date]:
        return [d for d in self.store.dates() if not self.path_for(d).exists()]

    def build(self, max_days: Optional[int] = None, progress: Optional[Callable[[dt.date], None]] = None) -> dict[str, int]:
        """Summarise days that have no summary yet, oldest first. Returns {built, remaining}. Safe to call repeatedly."""
        todo = self.missing_days()
        batch = todo[:max_days] if max_days else todo
        for d in batch:
            _write(self.path_for(d), build_day_summary(self.store.read_day(d), self.mkt))
            if progress:
                progress(d)
        return {"built": len(batch), "remaining": len(todo) - len(batch)}

    def load(self, upto: Optional[dt.date] = None, lookback_days: int = 300) -> dict[str, list[dict[str, Any]]]:
        """symbol -> that stock's summary rows, oldest first, for the last `lookback_days` stored days up to `upto`."""
        days = [d for d in self.store.dates() if (upto is None or d <= upto)][-lookback_days:]
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for d in days:
            p = self.path_for(d)
            if not p.exists():
                continue
            for r in _read(p):
                out[r["symbol"]].append(r)
        return out
