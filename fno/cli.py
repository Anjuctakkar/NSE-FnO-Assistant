"""
Command line:

    python -m fno demo                                   # end to end on synthetic data (no files needed)
    python -m fno suggest  --symbol TCS --bars tcs.csv [--asof 2026-10-23] [--log fno_log.jsonl]
    python -m fno backtest --symbol TCS --bars tcs.csv [--random] [--iv-scale 1.0]
    python -m fno evaluate --log fno_log.jsonl --symbol TCS --bars tcs.csv

    python -m fno bhav sync   [--from 2026-09-01] [--to 2026-10-23] --data-dir data     # download NSE F&O bhavcopy
    python -m fno bhav import FILE [FILE ...]  --data-dir data                           # files saved from a browser
    python -m fno bhav status | schema FILE    --data-dir data
    python -m fno suggest --source bhavcopy --symbol TCS --data-dir data [--bars tcs.csv]
    python -m fno calibrate --source bhavcopy --data-dir data --symbols TCS,INFY,RELIANCE [--reset]  # fit the win-% calibration
    python -m fno serve --data-dir data [--port 8100]                                    # the web UI
    python -m fno probe https://host/path/mcp                                            # what tools does an MCP server offer?

--bars is a CSV of daily bars with the header date,open,high,low,close. With --source synthetic (the default) options are
priced with the Black-Scholes provider (see data.py for what that does and does not mean).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from typing import Any

from .backtest import edge_vs_random, run_backtest
from .config import CostModel, MarketConfig, StrategyConfig
from .data import SyntheticProvider
from .demo_data import bars_from_csv, make_synthetic_bars
from .evaluate import evaluate_log, log_suggestions
from . import service
from .bhavcopy import describe_file
from .mcp_probe import probe
from .models import Suggestion
from .selector import SuggestResult, suggest


def _inr(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "unbounded"
    return f"{'-' if x < 0 else ''}Rs {abs(x):,.0f}"


def fmt_suggestion(s: Suggestion) -> str:
    st, r = s.structure, s.risk
    lines = [f"[{s.score:.0f}/100] {st.name} on {s.symbol}  (expiry {st.expiry}, {s.dte} days, {st.legs[0].lots} lot x {st.lot_size})"]
    for l in st.legs:
        what = "FUT" if l.kind == "FUT" else f"{l.strike:g} {l.kind}"
        lines.append(f"    {'BUY ' if l.side > 0 else 'SELL'} {what:>10} @ {l.entry_price:,.2f}" + (f"   IV {l.iv:.0%}" if l.iv else ""))
    pop = f"{s.pop:.0%}" if s.pop is not None else "n/a"
    if st.has_future:
        lines.append(f"    no premium | profit and loss are set by the stop and target below (the futures payoff is not capped or floored) | breakeven {r['breakevens']}")
    else:
        single = st.name in ("Long call", "Long put")
        mp = "open-ended" if single else _inr(r["max_profit"])
        lines.append(f"    net {'credit' if r['net_premium'] > 0 else 'debit'} {_inr(abs(r['net_premium']))} | max profit {mp} | "
                     f"max loss {_inr(r['max_loss'])} | breakevens {r['breakevens']} | model POP {pop}")
    g = s.greeks
    lines.append(f"    delta {g['delta']:+.0f} | gamma {g['gamma']:+.3f} | theta {_inr(g['theta'])}/day | vega {_inr(g['vega'])}/IV pt")
    p = s.exit_plan
    inv = f" | invalidated if close {p.invalidation[0]} {p.invalidation[1]:g}" if p.invalidation else ""
    lines.append(f"    exit: profit at {_inr(p.profit_target)} | stop at {_inr(p.stop_loss)} | close with {p.exit_dte} days left{inv}")
    lines += [f"    - {x}" for x in s.reasons]
    lines += [f"    ! {x}" for x in s.warnings]
    return "\n".join(lines)


def print_result(res: SuggestResult, why: bool = False) -> None:
    print(f"\n{res.symbol} as of {res.asof}" + (f", expiry {res.expiry}" if res.expiry else ""))
    if res.regime:
        g = res.regime
        print(f"  trend {g.trend} ({g.trend_score:+d}/4) | RSI {g.rsi:.0f} | ATR {g.atr_pct:.1f}% | realised vol {g.rv20 or 0:.0%} | "
              f"IV rank {'n/a' if g.iv_rank is None else f'{g.iv_rank:.0f}'}")
    if not res.suggestions and res.code in ("insufficient_history", "no_chain", "no_expiry"):
        print(f"  DATA PROBLEM (not a trading decision): {res.note}.")
        if res.code == "insufficient_history":
            print("  Run:  python -m fno bhav sync --data-dir data      (fetches a year), or supply a daily-price CSV with --bars")
    elif not res.suggestions:
        print(f"  NO TRADE: {res.note}")
    for s in res.suggestions:
        print("\n" + fmt_suggestion(s))
    if why and res.rejected:
        print("\n  considered and rejected:")
        for n, w in res.rejected:
            print(f"    - {n}: {w}")


def print_metrics(m: dict[str, Any], title: str) -> None:
    print(f"\n{title}")
    if not m.get("trades"):
        print("  no closed trades")
        return
    print(f"  trades {m['trades']} | win rate {m['win_rate']}% | expectancy {m['expectancy_r']:+.2f} R | net {_inr(m['total_net_pnl'])} | "
          f"profit factor {m['profit_factor']} | max drawdown {_inr(m['max_drawdown'])} | costs {_inr(m['total_costs'])}")
    a = m["attribution"]
    print(f"  P&L from: delta {_inr(a['delta'])}, gamma {_inr(a['gamma'])}, theta {_inr(a['theta'])}, vega {_inr(a['vega'])}, "
          f"entry slippage {_inr(a['entry_slippage'])}, exit slippage {_inr(a['exit_slippage'])}, unexplained {_inr(a['unexplained'])}")
    for k, v in m["by_strategy"].items():
        print(f"    {k:<18} {v['trades']:>3} trades, win {v['win_rate']:>5}%, net {_inr(v['net_pnl']):>12}, avg {v['avg_r']:+.2f} R")
    if m.get("calibration"):
        c = m["calibration"]
        print(f"  probability calibration (Brier {c['brier']}): " + "; ".join(
            f"{b}: predicted {v['predicted']}% vs realised {v['realised']}% (n={v['trades']})" for b, v in c["buckets"].items()))
    if m["small_sample"]:
        print(f"  CAUTION: only {m['trades']} closed trades. Fewer than 30 prove little.")


def _setup(a: argparse.Namespace):
    mkt = MarketConfig()
    cfg = StrategyConfig(risk_budget=a.risk_budget, min_score=a.min_score, min_volume=a.min_volume, min_oi=a.min_oi)
    if a.source == "bhavcopy":
        ctx = service.Context(a.data_dir)
        prov, bars, notes = service.get_provider(ctx, "bhavcopy", a.symbol, bars_from_csv(a.bars) if a.bars else None)
        for n in notes:
            print(f"note: {n}", file=sys.stderr)
        return bars, prov, mkt, CostModel(), cfg
    bars = bars_from_csv(a.bars) if a.bars else make_synthetic_bars(700, seed=a.seed)
    return bars, SyntheticProvider(a.symbol, bars, mkt, iv_scale=a.iv_scale), mkt, CostModel(), cfg


def _date(s: str | None, default: dt.date) -> dt.date:
    return dt.date.fromisoformat(s) if s else default


def _bhav(a: argparse.Namespace) -> int:
    ctx = service.Context(a.data_dir)
    action, files = (a.rest + [""])[0], a.rest[1:]
    if action == "sync":
        have = ctx.store.dates()
        end = _date(a.to, dt.date.today())
        start = _date(a.start, (have[-1] + dt.timedelta(days=1)) if have else end - dt.timedelta(days=service.DEFAULT_HISTORY_CALENDAR_DAYS))
        if not have and not a.start:
            print(f"The store is empty: fetching a year of history (the trend rules need at least {service.MIN_HISTORY_DAYS} trading days, IV rank about a year).")
        print(f"syncing {start} to {end} into {ctx.store.root}")
        res = ctx.store.sync(start, end, progress=lambda r: print(f"  {r.date}  {r.status:<10} {r.message}"))
        bad = [r for r in res if r.status == "error"]
        got, missing = sum(r.status == "downloaded" for r in res), sum(r.status == "no_file" for r in res)
        if got == 0 and missing >= 8:
            print("NSE returned no file for any of these weekdays. Holidays cannot explain that: the download address may be wrong or changed.\n"
                  "Save a file from nseindia.com in a browser and run: python -m fno bhav import FILE")
            return 1
        print(f"done: {sum(r.status == 'downloaded' for r in res)} downloaded, {sum(r.status == 'no_file' for r in res)} with no file, "
              f"{len(bad)} error(s). Stored: {ctx.store.coverage()}")
        return 1 if bad else 0
    if action == "import":
        if not files:
            print("give one or more files to import")
            return 2
        for f in files:
            r = ctx.store.import_path(f)
            print(f"  {f}: {r.status} {r.date or ''} {r.message}")
        return 0
    if action == "status":
        print(json.dumps(service.status(ctx), indent=1))
        return 0
    if action == "schema" and files:
        print(json.dumps(describe_file(files[0]), indent=1, default=str))
        return 0
    print("usage: python -m fno bhav sync|import|status|schema")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fno", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["demo", "suggest", "backtest", "evaluate", "bhav", "serve", "probe", "calibrate"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--symbol", default="DEMO")
    ap.add_argument("--bars")
    ap.add_argument("--asof")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--source", choices=["synthetic", "bhavcopy"], default="synthetic")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--iv-scale", type=float, default=1.0, help="synthetic IV = realised vol x this (1.0 is neutral)")
    ap.add_argument("--risk-budget", type=float, default=None, help="Rs max loss per trade; sets the number of lots")
    ap.add_argument("--min-score", type=float, default=55.0)
    ap.add_argument("--min-volume", type=int, default=0, help="reject contracts that traded fewer than this many (bhavcopy)")
    ap.add_argument("--min-oi", type=int, default=0)
    ap.add_argument("--random", action="store_true", help="backtest with random trend (baseline)")
    ap.add_argument("--symbols", help="comma-separated symbols (calibrate)")
    ap.add_argument("--reset", action="store_true", help="calibrate: start the record afresh")
    ap.add_argument("--log", default="fno_log.jsonl")
    ap.add_argument("--why", action="store_true", help="also list rejected candidates")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "bhav":
        return _bhav(a)
    if a.cmd == "probe":
        if not a.rest:
            print("usage: python -m fno probe URL")
            return 2
        r = probe(a.rest[0])
        if not r["ok"]:
            print(f"could not read tools from {r['url']}: {r['error']}")
            return 1
        print(f"{r['url']}: {len(r['tools'])} tool(s)")
        for t in r["tools"]:
            flag = "  <- looks derivatives-related" if t["name"] in r["derivatives_like"] else ""
            print(f"  {t['name']}({', '.join(t['properties'])}){flag}\n      {t['description'][:140]}")
        return 0
    if a.cmd == "calibrate":
        ctx = service.Context(a.data_dir)
        out = service.run_calibrate(ctx, {"source": a.source, "symbols": [x for x in (a.symbols or "").split(",") if x.strip()] or None,
                                          "reset": a.reset, "min_score": a.min_score})
        rep = out["report"]
        print(f"calibration ({out['source']}): {out['added']} trades added, {out['total_points']} on record")
        for sym, v in out["symbols"].items():
            print(f"  {sym}: {v['trades']} trades, win rate {v['win_rate']}")
        if rep.get("n"):
            print(f"  base rate {rep['base_rate']}% | Brier {rep['brier']} | Brier skill vs base rate {rep['brier_skill']} | calibration error {rep['ece']}")
            for b in rep["buckets"]:
                print(f"    model said {b['predicted']:>5.1f}%  ->  realised {b['realised']:>5.1f}%   ({b['trades']} trades)")
        wf = out["walk_forward"]
        if wf.get("brier_raw") is not None:
            print(f"  out-of-sample: Brier raw {wf['brier_raw']} vs calibrated {wf['brier_calibrated']} -> calibration {'helps' if wf['calibration_helps'] else 'does NOT help'}")
        for n in out["notes"]:
            print(f"  note: {n}")
        print(f"\n{out['caveat']}")
        return 0
    if a.cmd == "serve":
        from .server import serve
        srv = serve(service.Context(a.data_dir), a.host, a.port)
        print(f"F&O UI on http://{a.host}:{a.port}  (data in {a.data_dir}; Ctrl+C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    bars, prov, mkt, cm, cfg = _setup(a)
    asof = dt.date.fromisoformat(a.asof) if a.asof else bars[-1]["date"]

    if a.cmd == "suggest":
        res = suggest(a.symbol, bars, prov, asof, cfg, mkt, cm)
        if a.json:
            print(json.dumps([s.to_dict() for s in res.suggestions], indent=1))
        else:
            print_result(res, a.why)
        if res.suggestions and a.log:
            print(f"\nlogged: {log_suggestions(a.log, res.suggestions)}")
    elif a.cmd == "backtest":
        r = run_backtest(a.symbol, bars, prov, cfg, mkt, cm, entry_mode="random" if a.random else "signal")
        if a.json:
            print(json.dumps({"metrics": r.metrics, "trades": r.trades}, indent=1, default=str))
        else:
            print_metrics(r.metrics, f"backtest ({r.params['entry_mode']}, {r.params['provider']} prices, {r.params['bars']} bars)")
    elif a.cmd == "evaluate":
        ev = evaluate_log(a.log, prov, cm, mkt)
        print_metrics(ev["all"], "forward evaluation: all logged suggestions")
        print(f"  still open: {ev['open_positions']} (net so far {_inr(ev['open_net_pnl'])})")
        if ev["taken"].get("trades"):
            print_metrics(ev["taken"], "forward evaluation: suggestions marked taken")
    else:
        print("DEMO on synthetic prices. Nothing here predicts a real market. The generator has persistent trend regimes, so a")
        print("trend-following signal is EXPECTED to look good on it: this checks that the machinery works, not that there is an edge.\n")
        cut = len(bars) - 120
        res = None
        for idx in range(cut, cut - 90, -3):                       # latest day in the window with a trade to show
            res = suggest(a.symbol, bars[:idx], prov, bars[idx - 1]["date"], cfg, mkt, cm)
            if res.suggestions:
                cut = idx
                break
        print_result(res, why=True)
        sig = run_backtest(a.symbol, bars, prov, cfg, mkt, cm, entry_mode="signal")
        rnd_runs = [run_backtest(a.symbol, bars, prov, cfg, mkt, cm, entry_mode="random", seed=i + 1) for i in range(4)]
        rnd = rnd_runs[0]
        print_metrics(sig.metrics, "BACKTEST: selector signals")
        print_metrics(rnd.metrics, "BACKTEST: random trend baseline (same structures, exits, costs)")
        print("\nSignal vs random (4 seeds):", json.dumps(edge_vs_random(sig, rnd_runs)))
        if res.suggestions:
            log_suggestions(a.log, res.suggestions)
            ev = evaluate_log(a.log, prov, cm, mkt)
            print_metrics(ev["all"], f"FORWARD EVALUATION of the suggestion above, replayed over the following {len(bars) - cut} bars")
            print(f"  still open: {ev['open_positions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
