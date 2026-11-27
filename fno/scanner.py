"""
Market scan: find trades across every F&O stock without being told which one to look at.

Two passes, because the full evaluation (strikes, Greeks, costs, OI walls) is too slow to run on ~200 stocks per click:

  Pass 1, cheap, every stock, from the summary index (summary.py):
      trend score from the stock's own closes, IV rank from its ATM-IV history, futures build-up, put/call OI ratio and
      liquidity. Each stock gets a shortlist score from how clearly these point somewhere:
          40  trend strength (or, for a sideways stock, whether IV is rich enough to sell)
          25  does open interest back that view (futures build-up, put/call tilt)
          20  how extreme IV rank is (a clear buy-premium or sell-premium setting)
          15  liquidity rank among all stocks
  Pass 2, thorough, the shortlist only: the same selector used for a single stock (rule matrix, strikes by delta, Greek
      and cost filters, exit plan) with open interest feeding the score. The best idea per stock is kept and the
      stocks are ranked by score.

Guards that matter on real data:
  * A one-day move of 25% or more in the underlying is treated as a probable split or bonus (bhavcopy prices are not
    adjusted), and only the days after it are used. A stock left with too few days is skipped and counted.
  * Stocks with no traded options or no ATM implied volatility are skipped and counted, never guessed at.

Limit: the technicals use the closes recorded in the bhavcopy (no intraday high or low), so ATR and swing levels are
close-to-close. The scan finds candidates; it cannot tell you whether the idea is good, and it is not backtested as a
whole (a single stock can be backtested from its idea).
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from .bhavcopy import BhavcopyStore
from .config import CostModel, MarketConfig, StrategyConfig
from .data import BhavcopyProvider
from .models import Suggestion
from .oi import classify_buildup
from .selector import SuggestResult, suggest
from .signals import Regime, compute_regime


@dataclass(frozen=True)
class ScanConfig:
    top_candidates: int = 20       # how many stocks get the full evaluation
    top_n: int = 10                # how many ideas are returned
    min_history: int = 60          # trading days of prices needed (same as a single-stock suggestion)
    jump: float = 0.25             # a one-day move this big is treated as a probable split or bonus
    lookback: int = 300            # days of summaries to read


@dataclass
class Row:
    symbol: str
    regime: Regime
    spot: float
    pcr: Optional[float]
    buildup: Optional[str]
    buildup_strength: Optional[str]
    fut_price_chg: Optional[float]
    fut_oi_chg: Optional[float]
    opt_volume: int
    atm_oi: int
    liq_pct: float = 0.0
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    iv_history: list[float] = field(default_factory=list)
    bars: list[dict[str, Any]] = field(default_factory=list)
    live_chg: Optional[float] = None                        # today's move vs the last stored close, set by rerank_with_live
    eod_rank: Optional[int] = None                          # position before live prices were applied (1 = best)


@dataclass
class ScanResult:
    asof: dt.date
    universe: int = 0                                      # stocks with a chain on this day
    rows: list[Row] = field(default_factory=list)          # every stock that passed pass 1, best first
    skipped: dict[str, int] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    ideas: list[tuple[Suggestion, Row, list[dict[str, Any]]]] = field(default_factory=list)   # (best idea, its stock's pass-1 row, other ideas for that stock)
    nothing_passed: int = 0                                # shortlisted stocks where no trade survived the filters
    market: dict[str, Any] = field(default_factory=dict)


def _skip(res: ScanResult, why: str) -> None:
    res.skipped[why] = res.skipped.get(why, 0) + 1


def shortlist_score(row: Row, strat: StrategyConfig) -> tuple[float, dict[str, float]]:
    g = row.regime
    d = 1 if g.trend == "bullish" else -1 if g.trend == "bearish" else 0
    ivr = g.iv_rank
    trend = 40.0 * abs(g.trend_score) / 4 if d else (20.0 if ivr is not None and ivr >= strat.iv_high else 0.0)
    align = 0.0
    if row.buildup:
        sign = 1.0 if row.buildup in ("long_buildup", "short_covering") else -1.0
        weight = (1.0 if row.buildup in ("long_buildup", "short_buildup") else 0.5) * (1.0 if row.buildup_strength == "strong" else 0.7)
        align = sign * weight * d if d else -abs(sign * weight) * 0.5           # a build-up contradicts a sideways view
    if d and row.pcr is not None:
        align += d * (0.3 if row.pcr >= 1.2 else -0.3 if row.pcr <= 0.7 else 0.0)
    oi_part = (max(-1.0, min(1.0, align)) + 1) / 2 * 25.0
    iv_part = 20.0 * abs(ivr - 50.0) / 50.0 if ivr is not None else 0.0
    liq_part = 15.0 * row.liq_pct
    parts = {"trend": round(trend, 1), "oi": round(oi_part, 1), "iv": round(iv_part, 1), "liquidity": round(liq_part, 1)}
    return trend + oi_part + iv_part + liq_part, parts


def pass_one(data: dict[str, list[dict[str, Any]]], asof: dt.date, strat: StrategyConfig, mkt: MarketConfig, scfg: ScanConfig) -> ScanResult:
    """Every stock from the summary index. `data` is SummaryIndex.load(): symbol -> daily rows, oldest first."""
    res = ScanResult(asof)
    rows: list[Row] = []
    for sym, hist in data.items():
        hist = [r for r in hist if r["date"] <= asof]
        if not hist or hist[-1]["date"] != asof:
            continue                                                          # not traded in F&O on this day
        res.universe += 1
        today = hist[-1]
        if not today["opt_volume"] or not today["atm_oi"]:
            _skip(res, "no traded options"); continue
        if today["atm_iv"] is None:
            _skip(res, "no ATM implied volatility"); continue
        cut = 0
        for i in range(1, len(hist)):
            if hist[i - 1]["spot"] and abs(hist[i]["spot"] / hist[i - 1]["spot"] - 1) >= scfg.jump:
                cut = i
        series = hist[cut:]
        if len(series) < scfg.min_history:
            _skip(res, "probable split or bonus" if cut else "less than %d days of history" % scfg.min_history); continue
        bars = [{"date": r["date"], "open": r["spot"], "high": r["spot"], "low": r["spot"], "close": r["spot"]} for r in series]
        iv_hist = [r["atm_iv"] for r in hist if r["atm_iv"]][-252:]
        regime = compute_regime(bars, iv_hist, today["atm_iv"])
        if regime is None:
            _skip(res, "indicators not ready"); continue
        prev = hist[-2] if len(hist) > 1 else None
        p_chg = o_chg = None
        if prev and today["fut_price"] and prev["fut_price"] and today["fut_expiry"] == prev["fut_expiry"] and today["fut_oi"] is not None and prev["fut_oi"]:
            p_chg, o_chg = today["fut_price"] / prev["fut_price"] - 1, today["fut_oi"] / prev["fut_oi"] - 1
        label, strength = classify_buildup(p_chg, o_chg)
        rows.append(Row(sym, regime, today["spot"], today["put_oi"] / today["call_oi"] if today["call_oi"] else None, label, strength, p_chg, o_chg,
                        today["opt_volume"], today["atm_oi"], iv_history=iv_hist, bars=bars))
    vols = sorted(r.opt_volume for r in rows)
    for r in rows:
        r.liq_pct = (sum(1 for v in vols if v <= r.opt_volume) - 1) / max(1, len(vols) - 1) if len(vols) > 1 else 1.0
        r.score, r.parts = shortlist_score(r, strat)
    res.rows = sorted(rows, key=lambda r: (-r.score, r.symbol))
    res.market = market_context(res.rows)
    return res


def rerank_with_live(rows: list[Row], quotes: dict[str, dict[str, Any]], today: dt.date, strat: StrategyConfig,
                     scfg: ScanConfig = ScanConfig()) -> list[Row]:
    """Pass 1 re-scored on CURRENT prices. The stored rows end at the last bhavcopy close; each stock with a live quote gets today's
    candle built from that quote (open, high, low and the last price as the close), its regime recomputed, and its shortlist score
    recomputed, so a stock that gapped or broke out today moves up (or down) the ranking instead of waiting for tonight's file.
    The stored IV history and open-interest context are unchanged: IV rank still compares with the stored daily history, the live ATM
    IV only enters at the full evaluation. A move of `scfg.jump` or more is treated as a probable corporate action and left as it was."""
    for i, r in enumerate(rows, 1):
        r.eod_rank = i
    out: list[Row] = []
    for r in rows:
        q = quotes.get(r.symbol)
        past = [b for b in r.bars if b["date"] < today]
        if not q or not q.get("ltp") or not past or not past[-1]["close"]:
            out.append(r)
            continue
        ltp, prev = q["ltp"], past[-1]["close"]
        if abs(ltp / prev - 1) >= scfg.jump:
            out.append(r)
            continue
        o = q.get("open") or ltp
        bar = {"date": today, "open": o, "high": max(x for x in (q.get("high"), o, ltp) if x),
               "low": min(x for x in (q.get("low"), o, ltp) if x), "close": ltp}
        bars = past + [bar]
        g = compute_regime(bars, r.iv_history, r.regime.iv)
        if g is None:
            out.append(r)
            continue
        nr = replace(r, regime=g, spot=ltp, bars=bars, live_chg=ltp / prev - 1)
        nr.score, nr.parts = shortlist_score(nr, strat)
        out.append(nr)
    out.sort(key=lambda x: (-x.score, x.symbol))
    return out


def market_context(rows: list[Row]) -> dict[str, Any]:
    """A few numbers for the top of the screen: what the whole market looks like today."""
    if not rows:
        return {}
    ivr = [r.regime.iv_rank for r in rows if r.regime.iv_rank is not None]
    calls = sum(1 for r in rows if r.pcr is not None)
    count = lambda f: sum(1 for r in rows if f(r))           # noqa: E731
    return {"stocks": len(rows), "bullish": count(lambda r: r.regime.trend == "bullish"), "bearish": count(lambda r: r.regime.trend == "bearish"),
            "neutral": count(lambda r: r.regime.trend == "neutral"), "above_sma20_pct": round(100 * count(lambda r: r.spot > r.regime.sma20) / len(rows), 1),
            "median_iv_rank": round(statistics.median(ivr), 1) if ivr else None, "median_pcr": round(statistics.median([r.pcr for r in rows if r.pcr is not None]), 2) if calls else None,
            "long_buildup": count(lambda r: r.buildup == "long_buildup"), "short_buildup": count(lambda r: r.buildup == "short_buildup"),
            "short_covering": count(lambda r: r.buildup == "short_covering"), "long_unwinding": count(lambda r: r.buildup == "long_unwinding")}


def scan(store: BhavcopyStore, data: dict[str, list[dict[str, Any]]], asof: dt.date, strat: StrategyConfig = StrategyConfig(), mkt: MarketConfig = MarketConfig(),
         cm: CostModel = CostModel(), scfg: ScanConfig = ScanConfig(), progress: Optional[Callable[[str], None]] = None,
         selected_expiry: Optional[dt.date] = None) -> ScanResult:
    res = pass_one(data, asof, strat, mkt, scfg)
    if selected_expiry is not None:
        available = {r["symbol"] for r in store.read_day(asof)
                     if r["inst"] == "OPT" and r["expiry"] == selected_expiry}
        before = len(res.rows)
        res.rows = [row for row in res.rows if row.symbol in available]
        res.universe = len(available)
        excluded = before - len(res.rows)
        if excluded:
            res.skipped["no contracts for selected expiry"] = excluded
        res.market = market_context(res.rows)
    if not res.rows:
        return res
    cands = res.rows[:scfg.top_candidates]
    res.candidates = [r.symbol for r in cands]
    if progress:
        progress(f"Evaluating {len(cands)} stocks in full…")
    days = [d for d in store.dates() if d <= asof]
    prev = days[-2] if len(days) > 1 else None
    syms = set(res.candidates)
    rows = store.read_day(asof, syms) + (store.read_day(prev, syms) if prev else [])
    prov = BhavcopyProvider.from_rows(rows, {r.symbol: r.bars for r in cands}, mkt, iv_history={r.symbol: r.iv_history for r in cands})
    found: list[tuple[Suggestion, Row, list[dict[str, Any]]]] = []
    for r in cands:
        out: SuggestResult = suggest(r.symbol, r.bars, prov, asof, strat, mkt, cm, top_n=3,
                                     selected_expiry=selected_expiry)
        if not out.suggestions:
            res.nothing_passed += 1
            continue
        best, rest = out.suggestions[0], out.suggestions[1:]
        found.append((best, r, [{"name": s.structure.name, "score": round(s.score, 1)} for s in rest]))
    res.ideas = sorted(found, key=lambda t: (-t[0].score, t[0].symbol))[:scfg.top_n]
    return res
