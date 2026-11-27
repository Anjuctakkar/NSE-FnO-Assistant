"""
Hybrid market data: bhavcopy end-of-day history as the BASE, ICICI Breeze as the LIVE overlay. One pipeline serves the single-stock
view and the whole-market scan, so both see the same current picture.

    Base (EOD, stored)    the universe, daily price history, one year of daily ATM-IV history (so IV rank is a real one-year rank),
                          futures/open-interest build-up and the put/call tilt from the last bhavcopy.
    Overlay (live)        the current price becomes today's daily candle (its high and low come from the live quote), daily candles
                          for ATR and swing levels, and the live option chain with bid/ask for pricing. The ATM IV that is ranked
                          against the stored history is the LIVE one.

Whole-market scan, in two live stages, because one full live evaluation costs about 4 Breeze calls and the universe is ~200 stocks:

    Stage A  every eligible stock, 1 call each: the live quote. Every stock is re-scored on current prices (scanner.rerank_with_live),
             so a stock that gapped or broke out today is ranked on today's price instead of last night's close.
    Stage B  the best `shortlist` stocks of that LIVE ranking, about 3 more calls each: daily candles and the option chain. The same
             selector as everywhere else (Greeks, costs, skew-aware win probability, managed-trade simulation) runs on live bid/ask.

A refresh re-runs stage B only for the stocks you are looking at (about 4 calls each), by hand or automatically in market hours.

Why not run the old single-stock live mode for every stock: that mode computed trend from 5-minute candles (an SMA50 on 5-minute candles
is about four hours of price) and IV rank from about a week of option candles, for trades held for weeks.

Limits you should know about:
  * Stage A needs about one call per eligible stock (about 2.5 minutes at 75 calls a minute). It is skipped, and the result says so,
    when the day's allowance or this scan's budget cannot cover it, or when the stored bhavcopy already is today's.
  * Without stored bhavcopy there is no universe and no IV history: name the stocks (`symbols`); IV rank is then unknown (neutral).
  * Open-interest build-up is from the last stored bhavcopy (the previous session); the live chain adds the OI snapshot only.
  * Futures are not offered (no live futures quote is fetched).
  * A job stops early, and says so, when its call budget or the day's remaining allowance runs out. Stocks not reached are listed.
  * It does not know NSE holidays: outside market hours Breeze returns the last close and the result is only as current as that.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from .breeze_market import GLOBAL_LIMITER, BreezeMarketClient, MarketDataError
from .config import StrategyConfig
from .data import StaticProvider, chain_from_records
from .expiry import default_lot_size, monthly_expiries
from .scanner import Row, ScanConfig, market_context, pass_one, rerank_with_live
from .selector import suggest
from .summary import SummaryIndex

FATAL_HINTS = ("checksum", "session", "authentication", "token", "api key", "daily limit")
STALE_DAYS = 5


@dataclass(frozen=True)
class LiveScanConfig:
    shortlist: int = 30            # stocks evaluated in full on live data (about 3 calls each after the stage-A quote)
    quote_all: bool = True         # stage A: refresh every eligible stock's price first (1 call each) and re-rank
    top_n: int = 10                # ideas returned
    max_calls: int = 600           # this job never spends more than this many Breeze calls (stage A ~200 + stage B ~3 per stock)
    reserve_calls: int = 300       # ... nor dips into the last N of the day's allowance, kept for single-stock use
    history_days: int = 420        # calendar days of daily candles requested
    min_bars: int = 60


@dataclass
class LiveScanJob:
    id: str
    state: str = "queued"          # queued | running | done | cancelled | error
    stage: str = "queued"
    done: int = 0
    total: int = 0
    current: str = ""
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    calls: int = 0
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self, with_result: bool = True) -> dict[str, Any]:
        d = {"id": self.id, "state": self.state, "stage": self.stage, "done": self.done, "total": self.total, "current": self.current,
             "elapsed_s": round((self.finished or time.time()) - self.started, 1), "api_calls": self.calls, "error": self.error}
        if with_result and self.result is not None:
            d["result"] = self.result
        return d


class LiveScanManager:
    """Runs one scan at a time in a background thread (a scan takes minutes, longer than an HTTP request may live)."""

    MAX_KEPT = 5

    def __init__(self) -> None:
        self._jobs: dict[str, LiveScanJob] = {}
        self._lock = threading.Lock()

    def running(self) -> Optional[LiveScanJob]:
        with self._lock:
            return next((j for j in self._jobs.values() if j.state in ("queued", "running")), None)

    def start(self, work: Callable[[LiveScanJob], dict[str, Any]]) -> tuple[LiveScanJob, bool]:
        """(job, started). When a scan is already running that job is returned and started is False."""
        with self._lock:
            live = next((j for j in self._jobs.values() if j.state in ("queued", "running")), None)
            if live:
                return live, False
            job = LiveScanJob(uuid.uuid4().hex[:12])
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.started)[:-self.MAX_KEPT]:
                self._jobs.pop(old.id, None)

        def run() -> None:
            job.state = "running"
            try:
                job.result = work(job)
                job.state = "cancelled" if job.cancel.is_set() else "done"
            except Exception as exc:                                   # noqa: BLE001 - reported to the page, never raised into a thread
                job.error = str(exc) if isinstance(exc, (MarketDataError, ValueError)) or exc.__class__.__name__.endswith("ServiceError") \
                    else "The scan failed unexpectedly. The details are in the server log."
                if job.error.startswith("The scan failed"):
                    import logging
                    logging.getLogger("fno.live_scan").exception("live scan failed")
                job.state = "error"
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"live-scan-{job.id}", daemon=True).start()
        return job, True

    def get(self, job_id: str) -> Optional[LiveScanJob]:
        with self._lock:
            return self._jobs.get(job_id)


def _fatal(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(h in text for h in FATAL_HINTS)


def _rate_limited(exc: Exception) -> bool:
    text = str(exc).casefold()
    return "429" in text or "too many" in text or "rate limit" in text


def _merge_today(bars: list[dict[str, Any]], quote: dict[str, Any], today: dt.date) -> list[dict[str, Any]]:
    """The daily candles end at the last COMPLETED session (or include a partial one, depending on the hour). Make the last bar
    today's, with the live price as its close, so trend, ATR and the option chain all describe the same moment."""
    out = [b for b in bars if b["date"] < today]
    ltp = quote["ltp"]
    o = quote.get("open") or ltp
    hi = max(x for x in (quote.get("high"), o, ltp) if x)
    lo = min(x for x in (quote.get("low"), o, ltp) if x)
    out.append({"date": today, "timestamp": dt.datetime.combine(today, dt.time(15, 30)), "open": o, "high": hi, "low": lo, "close": ltp})
    return out


def pick_expiry(asof: dt.date, cfg: StrategyConfig, mkt, requested: Optional[dt.date]) -> dt.date:
    if requested is not None:
        if requested <= asof:
            raise ValueError("Select an upcoming expiry.")
        return requested
    e = next((x for x in monthly_expiries(asof, 12, mkt) if cfg.min_dte <= (x - asof).days <= cfg.max_dte), None)
    if e is None:
        raise ValueError(f"No monthly expiry is between {cfg.min_dte} and {cfg.max_dte} days away.")
    return e


@dataclass
class HistoryBase:
    """The stored (EOD) side of a stock: what the live overlay is added to."""
    iv_history: list[float] = field(default_factory=list)       # daily ATM IV, oldest first (up to a year)
    bars: list[dict[str, Any]] = field(default_factory=list)    # daily closes from bhavcopy (close-only bars)
    lot: Optional[int] = None


def shortlist_rows(ctx, asof: dt.date, cfg: StrategyConfig, lcfg: LiveScanConfig, symbols: Optional[list[str]],
                   notes: list[str]) -> tuple[list[Row], dict[str, Any], dict[str, list[dict[str, Any]]], Optional[dt.date], int]:
    """Pass 1 from stored bhavcopy: (rows, market context, summary data by symbol, data day, universe size). Without `symbols` the rows
    are EVERY eligible stock, best first (stage A re-ranks them on live prices before the cut); with `symbols` only the named
    stocks, in the order given, whatever their rank (names the stored ranking cannot use are reported in `notes`)."""
    days = [d for d in ctx.store.dates() if d <= asof]
    if not days:
        if not symbols:
            raise ValueError("The whole-market scan needs a stock universe. Sync NSE bhavcopy once (Settings, free) so the scan can rank every F&O "
                             "stock before spending Breeze calls, or name the stocks to scan.")
        return [], {}, {}, None, len(symbols)
    last = days[-1]
    idx = SummaryIndex(ctx.store, ctx.mkt)
    if idx.missing_days():
        idx.build(max_days=120)
    data = idx.load(upto=last, lookback_days=ScanConfig().lookback)
    res = pass_one(data, last, cfg, ctx.mkt, ScanConfig())
    age = (asof - last).days
    if age > STALE_DAYS:
        notes.append(f"The stored bhavcopy ends {last} ({age} days ago): the EOD base (history, IV history, open-interest build-up) is that old. "
                     "Sync bhavcopy for a current base.")
    by_sym = {r.symbol: r for r in res.rows}
    if symbols:
        for s in symbols:
            if s not in by_sym:
                notes.append(f"{s}: not in the stored ranking (no traded options or too little history on {last}); it is skipped.")
        rows = [by_sym[s] for s in symbols if s in by_sym]
    else:
        rows = list(res.rows)
    return rows, res.market, data, last, res.universe


def hybrid_inputs(ctx, client, symbol: str, asof: dt.date, expiry: dt.date, base: HistoryBase, lcfg: LiveScanConfig, service_mod,
                  quote: Optional[dict[str, Any]] = None) -> tuple[StaticProvider, list[dict[str, Any]], dict[str, Any], Any, list[str]]:
    """The one hybrid pipeline. Returns (provider, daily bars, live quote, live chain, notes). Raises MarketDataError when the stock
    cannot be priced. Daily candles come from Breeze (real high/low); if they are unavailable the stored closes are used instead."""
    notes: list[str] = []
    quote = quote or client.get_stock_quote(symbol)
    bars: Optional[list[dict[str, Any]]] = None
    if hasattr(client, "get_daily_bars"):
        now = service_mod._india_now().replace(tzinfo=None)
        start = (now - dt.timedelta(days=lcfg.history_days)).replace(hour=9, minute=15, second=0, microsecond=0)
        try:
            got = service_mod._intraday_bars(client.get_daily_bars(symbol, service_mod._breeze_date_time(start), service_mod._breeze_date_time(now)))
        except MarketDataError as exc:
            if _fatal(exc):
                raise
            got = []
            notes.append(f"Breeze daily candles were unavailable ({str(exc)[:80]}): stored bhavcopy closes were used for the trend.")
        if len(got) >= lcfg.min_bars:
            bars = got
    if bars is None:
        bars = [b for b in base.bars]
        if not any("closes were used" in n for n in notes):
            notes.append("Trend, ATR and swing levels use stored bhavcopy closes (no intraday range).")
    if len(bars) < lcfg.min_bars:
        raise MarketDataError(f"only {len(bars)} daily candles for {symbol} (need {lcfg.min_bars})")
    bars = _merge_today(bars, quote, asof)
    records = client.get_option_chain_records(symbol, service_mod._breeze_expiry(expiry))
    lot, estimated = (base.lot, False) if base.lot else service_mod._live_lot_size(ctx, symbol, quote["ltp"], records)
    if estimated:
        notes.append(f"No lot size was available for {symbol}: sizing uses an estimate of {lot} units. Verify the current NSE lot size.")
    chain = chain_from_records(symbol, asof, quote["ltp"], expiry, records, int(lot), ctx.mkt)
    if not chain.quotes:
        raise MarketDataError(f"no usable option quotes for {symbol} expiry {expiry}")
    prov = StaticProvider(symbol, bars, {expiry: chain}, list(base.iv_history), periods_per_year=252, snapshot_asof=asof)
    prov.name = "breeze-live"
    return prov, bars, quote, chain, notes


def hybrid_notes(base: HistoryBase, quote: dict[str, Any]) -> list[str]:
    out = ["Hybrid data. Live (Breeze, now): the price (today's candle), the option chain with bid/ask, and the ATM IV that is ranked. "
           "End-of-day (stored bhavcopy): the daily history, the one-year IV history behind IV rank, and futures/open-interest build-up."]
    if not base.iv_history:
        out.append("No stored IV history for this stock: IV rank is unknown and treated as neutral.")
    if quote.get("timestamp"):
        out.append(f"Breeze cash-quote timestamp: {quote['timestamp']}.")
    return out


def _evaluate_stock(ctx, client: BreezeMarketClient, row: Row, hist: list[dict[str, Any]], asof: dt.date, expiry: dt.date,
                    cfg: StrategyConfig, lcfg: LiveScanConfig, service_mod, quote: Optional[dict[str, Any]] = None) -> tuple[Any, float, Optional[str]]:
    """Evaluate one stock on live data. Returns (SuggestResult or None, live spot, problem)."""
    base = HistoryBase(list(row.iv_history), [dict(b) for b in row.bars if b["date"] < asof], int(hist[-1]["lot"]) if hist and hist[-1].get("lot") else None)
    try:
        prov, bars, quote, chain, _ = hybrid_inputs(ctx, client, row.symbol, asof, expiry, base, lcfg, service_mod, quote)
    except MarketDataError as exc:
        if _fatal(exc) or _rate_limited(exc):
            raise
        return None, (quote or {}).get("ltp", 0.0), str(exc)[:160]
    res = suggest(row.symbol, bars, prov, asof, replace(cfg, allow_futures=False), ctx.mkt, ctx.cm, top_n=3, selected_expiry=expiry,
                  calibration=ctx.calibration("breeze-live"))
    return res, quote["ltp"], None


def run_live_scan(ctx, req: dict[str, Any], client: BreezeMarketClient, job: LiveScanJob, service_mod,
                  lcfg: Optional[LiveScanConfig] = None) -> dict[str, Any]:
    """The whole scan. `service_mod` is fno.service (passed in to avoid an import cycle). `req` options: shortlist, top_n, max_calls,
    expiry, symbols, quote_all (default true), refresh (true = evaluate only the named symbols, skip stage A)."""
    lcfg = lcfg or LiveScanConfig()
    t0 = time.time()
    limiter = client.limiter or GLOBAL_LIMITER
    calls0 = limiter.used_today
    asof = service_mod._india_now().date()
    cfg = replace(service_mod.strategy_config(req), allow_futures=False)
    expiry = pick_expiry(asof, cfg, ctx.mkt, service_mod._parse_day(req.get("expiry"), "expiry"))
    symbols = [service_mod._sym({"symbol": s}) for s in req.get("symbols")] if req.get("symbols") else None
    refresh = bool(req.get("refresh")) and bool(symbols)
    notes: list[str] = []
    job.stage = "ranking stocks from stored bhavcopy"
    rows, market, data, data_day, universe = shortlist_rows(ctx, asof, cfg, lcfg, symbols, notes)
    if not rows and symbols:                                       # no stored data at all: fetch the named stocks, IV rank unknown
        rows = [Row(s, None, 0.0, None, None, None, None, None, 0, 0) for s in symbols]       # type: ignore[arg-type]
        notes.append("No stored bhavcopy: there is no IV history, so IV rank is unknown (treated as neutral) for every stock.")
    budget = int(req.get("max_calls") or lcfg.max_calls)
    n_full = max(1, min(int(req.get("shortlist") or lcfg.shortlist), 120))
    want_quotes = bool(req.get("quote_all", lcfg.quote_all)) and not symbols and bool(data_day)
    quotes: dict[str, dict[str, Any]] = {}
    quote_failed: dict[str, str] = {}
    stopped = ""
    unreached: list[str] = []

    def spent() -> int:
        return limiter.used_today - calls0

    def room(need: int) -> Optional[str]:
        if spent() + need > budget:
            return "this scan's call budget was reached"
        if limiter.remaining_today <= lcfg.reserve_calls + need:
            return "the day's Breeze allowance was reached (a reserve is kept for single-stock use)"
        return None

    # ---- stage A: live prices for every eligible stock, then re-rank
    stage_a = "not run"
    if want_quotes:
        if data_day >= asof:
            stage_a = "skipped: the stored bhavcopy already is today's"
        elif len(rows) + n_full * 3 + 10 > budget - spent() or limiter.remaining_today < len(rows) + n_full * 3 + lcfg.reserve_calls + 10:
            stage_a = "skipped: the call budget or the day's allowance cannot cover a price for every stock plus the full evaluations"
            notes.append(f"Live prices were not fetched for every stock ({stage_a.split(': ', 1)[1]}); the ranking is the stored end-of-day one, "
                         f"and only the shortlisted stocks are live. Raise the call budget or lower the number of stocks to evaluate.")
        else:
            job.stage, job.total, job.done = "refreshing live prices for every stock", len(rows), 0
            for i, row in enumerate(rows):
                job.current, job.calls = row.symbol, spent()
                if job.cancel.is_set():
                    stopped = "cancelled"
                    break
                try:
                    q = client.get_stock_quote(row.symbol)
                    if q and q.get("ltp"):
                        quotes[row.symbol] = q
                except MarketDataError as exc:
                    if _fatal(exc):
                        raise
                    if _rate_limited(exc):
                        time.sleep(20)
                    quote_failed[row.symbol] = str(exc)[:100]
                job.done = i + 1
            stage_a = f"{len(quotes)} of {len(rows)} stocks priced live"
            if quotes and not stopped:
                rows = rerank_with_live(rows, quotes, asof, cfg)
                moved = sum(1 for r in rows[:n_full] if r.eod_rank and r.eod_rank > n_full)
                if moved:
                    notes.append(f"Live prices moved {moved} stock(s) into the evaluated {n_full} that the end-of-day ranking would have missed.")
    elif refresh:
        stage_a = "skipped: refreshing named stocks"
    rows_all = rows
    rows = rows[:n_full] if not symbols else rows[:max(1, min(len(rows), n_full))]

    # ---- stage B: full live evaluation of the top of the live ranking
    job.total, job.done, job.stage = len(rows), 0, "evaluating the best stocks on live option chains"
    found: list[tuple[Any, Row, float, list[dict[str, Any]]]] = []
    failed: dict[str, str] = {}
    nothing = 0
    for i, row in enumerate(rows):
        if stopped:
            unreached = [r.symbol for r in rows[i:]]
            break
        job.current, job.calls = row.symbol, spent()
        if job.cancel.is_set():
            stopped, unreached = "cancelled", [r.symbol for r in rows[i:]]
            break
        cached = quotes.get(row.symbol)
        why = room(3 if cached else 4)
        if why:
            stopped, unreached = why, [r.symbol for r in rows[i:]]
            break
        hist = data.get(row.symbol, [])
        try:
            try:
                res, spot, problem = _evaluate_stock(ctx, client, row, hist, asof, expiry, cfg, lcfg, service_mod, cached)
            except MarketDataError as exc:
                if _fatal(exc):
                    raise
                if _rate_limited(exc):
                    time.sleep(20)
                    res, spot, problem = _evaluate_stock(ctx, client, row, hist, asof, expiry, cfg, lcfg, service_mod, cached)
                else:
                    raise
        except MarketDataError as exc:
            if _fatal(exc):
                raise                                              # every later call would fail the same way: stop and say why
            failed[row.symbol] = str(exc)[:160]
            job.done = i + 1
            continue
        job.done = i + 1
        if problem:
            failed[row.symbol] = problem
        elif res is None or not res.suggestions:
            nothing += 1
        else:
            best, rest = res.suggestions[0], res.suggestions[1:]
            found.append((best, row, spot, [{"name": s.structure.name, "score": round(s.score, 1)} for s in rest]))
    job.calls = spent()
    found.sort(key=lambda t: (-t[0].score, t[0].symbol))
    ideas = []
    for rank, (sug, row, spot, alts) in enumerate(found[:int(req.get("top_n") or lcfg.top_n)], 1):
        d = service_mod.suggestion_payload(sug, ctx, spot, "breeze-live")
        g = sug.regime
        d["scan"] = {"rank": rank, "bias": g.get("trend"), "alternatives": alts,
                     "shortlist_score": round(row.score, 1) if getattr(row, "regime", None) else None,
                     "shortlist_parts": row.parts or None, "buildup": row.buildup,
                     "pcr": round(row.pcr, 2) if row.pcr is not None else None,
                     "live_chg_pct": round(100 * row.live_chg, 2) if row.live_chg is not None else None,
                     "eod_rank": row.eod_rank, "live_rank": (rows_all.index(row) + 1) if row in rows_all else None}
        ideas.append(d)
    notes += ["Hybrid scan. Live from Breeze: prices" + (" (every stock)" if quotes else " (evaluated stocks)") + ", daily candles, option chains with bid/ask, "
              "and the ATM IV that is ranked. End-of-day from stored bhavcopy: the universe, daily history, the one-year IV history behind IV rank, "
              "and futures/open-interest build-up (last session).",
              "Futures are not offered here and open interest is the chain snapshot plus last session's build-up."]
    if stopped:
        notes.append(f"The scan stopped early: {stopped}. {len(unreached)} stock(s) were not evaluated: {', '.join(unreached[:12])}"
                     + ("…" if len(unreached) > 12 else "") + ".")
    note = "" if ideas else (f"{nothing} stock(s) were evaluated live and none produced a trade that passed the filters." if nothing else
                             "No stock produced a trade. See the stocks that failed.")
    return {"mode": "scan", "source": "breeze-live", "asof": asof.isoformat(), "expiry": expiry.isoformat(), "issue": None, "note": note,
            "data_notes": notes, "universe": universe, "eligible": len(rows_all), "shortlisted": len(rows), "fetched": len(rows) - len(failed) - len(unreached),
            "nothing_passed": nothing, "failed": failed, "not_reached": unreached, "stopped": stopped or None, "refresh": refresh,
            "stage_a": stage_a, "priced_live": len(quotes), "quote_failed": quote_failed,
            "ranking_day": data_day.isoformat() if data_day else None, "skipped": {}, "market": market,
            "top_shortlist": [{"symbol": r.symbol, "score": round(r.score, 1), "bias": r.regime.trend} for r in rows[:10] if getattr(r, "regime", None)],
            "ideas": ideas, "api_calls": job.calls, "calls_remaining_today": limiter.remaining_today, "elapsed_s": round(time.time() - t0, 1),
            "config": {"risk_budget": cfg.risk_budget, "min_score": cfg.min_score, "shortlist": len(rows), "max_calls": budget}}
