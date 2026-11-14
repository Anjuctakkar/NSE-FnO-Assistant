import datetime as dt

try:
    from .test_bhavcopy import udiff_csv
    from .test_service import seeded_ctx
except ImportError:
    from test_bhavcopy import udiff_csv
    from test_service import seeded_ctx

from fno.bhavcopy import read_raw
from fno.config import CostModel, MarketConfig, StrategyConfig
from fno.data import BhavcopyProvider, SyntheticProvider
from fno.demo_data import make_synthetic_bars
from fno.oi import OiContext
from fno.selector import suggest
from fno.summary import SummaryIndex, build_day_summary

MKT, CM = MarketConfig(), CostModel()
RELAXED = StrategyConfig(min_score=0, futures_min_score=0, min_credit_to_width=0, min_pop_credit=0, max_theta_decay_pct_day=100, min_rr_debit=0, max_spread_pct=100)
D, EX = dt.date(2026, 10, 1), dt.date(2026, 10, 27)


def test_day_summary_recovers_iv_oi_futures_and_lot():
    rows = read_raw(udiff_csv(D, EX, symbols=("AAA", "BBB")))
    out = {r["symbol"]: r for r in build_day_summary(rows, MKT)}
    assert set(out) == {"AAA", "BBB"} and "NIFTY" not in out
    a = out["AAA"]
    assert abs(a["atm_iv"] - 0.30) < 0.01 and a["spot"] == 100.0 and a["expiry"] == EX and a["lot"] == 250
    assert a["call_oi"] == sum(1000 + k for k in (90, 95, 100, 105, 110)) and a["put_oi"] == a["call_oi"]
    assert a["atm_oi"] == 2 * 1100 and a["fut_price"] == 100.4 and a["fut_oi"] == 9000 and a["fut_expiry"] == EX
    assert a["opt_volume"] == 2 * sum(40 + k for k in (90, 95, 100, 105, 110))


def test_stocks_without_a_usable_chain_are_left_out_not_guessed():
    near = read_raw(udiff_csv(D, D + dt.timedelta(days=5)))                       # expires in 5 days: inside the 7-day cut-off
    assert build_day_summary(near, MKT) == []
    no_under = [dict(r, under=None) for r in read_raw(udiff_csv(D, EX))]
    assert build_day_summary(no_under, MKT) == []
    no_fut = [r for r in read_raw(udiff_csv(D, EX)) if r["inst"] == "OPT"]
    s = build_day_summary(no_fut, MKT)[0]
    assert s["fut_price"] is None and s["fut_oi"] is None and s["atm_iv"] is not None


def test_index_builds_in_resumable_chunks_and_matches_the_providers_iv_history():
    ctx, _ = seeded_ctx(days=90)
    idx = SummaryIndex(ctx.store, MKT)
    n = len(ctx.store.dates())
    assert len(idx.missing_days()) == n
    first = idx.build(max_days=40)
    assert first == {"built": 40, "remaining": n - 40}
    assert idx.build() == {"built": n - 40, "remaining": 0} and idx.build() == {"built": 0, "remaining": 0}      # idempotent
    data = idx.load()
    rows = data["TEST"]
    assert len(rows) == n and [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    prov = BhavcopyProvider.from_store(ctx.store, {"TEST"}, mkt=MKT)
    asof = ctx.store.dates()[-1]
    hist = prov.atm_iv_history("TEST", asof, 252)
    idx_hist = [r["atm_iv"] for r in rows if r["atm_iv"]]
    assert len(hist) == len(idx_hist) and max(abs(a - b) for a, b in zip(hist, idx_hist)) < 1e-9      # same rule, same numbers
    assert len(idx.load(upto=ctx.store.dates()[40])["TEST"]) == 41 and len(idx.load(lookback_days=10)["TEST"]) == 10


def test_provider_oi_context_compares_with_the_previous_trading_day_and_the_same_futures_series():
    d1, d2, ex2 = dt.date(2026, 10, 1), dt.date(2026, 10, 5), dt.date(2026, 11, 24)
    def day(d, fut_px, fut_oi, ce_oi):
        return [{"date": d, "symbol": "X", "inst": "OPT", "expiry": EX, "strike": 100.0, "kind": "CE", "price": 5.0, "oi": ce_oi, "volume": 9, "under": 100.0, "lot": 10},
                {"date": d, "symbol": "X", "inst": "FUT", "expiry": EX, "strike": None, "kind": "", "price": fut_px, "oi": fut_oi, "volume": 9, "under": 100.0, "lot": 10},
                {"date": d, "symbol": "X", "inst": "FUT", "expiry": ex2, "strike": None, "kind": "", "price": fut_px + 1, "oi": 5, "volume": 1, "under": 100.0, "lot": 10}]
    prov = BhavcopyProvider.from_rows(day(d1, 100.0, 1000, 500) + day(d2, 103.0, 1060, 650), mkt=MKT)
    c = prov.oi_context("X", d2, EX)
    assert (c.fut_price, c.fut_oi, c.prev_fut_price, c.prev_fut_oi) == (103.0, 1060, 100.0, 1000)       # near-month future, not the next series
    assert c.prev_oi == {("CE", 100.0): 500}
    first = prov.oi_context("X", d1, EX)                                                                # no previous day: no comparison, no crash
    assert first.prev_fut_price is None and first.prev_oi == {}
    assert prov.oi_context("X", dt.date(2026, 1, 1), EX) is None


class OiSynth(SyntheticProvider):
    """Synthetic prices, but with an OI picture: resistance 5% above spot, support 5% below, and a chosen futures build-up."""
    def __init__(self, *a, fut_move=(0.02, 0.08), **kw):
        super().__init__(*a, **kw)
        self.fut_move = fut_move

    def get_chain(self, symbol, asof, expiry):
        c = super().get_chain(symbol, asof, expiry)
        if c:
            up = min((q.strike for q in c.quotes if q.kind == "CE"), key=lambda k: abs(k - c.spot * 1.05))
            dn = min((q.strike for q in c.quotes if q.kind == "PE"), key=lambda k: abs(k - c.spot * 0.95))
            for q in c.quotes:
                q.oi, q.volume = (30000 if (q.kind == "CE" and q.strike == up) or (q.kind == "PE" and q.strike == dn) else 100), 50
        return c

    def oi_context(self, symbol, asof, expiry):
        p, o = self.fut_move
        return OiContext(100.0 * (1 + p), int(1_000_000 * (1 + o)), 100.0, 1_000_000, {})


def scored(provider, bars, asof, trend):
    res = suggest("TEST", bars, provider, asof, RELAXED, MKT, CM, top_n=6, override_trend=trend)
    return res, {s.structure.name: s for s in res.suggestions}


def test_oi_points_are_exactly_the_difference_between_scoring_with_and_without_oi():
    bars = make_synthetic_bars(400, seed=5)
    asof = bars[-30]["date"]
    plain, with_oi = SyntheticProvider("TEST", bars, MKT), OiSynth("TEST", bars, MKT)
    r0, base = scored(plain, bars[:-29], asof, "bullish")
    r1, boosted = scored(with_oi, bars[:-29], asof, "bullish")
    assert r0.oi is None and r1.oi is not None and r1.oi.buildup == "long_buildup"
    common = set(base) & set(boosted)
    assert common
    for name in common:
        pts = boosted[name].risk["score_parts"]["oi"]
        assert abs((boosted[name].score - base[name].score) - pts) < 0.11 or boosted[name].score >= 99.9, (name, pts)
        assert base[name].risk["oi"] is None and boosted[name].risk["oi"]["points"] == pts
        assert abs(sum(boosted[name].risk["score_parts"].values()) - boosted[name].score) < 0.2 or boosted[name].score >= 99.9
    assert any(boosted[n].risk["score_parts"]["oi"] > 0 for n in common)                          # a long build-up helps a bullish trade
    assert any("long build-up" in x for n in common for x in boosted[n].reasons)


def test_a_contradicting_oi_picture_lowers_the_score_and_says_why():
    bars = make_synthetic_bars(400, seed=5)
    asof = bars[-30]["date"]
    _, up = scored(OiSynth("TEST", bars, MKT, fut_move=(0.02, 0.08)), bars[:-29], asof, "bullish")
    _, down = scored(OiSynth("TEST", bars, MKT, fut_move=(-0.02, 0.08)), bars[:-29], asof, "bullish")        # short build-up against a bullish trade
    shared = set(up) & set(down)
    assert shared and all(down[n].score <= up[n].score + 1e-9 for n in shared)
    assert any(down[n].risk["score_parts"]["oi"] < up[n].risk["score_parts"]["oi"] for n in shared)
    assert any("short build-up" in x for n in shared for x in down[n].reasons)


def test_without_oi_scores_are_unchanged_from_before_oi_existed():
    bars = make_synthetic_bars(400, seed=5)
    r = suggest("TEST", bars, SyntheticProvider("TEST", bars, MKT), bars[-1]["date"], RELAXED, MKT, CM, top_n=6)
    for s in r.suggestions:
        assert s.risk["score_parts"]["oi"] == 0.0 and s.risk["oi"] is None and 0 <= s.score <= 100
        assert not any("build-up" in x or "OI wall" in x for x in s.reasons)
