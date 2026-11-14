"""
Forward evaluation: did the suggestions the framework actually made work out?

A backtest asks "would these rules have worked?". Evaluation asks "did the trades we really suggested work?",
which is the only out-of-sample test there is. The flow:

    1. log_suggestions()   write each suggestion (legs, fill prices, exit plan, model probability) to a JSONL file.
    2. later, evaluate_log() replays every logged suggestion forward through the same position simulator the
       backtest uses, on data that arrived after the suggestion: target / stop / invalidation / time / expiry, or
       still open. Results are split into all suggestions and the ones marked as taken.
    3. The summary includes the same metrics as a backtest, including Greek attribution and probability
       calibration (predicted chance of profit against what happened).

Assumption: a logged suggestion is assumed filled at its suggested prices on its date. Real fills differ; mark
suggestions you actually took with set_taken() and compare the two groups.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Optional

from .backtest import simulate_position, summarize, trade_record
from .config import CostModel, MarketConfig
from .models import Suggestion


def load_log(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write(path: str | Path, entries: list[dict[str, Any]]) -> None:
    Path(path).write_text("\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""), encoding="utf-8")


def log_suggestions(path: str | Path, suggestions: list[Suggestion]) -> dict[str, int]:
    """Append suggestions to the log. The same symbol, date and structure is only logged once."""
    entries = load_log(path)
    seen = {(e["symbol"], e["asof"], e["structure"]["name"]) for e in entries}
    added = 0
    for s in suggestions:
        key = (s.symbol, s.asof.isoformat(), s.structure.name)
        if key in seen:
            continue
        e = s.to_dict()
        e.update(logged_at=dt.datetime.now().isoformat(timespec="seconds"), taken=False)
        entries.append(e)
        seen.add(key)
        added += 1
    _write(path, entries)
    return {"added": added, "skipped": len(suggestions) - added, "total": len(entries)}


def set_taken(path: str | Path, suggestion_id: str, taken: bool = True) -> bool:
    entries = load_log(path)
    hit = False
    for e in entries:
        if e["id"] == suggestion_id:
            e["taken"], hit = bool(taken), True
    if hit:
        _write(path, entries)
    return hit


def evaluate_entries(entries: list[dict[str, Any]], provider_for, cm: CostModel = CostModel(), mkt: MarketConfig = MarketConfig(),
                     upto: Optional[dt.date] = None) -> dict[str, Any]:
    """Replay logged suggestion dicts forward. provider_for(entry) returns the data provider for that entry (it may
    differ by symbol or source). An entry whose data cannot be loaded is returned with status "no_data", not dropped."""
    rows: list[dict[str, Any]] = []
    for e in entries:
        sug = Suggestion.from_dict(e)
        try:
            prov = provider_for(e)
            dates = prov.trading_dates(sug.symbol, sug.asof, upto)
            sim = simulate_position(sug, prov, dates, cm, mkt)
        except Exception as exc:                                 # noqa: BLE001 - one bad entry must not hide the rest
            rows.append({"id": e.get("id"), "symbol": sug.symbol, "strategy": sug.structure.name, "entry_date": sug.asof.isoformat(),
                         "exit_reason": "no_data", "status": "no_data", "error": f"{type(exc).__name__}: {exc}",
                         "taken": bool(e.get("taken")), "net_pnl": 0.0})
            continue
        row = trade_record(sug, sim)
        row.update(id=e.get("id"), taken=bool(e.get("taken")), logged_at=e.get("logged_at"),
                   status=sim["status"], open_gross_pnl=sim["gross_pnl"] if sim["status"] == "open" else None)
        rows.append(row)
    scored = [r for r in rows if r["exit_reason"] != "no_data"]
    open_rows = [r for r in scored if r["exit_reason"] == "open"]
    return {"rows": rows, "all": summarize(scored), "taken": summarize([r for r in scored if r["taken"]]),
            "open_positions": len(open_rows), "open_net_pnl": round(sum(r["net_pnl"] for r in open_rows), 2),
            "no_data": len(rows) - len(scored)}


def evaluate_log(path: str | Path, provider, cm: CostModel = CostModel(), mkt: MarketConfig = MarketConfig(),
                 upto: Optional[dt.date] = None) -> dict[str, Any]:
    """Replay every suggestion in a JSONL log over the data now available (up to `upto`, default: all of it)."""
    return evaluate_entries(load_log(path), lambda e: provider, cm, mkt, upto)
