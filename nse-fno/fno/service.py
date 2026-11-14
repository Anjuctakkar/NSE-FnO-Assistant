"""
Service layer: plain functions that take a request dict and return a JSON-safe dict. The standalone server
(fno/server.py) and the dashboard's FastAPI router (fno/api.py) are thin wrappers around these, so the UI talks to one
contract wherever it is hosted.

Data sources
    "bhavcopy"   real NSE F&O settlement prices from the BhavcopyStore (sync or import first).
    "synthetic"  Black-Scholes prices over a generated stock (DEMO, ALPHA, ...): shows the screens and checks the code,
                 says nothing about the market.

Cash-market bars (OHLC) for the technicals come from, in order: bars passed in (the dashboard supplies its adjusted
history), a SYMBOL.csv in bars_dir, or, failing both, the underlying close recorded in the bhavcopy itself. That last
fallback has no intraday high/low, so ATR and swing levels are close-to-close: a response says so when it applies.
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import greeks as bs
from .backtest import BacktestResult, edge_vs_random, run_backtest, summarize
from .bhavcopy import BhavcopyStore
from .config import CostModel, MarketConfig, StrategyConfig
from .data import BhavcopyProvider, SyntheticProvider
from .demo_data import bars_from_csv, make_synthetic_bars
from .evaluate import evaluate_entries
from .models import Structure, Suggestion
from .scanner import ScanConfig, market_context, scan as market_scan
from .selector import suggest
from .summary import SummaryIndex

DEMO_SYMBOLS = ["DEMO", "ALPHA", "BRAVO", "CHARLIE"]
MAX_SYNC_DAYS = 21          # calendar days per request: about 15 downloads, short enough for a proxy's timeout
MIN_HISTORY_DAYS = 60       # the trend rules need this many trading days of prices (SMA50 + MACD warm-up)
RECOMMENDED_HISTORY_DAYS = 250   # IV rank wants about a year
DEFAULT_HISTORY_CALENDAR_DAYS = 365
MAX_EVAL_ENTRIES = 300


class ServiceError(Exception):
    """A problem the user can act on; the message is safe to show."""


@dataclass
class Context:
    data_dir: Path
    mkt: MarketConfig = field(default_factory=MarketConfig)
    cm: CostModel = field(default_factory=CostModel)
    bars_dir: Optional[Path] = None
    store: Optional[BhavcopyStore] = None
    _cache: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.bars_dir = Path(self.bars_dir) if self.bars_dir else self.data_dir / "bars"
        self.store = self.store or BhavcopyStore(self.data_dir / "bhavcopy")


# ---------------------------------------------------------------- helpers
def jsonable(x: Any) -> Any:
    """Dates to ISO strings; inf and nan to None (JSON has neither)."""
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return x


def _f(req: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    v = req.get(key, default)
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ServiceError(f"'{key}' must be a number.")
    if not math.isfinite(x):
        raise ServiceError(f"'{key}' must be a finite number.")
    return min(max(x, lo), hi)


def _sym(req: dict[str, Any]) -> str:
    s = str(req.get("symbol", "")).strip().upper()
    if not s or len(s) > 20 or not all(c.isalnum() or c in "-&_" for c in s):
        raise ServiceError("Give an NSE symbol, for example TCS.")
    return s


def _source(req: dict[str, Any]) -> str:
    s = str(req.get("source", "bhavcopy")).lower()
    if s not in ("bhavcopy", "synthetic"):
        raise ServiceError("source must be 'bhavcopy' or 'synthetic'.")
    return s


def strategy_config(req: dict[str, Any]) -> StrategyConfig:
    rb = _f(req, "risk_budget", 0.0, 0.0, 1e9)
    return StrategyConfig(risk_budget=rb or None, min_score=_f(req, "min_score", 55.0, 0.0, 100.0),
                          allow_futures=bool(req.get("allow_futures", True)), min_oi=int(_f(req, "min_oi", 0, 0, 1e9)),
                          min_volume=int(_f(req, "min_volume", 0, 0, 1e9)))


def bars_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daily records as the dashboard's history tool returns them (keys in any case, numbers possibly strings) -> bars."""
    out = []
    for r in records or []:
        r = {str(k).lower(): v for k, v in r.items()}
        try:
            out.append({"date": dt.date.fromisoformat(str(r["date"])[:10]), "open": float(r.get("open") or r["close"]),
                        "high": float(r.get("high") or r["close"]), "low": float(r.get("low") or r["close"]), "close": float(r["close"])})
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda b: b["date"])
    return out


def demo_bars(symbol: str) -> list[dict[str, Any]]:
    h = zlib.crc32(symbol.encode())
    return make_synthetic_bars(700, seed=h % 997, s0=300 + h % 2700)


def bars_from_provider(prov: BhavcopyProvider, symbol: str) -> list[dict[str, Any]]:
    """Fallback bars from the underlying close in the bhavcopy: no intraday range, so high = low = open = close."""
    out = []
    for d in sorted(prov._dates.get(symbol, [])):
        s = prov._under.get((symbol, d))
        if s:
            out.append({"date": d, "open": s, "high": s, "low": s, "close": s})
    return out


def _load_csv_bars(ctx: Context, symbol: str) -> Optional[list[dict[str, Any]]]:
    p = ctx.bars_dir / f"{symbol}.csv"
    return bars_from_csv(p) if p.exists() else None


def get_provider(ctx: Context, source: str, symbol: str, bars: Optional[list[dict[str, Any]]], iv_scale: float = 1.0):
    """(provider, bars, notes). notes are caveats the UI must show next to any result built on this data."""
    notes: list[str] = []
    if source == "synthetic":
        if symbol not in DEMO_SYMBOLS:
            raise ServiceError(f"Synthetic mode only has {', '.join(DEMO_SYMBOLS)}.")
        b = demo_bars(symbol)
        notes.append("Synthetic data: option prices are Black-Scholes values from the stock's own realised volatility, and the stock is "
                     "generated. This shows how the screens and rules behave; it says nothing about the real market.")
        return SyntheticProvider(symbol, b, ctx.mkt, iv_scale=iv_scale), b, notes
    dates = ctx.store.dates()
    if not dates:
        raise ServiceError("No bhavcopy is stored yet. Use Sync (downloads from NSE) or Import (a file you downloaded in a browser).")
    key = ("bhav", symbol, dates[-1], len(dates))
    with ctx._lock:
        prov = ctx._cache.get(key)
        if prov is None:
            prov = BhavcopyProvider.from_store(ctx.store, {symbol}, mkt=ctx.mkt)
            ctx._cache = {k: v for k, v in ctx._cache.items() if k[0] != "bhav" or k[1] != symbol}
            ctx._cache[key] = prov
    if symbol not in prov.symbols():
        raise ServiceError(f"{symbol} has no stock option contracts in the stored bhavcopy ({dates[0]} to {dates[-1]}).")
    own = bars or _load_csv_bars(ctx, symbol)
    if own:
        prov.bars = {symbol: own}
    else:
        own = bars_from_provider(prov, symbol)
        prov.bars = {symbol: own}
        notes.append("No cash-market history was supplied, so the technicals use the underlying close recorded in the bhavcopy. It has no "
                     "intraday high/low: ATR and swing levels are close-to-close.")
    n = len(prov._dates[symbol])
    if n < 100:
        notes.append(f"Only {n} days of bhavcopy are stored for {symbol}. IV rank needs about a year of history to be meaningful, and a "
                     f"backtest over this span has few trades. Sync more history.")
    notes.append("Bhavcopy has settlement prices but no bid/ask or implied volatility: IV is solved from the settlement price, and "
                 "slippage uses an assumed spread. Contracts that did not trade carry a theoretical price (use the minimum-volume filter).")
    return prov, own, notes


def _asof(prov, symbol: str, req: dict[str, Any], bars: list[dict[str, Any]]) -> dt.date:
    avail = sorted(prov._dates[symbol]) if isinstance(prov, BhavcopyProvider) else [b["date"] for b in bars]
    if not avail:
        raise ServiceError(f"No data for {symbol}.")
    raw = req.get("asof")
    if not raw:
        return avail[-1]
    try:
        want = dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise ServiceError("asof must be a date like 2026-10-23.")
    ok = [d for d in avail if d <= want]
    if not ok:
        raise ServiceError(f"No data on or before {want}; the first date is {avail[0]}.")
    return ok[-1]


# ---------------------------------------------------------------- payoff and legs
def payoff_curves(s: Structure, spot: float, asof: dt.date, mkt: MarketConfig, iv: float, n: int = 121) -> dict[str, Any]:
    """P&L against the stock price at expiry, and 'now' (today's value of the legs at their current IV, Black-Scholes)."""
    T = max((s.expiry - asof).days, 1) / 365.0
    w = min(0.5, max(0.15, 2.0 * iv * math.sqrt(T)))
    strikes = [l.strike for l in s.legs if l.strike]
    lo = min([spot * (1 - w)] + [k * 0.97 for k in strikes])
    hi = max([spot * (1 + w)] + [k * 1.03 for k in strikes])
    xs = [round(lo + (hi - lo) * i / (n - 1), 2) for i in range(n)]       # round first, so every plotted point satisfies the payoff exactly
    expiry_pnl, now_pnl = [], []
    for S in xs:
        expiry_pnl.append(round(s.payoff(S), 2))
        v = 0.0
        for l in s.legs:
            u = s.units(l) * l.side
            if l.kind == "FUT":
                v += u * (S - l.entry_price)
            else:
                v += u * (bs.bs_price(S, l.strike, T, mkt.risk_free, mkt.div_yield, l.iv or iv, l.kind) - l.entry_price)
        now_pnl.append(round(v, 2))
    return {"price": xs, "expiry": expiry_pnl, "now": now_pnl, "spot": round(spot, 2)}


def leg_details(s: Structure, spot: float, asof: dt.date, mkt: MarketConfig) -> list[dict[str, Any]]:
    out = []
    for l in s.legs:
        d: dict[str, Any] = {"kind": l.kind, "side": l.side, "strike": l.strike, "price": l.entry_price, "iv": l.iv, "lots": l.lots,
                             "units": s.units(l), "expiry": l.expiry.isoformat()}
        if l.kind != "FUT":
            T = max(0.0, (l.expiry - asof).days / 365.0)
            g = bs.greeks(spot, l.strike, T, mkt.risk_free, mkt.div_yield, l.iv or 0.3, l.kind)
            d.update(delta=round(g.delta, 3), theta_day=round(g.theta, 3))
        else:
            d.update(delta=1.0, theta_day=0.0)
        out.append(d)
    return out


def suggestion_payload(sug: Suggestion, ctx: Context, spot: float, source: str) -> dict[str, Any]:
    d = sug.to_dict()
    iv = sug.risk.get("iv_atm") or 0.3
    d["source"] = source
    d["spot"] = round(spot, 2)
    d["legs_detail"] = leg_details(sug.structure, spot, sug.asof, ctx.mkt)
    d["payoff"] = payoff_curves(sug.structure, spot, sug.asof, ctx.mkt, iv)
    d["kind"] = ("futures" if sug.structure.has_future else "credit" if sug.risk["net_premium"] > 0 else "debit")
    return d


# ---------------------------------------------------------------- endpoints
def status(ctx: Context) -> dict[str, Any]:
    cov = ctx.store.coverage()
    dates = ctx.store.dates()
    n = cov["days"]
    missing = len(SummaryIndex(ctx.store, ctx.mkt).missing_days()) if n else 0
    return jsonable({"bhavcopy": {**cov, "index_missing": missing, "dates": [d.isoformat() for d in dates[-90:]], "symbols": len(ctx.store.symbols()),
                                  "url_templates": ctx.store.url_templates, "min_days": MIN_HISTORY_DAYS, "recommended_days": RECOMMENDED_HISTORY_DAYS,
                                  "readiness": "empty" if n == 0 else "too_short" if n < MIN_HISTORY_DAYS else "thin" if n < RECOMMENDED_HISTORY_DAYS - 50 else "good"},
                     "demo_symbols": DEMO_SYMBOLS, "bars_dir": str(ctx.bars_dir), "has_cash_bars": ctx.bars_dir.exists(),
                     "disclaimer": "Information only, not investment advice."})


def list_symbols(ctx: Context, source: str) -> dict[str, Any]:
    return {"source": source, "symbols": DEMO_SYMBOLS if source == "synthetic" else ctx.store.symbols()}


def history_issue(ctx: Context, symbol: str, source: str, have: int, need: int, title: Optional[str] = None, file_hint: bool = True) -> dict[str, Any]:
    stored = len(ctx.store.dates()) if source == "bhavcopy" else None
    where = f"Only {have} trading days of prices were available for {symbol}" + (f" ({stored} days of bhavcopy are stored)." if stored is not None else ".")
    return {"code": "insufficient_history", "title": title or f"Not enough price history for {symbol}", "have": have, "need": need,
            "recommended": RECOMMENDED_HISTORY_DAYS, "fix": "sync" if source == "bhavcopy" else None, "stored_days": stored,
            "detail": f"{where} The trend rules need at least {need} (about three months) and IV rank needs about a year. "
                      f"This is a data problem, not a decision to stay out of the trade."
                      + (f" Download more history, or put a daily-price file named {symbol}.csv in {ctx.bars_dir}." if source == "bhavcopy" and file_hint else
                         " Download more history." if source == "bhavcopy" else "")}


def data_issue(ctx: Context, res, symbol: str, source: str, asof: dt.date) -> Optional[dict[str, Any]]:
    """A missing-data problem, described so a front end can say what is wrong and offer the fix. None for a real
    trading outcome (suggestions, or a genuine 'no trade')."""
    if res.code == "insufficient_history":
        return history_issue(ctx, symbol, source, res.have_bars, res.need_bars)
    if res.code == "no_chain":
        return {"code": res.code, "title": f"No option chain for {symbol} on {asof}", "fix": None,
                "detail": f"There are no option contracts for {symbol} on {asof}: it may not trade in F&O, or that day is missing from the stored data."}
    if res.code == "no_expiry":
        return {"code": res.code, "title": "No suitable expiry", "fix": None,
                "detail": f"No monthly expiry falls inside the 12 to 45 day window on {asof}. Try another date."}
    return None


def run_suggest(ctx: Context, req: dict[str, Any], bars: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    symbol, source = _sym(req), _source(req)
    iv_scale = _f(req, "iv_scale", 1.0, 0.5, 2.0)
    prov, bars, notes = get_provider(ctx, source, symbol, bars, iv_scale)
    asof = _asof(prov, symbol, req, bars)
    cfg = strategy_config(req)
    res = suggest(symbol, bars, prov, asof, cfg, ctx.mkt, ctx.cm, top_n=int(_f(req, "top_n", 3, 1, 6)))
    spot = prov.spot(symbol, asof) or 0.0
    issue = data_issue(ctx, res, symbol, source, asof)
    if issue and issue["code"] == "insufficient_history":
        notes = [n for n in notes if not n.startswith("Only ")]          # the issue panel already says it
    return jsonable({
        "symbol": symbol, "source": source, "asof": asof, "expiry": res.expiry, "note": res.note, "code": res.code,
        "issue": issue, "data_notes": notes, "oi": res.oi.to_dict() if res.oi else None,
        "regime": res.regime.snapshot() if res.regime else None, "regime_notes": res.regime.notes if res.regime else [],
        "suggestions": [suggestion_payload(s, ctx, spot, source) for s in res.suggestions],
        "rejected": [{"structure": n, "reason": w} for n, w in res.rejected],
        "config": {"risk_budget": cfg.risk_budget, "min_score": cfg.min_score, "allow_futures": cfg.allow_futures,
                   "min_oi": cfg.min_oi, "min_volume": cfg.min_volume}})


def equity_curve(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cum, pts = 0.0, []
    for t in sorted((t for t in trades if t["exit_date"]), key=lambda t: t["exit_date"]):
        cum += t["net_pnl"]
        pts.append({"date": t["exit_date"], "pnl": round(cum, 2)})
    return pts


def _pooled(runs: list[BacktestResult], symbol: str) -> BacktestResult:
    trades = [t for r in runs for t in r.trades]
    return BacktestResult(symbol, {**runs[0].params, "runs": len(runs)}, trades, summarize(trades), {})


TRADE_FIELDS = ("strategy", "entry_date", "exit_date", "exit_reason", "lots", "days_held", "net_pnl", "costs", "r_multiple", "score", "pop", "iv_rank", "trend")


def run_backtest_req(ctx: Context, req: dict[str, Any], bars: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    symbol, source = _sym(req), _source(req)
    iv_scale = _f(req, "iv_scale", 1.0, 0.5, 2.0)
    prov, bars, notes = get_provider(ctx, source, symbol, bars, iv_scale)
    if len(bars) < MIN_HISTORY_DAYS + 20:                      # 60 days warm the indicators; a backtest needs room after that
        return jsonable({"symbol": symbol, "source": source, "bars": len(bars), "data_notes": [n for n in notes if not n.startswith("Only ")],
                         "issue": history_issue(ctx, symbol, source, len(bars), MIN_HISTORY_DAYS + 20)})
    cfg = strategy_config(req)
    start = min(prov._dates[symbol]) if isinstance(prov, BhavcopyProvider) else None
    sig = run_backtest(symbol, bars, prov, cfg, ctx.mkt, ctx.cm, entry_mode="signal", start=start)
    runs = int(_f(req, "random_runs", 3, 0, 5))
    rnd_runs = [run_backtest(symbol, bars, prov, cfg, ctx.mkt, ctx.cm, entry_mode="random", seed=i + 1, start=start) for i in range(runs)]
    rnd = _pooled(rnd_runs, symbol) if rnd_runs else None
    edge = edge_vs_random(sig, rnd) if rnd else None
    if source == "synthetic":
        notes.append("The synthetic stock has persistent trend regimes, so a trend-following signal is expected to look good here. "
                     "That checks the machinery, not that there is an edge.")
    if sig.metrics.get("small_sample"):
        notes.append(f"Only {sig.metrics.get('trades', 0)} closed trades: fewer than 30 prove little.")
    notes.append("Daily closes only: intraday stop touches, gaps through a stop and events such as earnings are not modelled.")
    return jsonable({
        "symbol": symbol, "source": source, "bars": len(bars), "data_notes": notes, "params": sig.params,
        "signal": {"metrics": sig.metrics, "equity": equity_curve(sig.trades), "skipped": sig.skipped,
                   "trades": [{k: t[k] for k in TRADE_FIELDS} for t in sig.trades[-200:]]},
        "random": ({"metrics": rnd.metrics, "equity": equity_curve(rnd_runs[0].trades), "runs": runs} if rnd else None),
        "edge": edge})


def run_evaluate(ctx: Context, req: dict[str, Any]) -> dict[str, Any]:
    """Replay suggestions the browser sends (its own log; nothing is stored here) over the data that has arrived since."""
    entries = req.get("entries")
    if not isinstance(entries, list):
        raise ServiceError("Send the logged suggestions as 'entries'.")
    if len(entries) > MAX_EVAL_ENTRIES:
        raise ServiceError(f"Send at most {MAX_EVAL_ENTRIES} suggestions at a time.")
    cache: dict[tuple[str, str], Any] = {}

    def provider_for(e: dict[str, Any]):
        key = (str(e.get("source", "bhavcopy")), str(e.get("symbol", "")).upper())
        if key not in cache:
            cache[key] = get_provider(ctx, key[0], key[1], None, 1.0)[0]
        return cache[key]

    out = evaluate_entries([e for e in entries if isinstance(e, dict)], provider_for, ctx.cm, ctx.mkt)
    return jsonable(out)


def _parse_day(v: Any, what: str) -> Optional[dt.date]:
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ServiceError(f"{what} must be a date like 2026-10-23.")


def sync_bhavcopy(ctx: Context, req: dict[str, Any]) -> dict[str, Any]:
    """Download missing days, one window of MAX_SYNC_DAYS at a time so no request runs for minutes. The caller loops:
    while the reply says more_to_sync, call again with start = next_start.

      history_days   start this many calendar days before `end` (the first sync: 365 gives the year IV rank needs)
      start / end    explicit range; `end` defaults to today
      neither        resume the day after the last stored day (or the last 14 days if the store is empty)

    Days already stored are skipped, so asking for a year you mostly have only fetches the gap."""
    end = _parse_day(req.get("end"), "end") or dt.date.today()
    start = _parse_day(req.get("start"), "start")
    if start is None and req.get("history_days"):
        start = end - dt.timedelta(days=int(_f(req, "history_days", DEFAULT_HISTORY_CALENDAR_DAYS, 30, 800)))
    if start is None:
        last = ctx.store.dates()
        start = (last[-1] + dt.timedelta(days=1)) if last else end - dt.timedelta(days=14)
    if start > end:
        return {"results": [], "counts": {}, "message": "Already up to date.", "more_to_sync": False, "next_start": None, "error": None, **status(ctx)}
    capped = min(end, start + dt.timedelta(days=MAX_SYNC_DAYS - 1))
    results = ctx.store.sync(start, capped)
    ctx._cache.clear()
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    err = next((r.message for r in results if r.status == "error"), None)
    # A window with no files at all cannot be explained by holidays (about 15 weekdays a year). It means the download address
    # is wrong or changed, or NSE does not publish files that far back. Stop instead of repeating the failing request for a year.
    suspect = counts.get("downloaded", 0) == 0 and counts.get("no_file", 0) >= 8
    more = capped < end and not err and not suspect
    return jsonable({"range": [start, capped], "end": end, "counts": counts, "error": err, "more_to_sync": more, "suspect_missing": suspect,
                     "next_start": (capped + dt.timedelta(days=1)) if more else None,
                     "results": [{"date": r.date, "status": r.status, "message": r.message} for r in results if r.status != "cached"],
                     **status(ctx)})


def import_bhavcopy(ctx: Context, name: str, data: bytes) -> dict[str, Any]:
    if len(data) > 200 * 1024 * 1024:
        raise ServiceError("That file is too large (limit 200 MB).")
    r = ctx.store.import_bytes(data)
    ctx._cache.clear()
    if r.status == "error":
        raise ServiceError(f"{name or 'The file'} could not be imported: {r.message}")
    return jsonable({"imported": {"date": r.date, "message": r.message, "name": name}, **status(ctx)})


# ---------------------------------------------------------------- market scan
SCAN_INLINE_INDEX_DAYS = 5        # a few new days are indexed on the fly; more than this is a one-time job with progress


def index_step(ctx: Context, req: dict[str, Any]) -> dict[str, Any]:
    """Build the per-stock summary index for stored days that lack one, a batch at a time. The caller loops until remaining is 0."""
    r = SummaryIndex(ctx.store, ctx.mkt).build(max_days=int(_f(req, "max_days", 40, 5, 80)))
    return {**r, "total": len(ctx.store.dates())}


def _store_asof(ctx: Context, req: dict[str, Any]) -> dt.date:
    dates = ctx.store.dates()
    want = _parse_day(req.get("asof"), "asof")
    if want is None:
        return dates[-1]
    ok = [d for d in dates if d <= want]
    if not ok:
        raise ServiceError(f"No data on or before {want}; the first stored day is {dates[0]}.")
    return ok[-1]


def _scan_payload(ctx: Context, sug, row, alts: list[dict[str, Any]], rank: int, source: str) -> dict[str, Any]:
    d = suggestion_payload(sug, ctx, row.spot if row else (sug.regime.get("spot") or 0.0), source)
    g = sug.regime
    d["scan"] = {"rank": rank, "bias": g.get("trend"), "alternatives": alts,
                 "shortlist_score": round(row.score, 1) if row else None, "shortlist_parts": row.parts if row else None,
                 "buildup": row.buildup if row else None, "pcr": round(row.pcr, 2) if row and row.pcr is not None else None}
    return d


def run_scan(ctx: Context, req: dict[str, Any], bars: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Suggest trades across the whole market. No stock is needed: every F&O stock is shortlisted from the summary index, and the
    shortlist gets the full Greeks and open-interest evaluation."""
    source = _source(req)
    cfg = strategy_config(req)
    top_n = int(_f(req, "top_n", 10, 1, 25))
    if source == "synthetic":
        return _scan_demo(ctx, req, cfg, top_n)
    dates = ctx.store.dates()
    if not dates:
        raise ServiceError("No bhavcopy is stored yet. Use Sync (downloads from NSE) or Import (a file you downloaded in a browser).")
    asof = _store_asof(ctx, req)
    n_days = len([d for d in dates if d <= asof])
    if n_days < MIN_HISTORY_DAYS:
        return jsonable({"mode": "scan", "source": source, "asof": asof, "ideas": [], "data_notes": [],
                         "issue": history_issue(ctx, "the market scan", source, n_days, MIN_HISTORY_DAYS, title="Not enough price history to scan the market", file_hint=False)})
    idx = SummaryIndex(ctx.store, ctx.mkt)
    missing = idx.missing_days()
    if len(missing) > SCAN_INLINE_INDEX_DAYS:
        return jsonable({"mode": "scan", "source": source, "asof": asof, "ideas": [], "data_notes": [],
                         "issue": {"code": "index_needed", "title": "One-time index needed", "missing": len(missing), "total": len(dates), "fix": "index",
                                   "detail": f"The scan looks at every stock at once, so it first builds a compact index of the {len(missing)} stored days that lack one "
                                             f"(about {max(1, len(missing) // 6)} seconds). It is done once; new days are added as they are synced."}})
    if missing:
        idx.build(max_days=SCAN_INLINE_INDEX_DAYS)
    data = idx.load(upto=asof, lookback_days=ScanConfig().lookback)
    res = market_scan(ctx.store, data, asof, cfg, ctx.mkt, ctx.cm, ScanConfig(top_n=top_n))
    notes = ["Technicals in the scan use the closes recorded in the bhavcopy (no intraday high/low), so ATR and swing levels are close-to-close.",
             "Bhavcopy has settlement prices but no bid/ask or implied volatility: IV is solved from the settlement price, and slippage uses an assumed spread. "
             "Contracts that did not trade carry a theoretical price (use the minimum-volume filter)."]
    if n_days < RECOMMENDED_HISTORY_DAYS - 50:
        notes.append(f"Only {n_days} days are stored: IV rank needs about a year to be meaningful.")
    note = ""
    if not res.rows:
        note = "No stock passed the first screen (history, traded options and implied volatility): see what was skipped."
    elif not res.ideas:
        note = f"{res.nothing_passed} shortlisted stocks were evaluated in full and none produced a trade that passed the filters."
    return jsonable({"mode": "scan", "source": source, "asof": asof, "issue": None, "note": note, "data_notes": notes,
                     "universe": res.universe, "eligible": len(res.rows), "shortlisted": len(res.candidates), "nothing_passed": res.nothing_passed,
                     "skipped": res.skipped, "market": res.market, "top_shortlist": [{"symbol": r.symbol, "score": round(r.score, 1), "bias": r.regime.trend} for r in res.rows[:10]],
                     "ideas": [_scan_payload(ctx, s, r, alts, i + 1, source) for i, (s, r, alts) in enumerate(res.ideas)],
                     "config": {"risk_budget": cfg.risk_budget, "min_score": cfg.min_score, "allow_futures": cfg.allow_futures, "min_oi": cfg.min_oi, "min_volume": cfg.min_volume, "top_n": top_n}})


def _scan_demo(ctx: Context, req: dict[str, Any], cfg: StrategyConfig, top_n: int) -> dict[str, Any]:
    """The same screen over the four generated demo stocks, so it can be tried before any bhavcopy is stored. With no date given it
    uses the latest of the last 40 days on which at least one demo stock has a trade, so the screen has something to show (the real
    scan never does this: it always uses the day it is given, or the latest stored day)."""
    asof_req = _parse_day(req.get("asof"), "asof")
    prov: dict[str, Any] = {}
    for sym in DEMO_SYMBOLS:
        p_, b_, _ = get_provider(ctx, "synthetic", sym, None, _f(req, "iv_scale", 1.0, 0.5, 2.0))
        prov[sym] = (p_, b_)
    dates = sorted({b["date"] for _, b in prov.values() for b in b if asof_req is None or b["date"] <= asof_req})
    tried = dates[-40:][::-1] if asof_req is None else dates[-1:]
    ideas, regimes, chosen = [], [], None
    for asof in tried:
        ideas, regimes = [], []
        for sym, (p_, bars) in prov.items():
            r = suggest(sym, bars, p_, asof, cfg, ctx.mkt, ctx.cm, top_n=3)
            if r.regime:
                regimes.append(r.regime)
            if r.suggestions:
                ideas.append((r.suggestions[0], p_.spot(sym, asof) or 0.0, [{"name": s.structure.name, "score": round(s.score, 1)} for s in r.suggestions[1:]]))
        chosen = asof
        if ideas:
            break
    ideas.sort(key=lambda t: -t[0].score)
    ideas = ideas[:top_n]
    mk = {"stocks": len(regimes), "bullish": sum(g.trend == "bullish" for g in regimes), "bearish": sum(g.trend == "bearish" for g in regimes),
          "neutral": sum(g.trend == "neutral" for g in regimes), "median_iv_rank": None, "median_pcr": None}
    out = []
    for i, (s, spot, alts) in enumerate(ideas):
        d = suggestion_payload(s, ctx, spot, "synthetic")
        d["scan"] = {"rank": i + 1, "bias": s.regime.get("trend"), "alternatives": alts, "shortlist_score": None, "shortlist_parts": None, "buildup": None, "pcr": None}
        out.append(d)
    notes = ["Synthetic data: option prices are Black-Scholes values and the four stocks are generated. This shows how the scan screen behaves; "
             "it says nothing about the real market, and it has no open-interest data."]
    if asof_req is None and chosen and ideas and chosen != dates[-1]:
        notes.append(f"Demo day: {chosen}, the most recent of the last 40 days on which a demo stock had a trade that passed the filters.")
    return jsonable({"mode": "scan", "source": "synthetic", "asof": chosen, "issue": None,
                     "note": "" if out else "None of the four demo stocks has a trade that passes the filters on this day.", "data_notes": notes,
                     "universe": len(DEMO_SYMBOLS), "eligible": len(regimes), "shortlisted": len(regimes), "nothing_passed": len(regimes) - len(ideas), "skipped": {},
                     "market": mk, "top_shortlist": [], "ideas": out, "config": {"risk_budget": cfg.risk_budget, "min_score": cfg.min_score, "top_n": top_n}})
