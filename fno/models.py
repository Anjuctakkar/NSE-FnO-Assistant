"""Core data models. Everything is a plain dataclass with to_dict/from_dict so suggestions can be logged to
JSON and replayed later (see evaluate.py)."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from . import greeks as bs


def _d(s: Any) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s)[:10])


# ------------------------------------------------------------------ market data
@dataclass
class Quote:
    kind: str                      # "CE" or "PE"
    strike: float
    expiry: dt.date
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None     # annual, fraction
    oi: Optional[int] = None
    volume: Optional[int] = None

    @property
    def mid(self) -> float:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread_pct(self) -> Optional[float]:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.ask - self.bid) / self.mid * 100.0
        return None


@dataclass
class Chain:
    """One expiry's option chain on one date."""
    symbol: str
    asof: dt.date
    spot: float
    expiry: dt.date
    lot_size: int
    quotes: list[Quote]
    futures_price: Optional[float] = None

    def get(self, kind: str, strike: float) -> Optional[Quote]:
        for q in self.quotes:
            if q.kind == kind and abs(q.strike - strike) < 1e-9:
                return q
        return None

    def strikes(self) -> list[float]:
        return sorted({q.strike for q in self.quotes})

    def atm_strike(self) -> float:
        return min(self.strikes(), key=lambda k: abs(k - self.spot))

    def atm_iv(self) -> Optional[float]:
        k = self.atm_strike()
        ivs = [q.iv for q in self.quotes if abs(q.strike - k) < 1e-9 and q.iv]
        if ivs:
            return sum(ivs) / len(ivs)
        near = sorted((q for q in self.quotes if q.iv), key=lambda q: abs(q.strike - self.spot))[:2]
        return sum(q.iv for q in near) / len(near) if near else None


# ------------------------------------------------------------------ positions
@dataclass
class Leg:
    kind: str                      # "CE", "PE" or "FUT"
    side: int                      # +1 bought, -1 sold
    strike: Optional[float]        # None for futures
    expiry: dt.date
    entry_price: float             # the fill, after slippage
    iv: Optional[float] = None     # implied vol at entry (options)
    lots: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "side": self.side, "strike": self.strike, "expiry": self.expiry.isoformat(),
                "entry_price": self.entry_price, "iv": self.iv, "lots": self.lots}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Leg":
        return Leg(d["kind"], int(d["side"]), d.get("strike"), _d(d["expiry"]), float(d["entry_price"]),
                   d.get("iv"), int(d.get("lots", 1)))


@dataclass
class Structure:
    name: str
    symbol: str
    legs: list[Leg]
    lot_size: int
    opened: dt.date

    @property
    def expiry(self) -> dt.date:
        return self.legs[0].expiry

    def units(self, leg: Leg) -> int:
        return leg.lots * self.lot_size

    @property
    def has_future(self) -> bool:
        return any(l.kind == "FUT" for l in self.legs)

    def net_premium(self) -> float:
        """Rupees received (+) or paid (-) at entry. Futures legs carry no premium."""
        return sum(-l.side * l.entry_price * self.units(l) for l in self.legs if l.kind != "FUT")

    def scaled(self, lots: int) -> "Structure":
        return Structure(self.name, self.symbol, [Leg(l.kind, l.side, l.strike, l.expiry, l.entry_price, l.iv, lots)
                                                  for l in self.legs], self.lot_size, self.opened)

    def payoff(self, S: float) -> float:
        """Gross rupee P&L if every leg is held to expiry and the underlying settles at S (before costs)."""
        total = 0.0
        for l in self.legs:
            u = self.units(l)
            if l.kind == "FUT":
                total += l.side * (S - l.entry_price) * u
            else:
                intrinsic = max(S - l.strike, 0.0) if l.kind == "CE" else max(l.strike - S, 0.0)
                total += l.side * (intrinsic - l.entry_price) * u
        return total

    def mark_pnl(self, prices: list[float]) -> float:
        """Gross rupee P&L at current leg prices (same order as legs)."""
        return sum(l.side * (p - l.entry_price) * self.units(l) for l, p in zip(self.legs, prices))

    def payoff_stats(self) -> dict[str, Any]:
        """Max profit / max loss / breakevens at expiry. max_profit is inf when profit is unbounded; max_loss is
        -inf when loss is unbounded. Futures are flagged not defined-risk (their real risk is the stop)."""
        strikes = sorted({l.strike for l in self.legs if l.strike})
        hi = (max(strikes) if strikes else max(l.entry_price for l in self.legs)) * 3.0
        pts = [0.0] + strikes + [hi]
        vals = [self.payoff(s) for s in pts]
        slope_hi = self.payoff(hi + 1.0) - vals[-1]
        max_profit = math.inf if slope_hi > 1e-9 else max(vals)
        max_loss = -math.inf if slope_hi < -1e-9 else min(vals)
        be: list[float] = []
        for (s0, v0), (s1, v1) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
            if v0 == 0 and s0 > 0:
                be.append(s0)
            elif v0 * v1 < 0:
                be.append(s0 + (0 - v0) * (s1 - s0) / (v1 - v0))
        return {"max_profit": max_profit, "max_loss": max_loss, "breakevens": sorted({round(b, 2) for b in be}),
                "defined_risk": math.isfinite(max_loss) and not self.has_future}

    def net_greeks(self, spot: float, asof: dt.date, r: float, q: float,
                   ivs: Optional[list[Optional[float]]] = None) -> dict[str, float]:
        """Position Greeks in rupee terms: delta in underlying units (shares-equivalent), gamma as change in
        that delta per Rs 1, theta in Rs per calendar day, vega in Rs per vol point."""
        out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        for i, l in enumerate(self.legs):
            u = self.units(l) * l.side
            if l.kind == "FUT":
                out["delta"] += u
                continue
            T = max(0.0, (l.expiry - asof).days / 365.0)
            iv = (ivs[i] if ivs and ivs[i] else None) or l.iv or 0.30
            g = bs.greeks(spot, l.strike, T, r, q, iv, l.kind)
            out["delta"] += g.delta * u
            out["gamma"] += g.gamma * u
            out["theta"] += g.theta * u
            out["vega"] += g.vega * u
            out["rho"] += g.rho * u
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "symbol": self.symbol, "lot_size": self.lot_size,
                "opened": self.opened.isoformat(), "legs": [l.to_dict() for l in self.legs]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Structure":
        return Structure(d["name"], d["symbol"], [Leg.from_dict(x) for x in d["legs"]], int(d["lot_size"]), _d(d["opened"]))


# ------------------------------------------------------------------ plans and suggestions
@dataclass
class ExitPlan:
    profit_target: float                         # rupees, gross mark-to-market P&L at which to take profit
    stop_loss: float                             # rupees, negative: gross P&L at which to cut the loss
    exit_dte: int                                # close when this many calendar days (or fewer) remain
    invalidation: Optional[tuple[str, float]] = None   # ("below"|"above", underlying level) on a daily close

    def to_dict(self) -> dict[str, Any]:
        return {"profit_target": self.profit_target, "stop_loss": self.stop_loss, "exit_dte": self.exit_dte,
                "invalidation": list(self.invalidation) if self.invalidation else None}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ExitPlan":
        inv = d.get("invalidation")
        return ExitPlan(float(d["profit_target"]), float(d["stop_loss"]), int(d["exit_dte"]),
                        (inv[0], float(inv[1])) if inv else None)


def _finite(x: float) -> Optional[float]:
    return None if x is None or not math.isfinite(x) else round(x, 2)


@dataclass
class Suggestion:
    id: str
    symbol: str
    asof: dt.date
    structure: Structure
    exit_plan: ExitPlan
    score: float
    reasons: list[str]
    risk: dict[str, Any]           # max_profit, max_loss, breakevens, net_premium, defined_risk, cost_roundtrip ...
    greeks: dict[str, float]       # net position Greeks at entry
    pop: Optional[float]           # model-implied probability of profit if held to expiry
    regime: dict[str, Any]         # trend, trend_score, iv_rank, rsi ... at the time
    dte: int
    warnings: list[str] = field(default_factory=list)
    win: dict[str, Any] = field(default_factory=dict)   # the win-probability block (probability.refine): managed simulation + calibrated p

    def to_dict(self) -> dict[str, Any]:
        r = dict(self.risk)
        for k in ("max_profit", "max_loss"):
            if k in r and r[k] is not None and not math.isfinite(r[k]):
                r[k] = None
                r[k + "_unbounded"] = True
        return {"id": self.id, "symbol": self.symbol, "asof": self.asof.isoformat(), "structure": self.structure.to_dict(),
                "exit_plan": self.exit_plan.to_dict(), "score": round(self.score, 1), "reasons": self.reasons,
                "risk": r, "greeks": {k: round(v, 4) for k, v in self.greeks.items()}, "pop": self.pop,
                "regime": self.regime, "dte": self.dte, "warnings": self.warnings, "win": self.win}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Suggestion":
        r = dict(d.get("risk", {}))
        if r.get("max_profit_unbounded"):
            r["max_profit"] = math.inf
        if r.get("max_loss_unbounded"):
            r["max_loss"] = -math.inf
        return Suggestion(d["id"], d["symbol"], _d(d["asof"]), Structure.from_dict(d["structure"]),
                          ExitPlan.from_dict(d["exit_plan"]), float(d["score"]), list(d.get("reasons", [])), r,
                          dict(d.get("greeks", {})), d.get("pop"), dict(d.get("regime", {})), int(d.get("dte", 0)),
                          list(d.get("warnings", [])), dict(d.get("win", {})))
