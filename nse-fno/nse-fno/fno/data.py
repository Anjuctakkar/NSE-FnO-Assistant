"""
Option-chain data providers. All providers expose the same small interface, so the selector and the backtester
never care where the numbers come from:

    expiries(symbol, asof, n)              -> list[date]
    get_chain(symbol, asof, expiry)        -> Chain | None
    spot(symbol, asof)                     -> float | None   (last close on or before asof)
    lot_size(symbol, asof)                 -> int
    atm_iv_history(symbol, asof, lookback) -> list[float]    (for IV rank; oldest first)

SyntheticProvider  prices options with Black-Scholes from the underlying's own realised volatility. It needs only
                   daily bars, so a backtest is possible without historical option data. READ ITS LIMITS BELOW.
BhavcopyProvider   builds chains from NSE F&O bhavcopy (real settlement prices, open interest, volume); see bhavcopy.py.
chain_from_records adapts live option-chain output (for example from an MCP server's get_option_chain tool).

SyntheticProvider limits, which any result built on it inherits:
  * IV is a function of trailing realised volatility (times iv_scale), not a market price. "IV rank" is therefore
    a realised-volatility rank. Real IV has a variance risk premium, event premia and a real skew that this does not.
  * iv_scale directly drives short-premium results: iv_scale > 1 hands option sellers a built-in edge. Keep it at
    1.0 unless you are deliberately stress-testing, and run a sweep.
  * No real bid/ask, liquidity or open interest: spreads are an assumption.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from . import greeks as bs
from .bhavcopy import BhavcopyStore, SchemaError, num as _num, read_raw
from .config import MarketConfig
from .expiry import default_lot_size, monthly_expiries, nice_strike_step
from .models import Chain, Quote
from .oi import OiContext


class ChainProvider(Protocol):
    def expiries(self, symbol: str, asof: dt.date, n: int = 3) -> list[dt.date]: ...
    def get_chain(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[Chain]: ...
    def spot(self, symbol: str, asof: dt.date) -> Optional[float]: ...
    def lot_size(self, symbol: str, asof: dt.date) -> int: ...
    def atm_iv_history(self, symbol: str, asof: dt.date, lookback: int = 252) -> list[float]: ...
    def trading_dates(self, symbol: str, after: dt.date, upto: Optional[dt.date] = None) -> list[dt.date]: ...


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


# ====================================================================== synthetic
class SyntheticProvider:
    name = "synthetic"

    def __init__(self, symbol: str, bars: list[dict[str, Any]], mkt: MarketConfig = MarketConfig(),
                 iv_scale: float = 1.0, skew: float = -0.6, curv: float = 2.0, vol_window: int = 20,
                 spread_pct: float = 3.0):
        self.symbol, self.bars, self.mkt = symbol, bars, mkt
        self.iv_scale, self.skew, self.curv, self.spread_pct = iv_scale, skew, curv, spread_pct
        self._dates = [b["date"] for b in bars]
        closes = [b["close"] for b in bars]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        self._rv: list[Optional[float]] = [None] * len(closes)        # annualised, uses returns up to and including i
        for i in range(vol_window, len(closes)):
            w = rets[i - vol_window:i]
            m = sum(w) / len(w)
            self._rv[i] = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1)) * math.sqrt(252)

    # -- helpers
    def _i(self, asof: dt.date) -> Optional[int]:
        i = bisect.bisect_right(self._dates, asof) - 1
        return i if i >= 0 else None

    def spot(self, symbol: str, asof: dt.date) -> Optional[float]:
        i = self._i(asof)
        return self.bars[i]["close"] if i is not None else None

    def lot_size(self, symbol: str, asof: dt.date) -> int:
        return self.mkt.lot_sizes.get(symbol) or default_lot_size(self.spot(symbol, asof) or 1000.0, self.mkt.lot_target_value)

    def trading_dates(self, symbol: str, after: dt.date, upto: Optional[dt.date] = None) -> list[dt.date]:
        return [d for d in self._dates if d > after and (upto is None or d <= upto)]

    def expiries(self, symbol: str, asof: dt.date, n: int = 3) -> list[dt.date]:
        return monthly_expiries(asof, n, self.mkt)

    def _atm_iv_at(self, i: int) -> Optional[float]:
        rv = self._rv[i]
        return None if rv is None else min(1.5, max(0.12, rv * self.iv_scale))

    def atm_iv_history(self, symbol: str, asof: dt.date, lookback: int = 252) -> list[float]:
        end = self._i(asof)
        if end is None:
            return []
        vals = [self._atm_iv_at(i) for i in range(max(0, end - lookback + 1), end + 1)]
        return [v for v in vals if v is not None]

    def _iv(self, atm: float, K: float, F: float) -> float:
        m = math.log(K / F)
        return max(0.05, atm * (1 + self.skew * m + self.curv * m * m))

    # -- chain
    def get_chain(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[Chain]:
        i = self._i(asof)
        if i is None or expiry <= asof:
            return None
        atm = self._atm_iv_at(i)
        if atm is None:
            return None
        S = self.bars[i]["close"]
        T = (expiry - asof).days / 365.0
        r, q = self.mkt.risk_free, self.mkt.div_yield
        F = S * math.exp((r - q) * T)
        step = nice_strike_step(S)
        k = math.floor(S * 0.70 / step) * step
        quotes: list[Quote] = []
        while k <= S * 1.30:
            if k > 0:
                iv = self._iv(atm, k, F)
                for kind in ("CE", "PE"):
                    mid = bs.bs_price(S, k, T, r, q, iv, kind)
                    if mid < 0.05:
                        continue
                    half = mid * self.spread_pct / 200.0
                    bid = max(0.05, _tick(mid - half))
                    ask = max(bid + 0.05, _tick(mid + half))
                    quotes.append(Quote(kind, round(k, 2), expiry, round((bid + ask) / 2, 2), bid, ask, iv))
            k += step
        return Chain(symbol, asof, S, expiry, self.lot_size(symbol, asof), quotes, futures_price=round(F, 2))


# ====================================================================== bhavcopy
class BhavcopyProvider:
    """Chains built from NSE F&O bhavcopy: real settlement prices, open interest and volume, but no bid/ask (so
    slippage uses CostModel.assumed_spread_pct) and no IV (solved here from the settlement price).
    Untraded contracts carry a theoretical settlement price: filter them with StrategyConfig.min_volume / min_oi."""
    name = "bhavcopy"

    def __init__(self, rows: dict[tuple[str, dt.date], list[dict[str, Any]]], bars: dict[str, list[dict[str, Any]]],
                 mkt: MarketConfig = MarketConfig(), iv_history: Optional[dict[str, list[float]]] = None):
        self.rows, self.bars, self.mkt = rows, bars, mkt
        self._iv_override = iv_history or {}          # when given (the market scan), IV history comes from the summary index, not from re-solving every day
        self._dates: dict[str, list[dt.date]] = {}
        for (sym, d) in rows:
            self._dates.setdefault(sym, []).append(d)
        for v in self._dates.values():
            v.sort()
        self._iv_cache: dict[tuple[str, dt.date], Optional[float]] = {}
        self._under: dict[tuple[str, dt.date], float] = {}
        self._lot: dict[tuple[str, dt.date], int] = {}
        for (sym, d), rs in rows.items():
            u = next((r["under"] for r in rs if r.get("under")), None)
            if u:
                self._under[(sym, d)] = u
            lot = next((r["lot"] for r in rs if r.get("lot")), None)
            if lot:
                self._lot[(sym, d)] = lot

    # -- construction
    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]], bars: Optional[dict[str, list[dict[str, Any]]]] = None,
                  mkt: MarketConfig = MarketConfig(), iv_history: Optional[dict[str, list[float]]] = None) -> "BhavcopyProvider":
        grouped: dict[tuple[str, dt.date], list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault((r["symbol"], r["date"]), []).append(r)
        return cls(grouped, bars or {}, mkt, iv_history)

    @classmethod
    def from_store(cls, store: BhavcopyStore, symbols: Optional[set[str]] = None, start: Optional[dt.date] = None,
                   end: Optional[dt.date] = None, bars: Optional[dict[str, list[dict[str, Any]]]] = None,
                   mkt: MarketConfig = MarketConfig()) -> "BhavcopyProvider":
        def rows():
            for d in store.dates():
                if (start and d < start) or (end and d > end):
                    continue
                yield from store.read_day(d, symbols)
        return cls.from_rows(rows(), bars, mkt)

    @classmethod
    def from_csv_files(cls, paths: Iterable[str | Path], bars: Optional[dict[str, list[dict[str, Any]]]] = None,
                       symbols: Optional[set[str]] = None, mkt: MarketConfig = MarketConfig(),
                       colmap: Optional[dict[str, str]] = None) -> "BhavcopyProvider":
        """Raw NSE files (zip or csv, either layout) straight into a provider, without a store."""
        rows: list[dict[str, Any]] = []
        for p in paths:
            try:
                rows += read_raw(Path(p).read_bytes(), colmap, symbols)
            except SchemaError as e:
                raise ValueError(f"{p}: {e} (pass colmap= to map the columns)") from e
        return cls.from_rows(rows, bars, mkt)

    # -- interface
    def symbols(self) -> list[str]:
        return sorted(self._dates)

    def spot(self, symbol: str, asof: dt.date) -> Optional[float]:
        ds = self._dates.get(symbol, [])
        i = bisect.bisect_right(ds, asof) - 1
        if i >= 0 and (symbol, ds[i]) in self._under:
            return self._under[(symbol, ds[i])]
        b = self.bars.get(symbol, [])
        j = bisect.bisect_right([x["date"] for x in b], asof) - 1
        return b[j]["close"] if j >= 0 else None

    def lot_size(self, symbol: str, asof: dt.date) -> int:
        ds = self._dates.get(symbol, [])
        i = bisect.bisect_right(ds, asof) - 1
        if i >= 0 and (symbol, ds[i]) in self._lot:
            return self._lot[(symbol, ds[i])]
        return self.mkt.lot_sizes.get(symbol) or default_lot_size(self.spot(symbol, asof) or 1000.0, self.mkt.lot_target_value)

    def trading_dates(self, symbol: str, after: dt.date, upto: Optional[dt.date] = None) -> list[dt.date]:
        return [d for d in self._dates.get(symbol, []) if d > after and (upto is None or d <= upto)]

    def expiries(self, symbol: str, asof: dt.date, n: int = 3) -> list[dt.date]:
        rs = self.rows.get((symbol, asof), [])
        return sorted({r["expiry"] for r in rs if r["inst"] == "OPT" and r["expiry"] > asof})[:n]

    def get_chain(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[Chain]:
        rs = self.rows.get((symbol, asof))
        S = self.spot(symbol, asof)
        if not rs or not S or expiry <= asof:
            return None
        T = (expiry - asof).days / 365.0
        r, q = self.mkt.risk_free, self.mkt.div_yield
        quotes = []
        for x in rs:
            if x["inst"] != "OPT" or x["expiry"] != expiry or not x["strike"]:
                continue
            iv = bs.implied_vol(x["price"], S, x["strike"], T, r, q, x["kind"])
            quotes.append(Quote(x["kind"], x["strike"], expiry, x["price"], None, None, iv, x["oi"], x["volume"]))
        fut = next((x["price"] for x in rs if x["inst"] == "FUT" and x["expiry"] == expiry), None)
        return Chain(symbol, asof, S, expiry, self.lot_size(symbol, asof), quotes, futures_price=fut) if quotes else None

    def oi_context(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[OiContext]:
        """Near-month futures price and OI today and on the previous trading day, plus yesterday's OI by strike for this expiry."""
        rs = self.rows.get((symbol, asof))
        if not rs:
            return None
        ds = self._dates.get(symbol, [])
        i = bisect.bisect_left(ds, asof) - 1
        prev_rows = self.rows.get((symbol, ds[i])) if i >= 0 else None
        live = sorted((r for r in rs if r["inst"] == "FUT" and r["expiry"] >= asof), key=lambda r: r["expiry"])
        fut = live[0] if live else None
        pfut = next((r for r in (prev_rows or []) if r["inst"] == "FUT" and fut and r["expiry"] == fut["expiry"]), None)
        prev_oi = {(r["kind"], r["strike"]): r["oi"] for r in (prev_rows or []) if r["inst"] == "OPT" and r["expiry"] == expiry and r["strike"]}
        return OiContext(fut["price"] if fut else None, fut["oi"] if fut else None, pfut["price"] if pfut else None, pfut["oi"] if pfut else None, prev_oi)

    def atm_iv_history(self, symbol: str, asof: dt.date, lookback: int = 252) -> list[float]:
        if symbol in self._iv_override:
            return self._iv_override[symbol][-lookback:]
        ds = [d for d in self._dates.get(symbol, []) if d <= asof][-lookback:]
        out: list[float] = []
        for d in ds:
            key = (symbol, d)
            if key not in self._iv_cache:
                iv = None
                for ex in self.expiries(symbol, d, 3):
                    if (ex - d).days >= 7:
                        c = self.get_chain(symbol, d, ex)
                        iv = c.atm_iv() if c else None
                        break
                self._iv_cache[key] = iv
            if self._iv_cache[key]:
                out.append(self._iv_cache[key])
        return out


# ====================================================================== live snapshot
class StaticProvider:
    """One day's real chains (for example fetched from an MCP get_option_chain tool and converted with
    chain_from_records) plus the stock's daily bars, so suggest() can run on live data.
    iv_history is a list of past ATM IVs (oldest first) from any source; without it IV rank is unknown and treated as
    neutral (50), which the suggestions flag."""
    name = "static"

    def __init__(self, symbol: str, bars: list[dict[str, Any]], chains: dict[dt.date, Chain],
                 iv_history: Optional[list[float]] = None, oi_context: Optional[OiContext] = None):
        self.symbol, self.bars, self.chains, self.iv_history = symbol, bars, chains, iv_history or []
        self._oi_ctx = oi_context

    def oi_context(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[OiContext]:
        return self._oi_ctx

    def expiries(self, symbol: str, asof: dt.date, n: int = 3) -> list[dt.date]:
        return sorted(e for e in self.chains if e > asof)[:n]

    def get_chain(self, symbol: str, asof: dt.date, expiry: dt.date) -> Optional[Chain]:
        c = self.chains.get(expiry)
        return c if c is not None and c.asof == asof else None

    def spot(self, symbol: str, asof: dt.date) -> Optional[float]:
        for c in self.chains.values():
            if c.asof == asof:
                return c.spot
        past = [b for b in self.bars if b["date"] <= asof]
        return past[-1]["close"] if past else None

    def lot_size(self, symbol: str, asof: dt.date) -> int:
        return next(iter(self.chains.values())).lot_size if self.chains else 1

    def atm_iv_history(self, symbol: str, asof: dt.date, lookback: int = 252) -> list[float]:
        return self.iv_history[-lookback:]

    def trading_dates(self, symbol: str, after: dt.date, upto: Optional[dt.date] = None) -> list[dt.date]:
        return [b["date"] for b in self.bars if b["date"] > after and (upto is None or b["date"] <= upto)]


# ====================================================================== generic records (live MCP output etc.)
_KEYS = {
    "strike": ["strike", "strikeprice", "strikepr", "strikepric"],
    "kind": ["optiontype", "opttype", "type", "optiontyp", "optntp"],
    "ltp": ["ltp", "lastprice", "lastpricetraded", "last"],
    "bid": ["bid", "bidprice", "buyprice1", "bidprice1"],
    "ask": ["ask", "askprice", "sellprice1", "askprice1", "offer"],
    "iv": ["iv", "impliedvolatility", "impliedvol"],
    "oi": ["oi", "openinterest", "openint"],
    "volume": ["volume", "totaltradedvolume", "tradedvolume", "totaltradedqty"],
}


def _nk(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _pick(d: dict[str, Any], field: str) -> Any:
    m = {_nk(k): v for k, v in d.items()}
    for name in _KEYS[field]:
        if m.get(name) not in (None, ""):
            return m[name]
    return None


def chain_from_records(symbol: str, asof: dt.date, spot: float, expiry: dt.date, records: list[dict[str, Any]],
                       lot_size: int, mkt: MarketConfig = MarketConfig(),
                       futures_price: Optional[float] = None) -> Chain:
    """Build a Chain from option-chain records as an MCP server or API returns them. Accepts one record per contract
    ({strike, optionType, ltp, bid, ask, iv, oi, volume}) or NSE's paired style ({strikePrice, CE: {...}, PE: {...}}).
    IV given above 3 is read as a percentage. A missing IV is solved from the price."""
    T = max(0.0, (expiry - asof).days / 365.0)
    flat: list[tuple[Optional[str], dict[str, Any]]] = []
    for rec in records:
        paired = {str(k).upper(): v for k, v in rec.items() if str(k).upper() in ("CE", "PE") and isinstance(v, dict)}
        if paired:
            strike = _pick(rec, "strike")
            for kind, sub in paired.items():
                flat.append((kind, {**sub, "strike": sub.get("strikePrice", strike)}))
        else:
            flat.append((None, rec))
    quotes: list[Quote] = []
    for kind, rec in flat:
        kind = kind or str(_pick(rec, "kind") or "").upper()[:2]
        strike, ltp = _num(_pick(rec, "strike")), _num(_pick(rec, "ltp"))
        if kind not in ("CE", "PE") or not strike or not ltp:
            continue
        iv = _num(_pick(rec, "iv"))
        iv = (iv / 100.0 if iv and iv > 3 else iv) or bs.implied_vol(ltp, spot, strike, T, mkt.risk_free, mkt.div_yield, kind)
        oi, vol = _num(_pick(rec, "oi")), _num(_pick(rec, "volume"))
        quotes.append(Quote(kind, strike, expiry, ltp, _num(_pick(rec, "bid")), _num(_pick(rec, "ask")), iv,
                            int(oi) if oi is not None else None, int(vol) if vol is not None else None))
    return Chain(symbol, asof, spot, expiry, lot_size, quotes, futures_price)
