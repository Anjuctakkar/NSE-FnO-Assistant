"""
Open-interest (OI) analysis, used two ways: to rank stocks in the market scan, and to confirm or contradict a trade.

What it reads from one expiry's option chain (and, when available, the previous day):

  Futures build-up   futures price change against futures OI change:
                       price up   + OI up    long build-up     (new longs: bullish)
                       price down + OI up    short build-up    (new shorts: bearish)
                       price up   + OI down  short covering    (shorts leaving: a weaker bullish sign)
                       price down + OI down  long unwinding    (longs leaving: a weaker bearish sign)
  Put/call OI ratio  total put OI / total call OI for the expiry. High means more puts outstanding (often written, a
                     mildly bullish tilt), low the reverse. It is a tilt, not a signal on its own.
  OI walls           the strike with the most call OI above spot (resistance) and the most put OI below spot (support),
                     within 15% of spot, holding at least a tenth of that side's OI. Writers defend these, so a trade
                     whose short strike sits beyond a wall has something between it and the stock, and a trade that
                     needs the stock to break through one does not.
  Max pain           the strike where option buyers collectively lose most at expiry. A mild magnet near expiry.

oi_adjust() turns that into points (-20 to +20) added to a trade's score, with a sentence for each reason. These are
conventions traders use, not laws: OI shows positions, not who is right, and a "wall" can be written by someone who is
hedged elsewhere. The points are small on purpose, and every one is shown in the reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Chain, Structure

MIN_PRICE_MOVE = 0.003      # a futures move smaller than 0.3% is noise
MIN_OI_MOVE = 0.015         # an OI change smaller than 1.5% is noise
STRONG_PRICE, STRONG_OI = 0.01, 0.05
WALL_WINDOW = 0.15          # walls are searched within 15% of spot
MIN_WALL_SHARE = 0.10       # a strike holding under a tenth of its side's OI is not a wall, however it ranks
MAX_POINTS = 20.0

BULL = {"Long call", "Bull call spread", "Bull put spread", "Long future"}
BEAR = {"Long put", "Bear put spread", "Bear call spread", "Short future"}
BUILDUP_TEXT = {"long_buildup": "long build-up", "short_buildup": "short build-up", "short_covering": "short covering", "long_unwinding": "long unwinding"}


@dataclass
class OiContext:
    """What a data provider knows about OI beyond the chain itself."""
    fut_price: Optional[float] = None
    fut_oi: Optional[int] = None
    prev_fut_price: Optional[float] = None
    prev_fut_oi: Optional[int] = None
    prev_oi: dict = field(default_factory=dict)        # {(kind, strike): oi} on the previous trading day, same expiry


@dataclass
class OiProfile:
    call_oi: int
    put_oi: int
    pcr: Optional[float]
    call_wall: Optional[dict[str, Any]]                # {"strike", "oi", "share", "change_pct"}
    put_wall: Optional[dict[str, Any]]
    max_pain: Optional[float]
    buildup: Optional[str] = None
    buildup_strength: Optional[str] = None             # "strong" | "moderate"
    fut_price_chg: Optional[float] = None
    fut_oi_chg: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        r = lambda v, d=4: None if v is None else round(v, d)               # noqa: E731
        return {"call_oi": self.call_oi, "put_oi": self.put_oi, "pcr": r(self.pcr, 2), "call_wall": self.call_wall, "put_wall": self.put_wall,
                "max_pain": self.max_pain, "buildup": self.buildup, "buildup_label": BUILDUP_TEXT.get(self.buildup or ""),
                "buildup_strength": self.buildup_strength, "fut_price_chg": r(self.fut_price_chg), "fut_oi_chg": r(self.fut_oi_chg)}


def classify_buildup(price_chg: Optional[float], oi_chg: Optional[float]) -> tuple[Optional[str], Optional[str]]:
    """(label, strength) from fractional changes, or (None, None) when either move is too small to mean anything."""
    if price_chg is None or oi_chg is None or abs(price_chg) < MIN_PRICE_MOVE or abs(oi_chg) < MIN_OI_MOVE:
        return None, None
    label = ("long_buildup" if oi_chg > 0 else "short_covering") if price_chg > 0 else ("short_buildup" if oi_chg > 0 else "long_unwinding")
    strong = abs(price_chg) >= STRONG_PRICE and abs(oi_chg) >= STRONG_OI
    return label, "strong" if strong else "moderate"


def _wall(chain: Chain, kind: str, prev: dict, above: bool) -> Optional[dict[str, Any]]:
    eps = chain.spot * 1e-9                       # 100 * 1.15 is 114.99999999999999: a strike exactly on the edge must still count
    lo, hi = (chain.spot, chain.spot * (1 + WALL_WINDOW) + eps) if above else (chain.spot * (1 - WALL_WINDOW) - eps, chain.spot)
    side_total = sum(q.oi or 0 for q in chain.quotes if q.kind == kind)
    cands = [q for q in chain.quotes if q.kind == kind and q.oi and lo <= q.strike <= hi]
    if not cands or not side_total:
        return None
    w = max(cands, key=lambda q: q.oi)
    if w.oi / side_total < MIN_WALL_SHARE:
        return None
    before = prev.get((kind, w.strike))
    return {"strike": w.strike, "oi": int(w.oi), "share": round(w.oi / side_total, 3),
            "change_pct": round((w.oi - before) / before, 3) if before else None}


def max_pain(chain: Chain) -> Optional[float]:
    calls = [(q.strike, q.oi or 0) for q in chain.quotes if q.kind == "CE"]
    puts = [(q.strike, q.oi or 0) for q in chain.quotes if q.kind == "PE"]
    strikes = sorted({k for k, _ in calls} | {k for k, _ in puts})
    if not strikes or not any(o for _, o in calls + puts):
        return None
    pain = lambda K: sum(max(K - k, 0) * o for k, o in calls) + sum(max(k - K, 0) * o for k, o in puts)    # noqa: E731
    return min(strikes, key=pain)


def oi_profile(chain: Chain, ctx: Optional[OiContext] = None) -> Optional[OiProfile]:
    """None when the chain carries no open interest at all (synthetic data, or a feed without it)."""
    call_oi = sum(q.oi or 0 for q in chain.quotes if q.kind == "CE")
    put_oi = sum(q.oi or 0 for q in chain.quotes if q.kind == "PE")
    if call_oi + put_oi == 0:
        return None
    prev = ctx.prev_oi if ctx else {}
    p = OiProfile(call_oi, put_oi, put_oi / call_oi if call_oi else None, _wall(chain, "CE", prev, True), _wall(chain, "PE", prev, False), max_pain(chain))
    if ctx and ctx.fut_price and ctx.prev_fut_price and ctx.fut_oi is not None and ctx.prev_fut_oi:
        p.fut_price_chg = ctx.fut_price / ctx.prev_fut_price - 1
        p.fut_oi_chg = ctx.fut_oi / ctx.prev_fut_oi - 1
        p.buildup, p.buildup_strength = classify_buildup(p.fut_price_chg, p.fut_oi_chg)
    return p


def _k(x: float) -> str:
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"


def oi_adjust(s: Structure, chain: Chain, prof: Optional[OiProfile], atr: float = 0.0, target_atr: float = 3.0, stop_atr: float = 2.0) -> tuple[float, list[str], dict[str, float]]:
    """(points, reasons, points by source) for one candidate structure. Positive means OI backs the trade."""
    if prof is None:
        return 0.0, [], {}
    name = s.name
    d = 1 if name in BULL else -1 if name in BEAR else 0
    parts: dict[str, float] = {}
    why: list[str] = []

    # 1. futures build-up
    if prof.buildup:
        base = (6.0 if prof.buildup_strength == "strong" else 4.0) if prof.buildup in ("long_buildup", "short_buildup") else (3.0 if prof.buildup_strength == "strong" else 2.0)
        sign = 1 if prof.buildup in ("long_buildup", "short_covering") else -1
        pts = d * sign * base if d else (-3.0 if prof.buildup_strength == "strong" else 0.0)
        parts["buildup"] = pts
        verdict = ("supports" if pts > 0 else "works against" if pts < 0 else "does not affect") + (" a bullish view" if d > 0 else " a bearish view" if d < 0 else " a range-bound view")
        why.append(f"Futures price {prof.fut_price_chg * 100:+.1f}% with open interest {prof.fut_oi_chg * 100:+.1f}%: {BUILDUP_TEXT[prof.buildup]} ({prof.buildup_strength}), which {verdict}.")

    # 2. put/call OI ratio
    if prof.pcr is not None:
        hi, lo = prof.pcr >= 1.2, prof.pcr <= 0.7
        pts = (3.0 if hi else -3.0 if lo else 0.0) * d if d else (2.0 if 0.8 <= prof.pcr <= 1.2 else -1.0)
        parts["pcr"] = pts
        tilt = "more puts than calls outstanding: a mildly bullish tilt" if hi else "more calls than puts outstanding: a mildly bearish tilt" if lo else "calls and puts roughly balanced"
        why.append(f"Put/call OI ratio {prof.pcr:.2f}: {tilt}" + (f" ({'helps' if pts > 0 else 'hurts'} this trade)." if pts else "."))

    # 3. walls against this trade's strikes
    cw, pw = prof.call_wall, prof.put_wall
    short = {l.kind: l.strike for l in s.legs if l.side < 0 and l.kind in ("CE", "PE")}
    be = s.payoff_stats()["breakevens"]
    def wall_txt(w: dict[str, Any], side: str) -> str:
        chg = f", {w['change_pct'] * 100:+.0f}% today" if w.get("change_pct") is not None else ""
        return f"{side} wall at {_k(w['strike'])} ({w['share'] * 100:.0f}% of all {'call' if side == 'call' else 'put'} OI{chg})"
    if name == "Bull put spread" and pw and "PE" in short:
        ok = short["PE"] <= pw["strike"]
        parts["wall"] = 4.0 if ok else -2.0
        why.append(f"Largest {wall_txt(pw, 'put')} " + ("sits above your short put: there is support between the stock and your strike." if ok else "is below your short put: your strike is above the main support."))
    elif name == "Bear call spread" and cw and "CE" in short:
        ok = short["CE"] >= cw["strike"]
        parts["wall"] = 4.0 if ok else -2.0
        why.append(f"Largest {wall_txt(cw, 'call')} " + ("sits below your short call: there is resistance between the stock and your strike." if ok else "is above your short call: your strike is below the main resistance."))
    elif name == "Iron condor" and (cw or pw):
        p_ok = bool(pw and "PE" in short and short["PE"] <= pw["strike"])
        c_ok = bool(cw and "CE" in short and short["CE"] >= cw["strike"])
        known = (1 if pw else 0) + (1 if cw else 0)
        parts["wall"] = 6.0 if p_ok and c_ok else 2.0 if (p_ok or c_ok) else -3.0 if known == 2 else 0.0
        why.append(f"OI walls: " + (wall_txt(pw, "put") if pw else "no put wall") + "; " + (wall_txt(cw, "call") if cw else "no call wall") +
                   f". {'Both' if p_ok and c_ok else 'One' if (p_ok or c_ok) else 'Neither'} of your short strikes is {'protected by a wall' if (p_ok or c_ok) else 'beyond a wall'}.")
    elif name in ("Bull call spread", "Long call") and cw and be:
        if be[-1] > cw["strike"]:
            parts["wall"] = -4.0
            why.append(f"Largest {wall_txt(cw, 'call')} is below your breakeven {_k(be[-1])}: the stock must break through resistance for this to pay.")
        elif name == "Bull call spread" and "CE" in short and short["CE"] <= cw["strike"]:
            parts["wall"] = 2.0
            why.append(f"Largest {wall_txt(cw, 'call')} is at or above your short call: the profit zone ends before the main resistance.")
    elif name in ("Bear put spread", "Long put") and pw and be:
        if be[0] < pw["strike"]:
            parts["wall"] = -4.0
            why.append(f"Largest {wall_txt(pw, 'put')} is above your breakeven {_k(be[0])}: the stock must break through support for this to pay.")
        elif name == "Bear put spread" and "PE" in short and short["PE"] >= pw["strike"]:
            parts["wall"] = 2.0
            why.append(f"Largest {wall_txt(pw, 'put')} is at or below your short put: the profit zone ends before the main support.")
    elif name == "Long future" and atr:
        if cw and cw["strike"] < chain.spot + target_atr * atr:
            parts["wall"] = -3.0
            why.append(f"Largest {wall_txt(cw, 'call')} is inside the target distance: resistance before the target.")
        elif pw and pw["strike"] >= chain.spot - stop_atr * atr:
            parts["wall"] = 2.0
            why.append(f"Largest {wall_txt(pw, 'put')} is inside the stop distance: support under the entry.")
    elif name == "Short future" and atr:
        if pw and pw["strike"] > chain.spot - target_atr * atr:
            parts["wall"] = -3.0
            why.append(f"Largest {wall_txt(pw, 'put')} is inside the target distance: support before the target.")
        elif cw and cw["strike"] <= chain.spot + stop_atr * atr:
            parts["wall"] = 2.0
            why.append(f"Largest {wall_txt(cw, 'call')} is inside the stop distance: resistance above the entry.")

    # 4. max pain, for range-bound trades
    if prof.max_pain is not None:
        if name == "Iron condor" and "PE" in short and "CE" in short:
            inside = short["PE"] <= prof.max_pain <= short["CE"]
            parts["max_pain"] = 2.0 if inside else 0.0
            why.append(f"Max pain is {_k(prof.max_pain)}, " + ("between your short strikes." if inside else "outside your short strikes."))
        else:
            why.append(f"Max pain for this expiry is {_k(prof.max_pain)}.")

    total = max(-MAX_POINTS, min(MAX_POINTS, sum(parts.values())))
    return total, why, parts
