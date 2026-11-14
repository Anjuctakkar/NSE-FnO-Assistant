import csv
import datetime as dt
import os
import tempfile

from fno import greeks as bs
from fno.config import MarketConfig
from fno.data import BhavcopyProvider, SyntheticProvider, chain_from_records
from fno.demo_data import make_synthetic_bars

MKT = MarketConfig()
D, EX = dt.date(2026, 10, 1), dt.date(2026, 10, 27)
T = (EX - D).days / 365


def px(k, kind):
    return round(bs.bs_price(100.0, k, T, MKT.risk_free, 0.0, 0.30, kind), 2)


def write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_bhavcopy_udiff_layout_builds_a_chain_and_recovers_iv():
    p = os.path.join(tempfile.mkdtemp(), "fo.csv")
    hdr = ["TradDt", "FinInstrmTp", "TckrSymb", "FininstrmActlXpryDt", "StrkPric", "OptnTp", "ClsPric", "SttlmPric", "OpnIntrst", "TtlTradgVol", "UndrlygPric"]
    rows = [[D.isoformat(), "STO", "TEST", EX.isoformat(), k, kind, px(k, kind), px(k, kind), 1000 + k, 50, 100.0] for k in (90, 100, 110) for kind in ("CE", "PE")]
    rows.append([D.isoformat(), "STF", "TEST", EX.isoformat(), "", "", 100.4, 100.4, 5000, 100, 100.0])
    rows.append([D.isoformat(), "IDO", "NIFTY", EX.isoformat(), 25000, "CE", 100, 100, 1, 1, 25000])       # index rows are ignored
    write(p, hdr, rows)
    prov = BhavcopyProvider.from_csv_files([p], symbols={"TEST"}, mkt=MKT)
    assert prov.expiries("TEST", D) == [EX]
    c = prov.get_chain("TEST", D, EX)
    assert c.spot == 100.0 and c.futures_price == 100.4 and len(c.quotes) == 6
    assert abs(c.atm_iv() - 0.30) < 0.01
    assert c.get("CE", 110).oi == 1110 and c.get("CE", 110).bid is None
    assert prov.get_chain("TEST", D, dt.date(2026, 12, 1)) is None


def test_bhavcopy_legacy_layout_uses_cash_bars_for_spot():
    p = os.path.join(tempfile.mkdtemp(), "fo_old.csv")
    hdr = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "CLOSE", "SETTLE_PR", "OPEN_INT", "CONTRACTS", "TIMESTAMP"]
    rows = [["OPTSTK", "TEST", "27-OCT-2026", k, kind, px(k, kind), px(k, kind), 10, 5, "01-OCT-2026"] for k in (95, 100, 105) for kind in ("CE", "PE")]
    write(p, hdr, rows)
    bars = {"TEST": [{"date": D, "open": 100, "high": 101, "low": 99, "close": 100.0}]}
    prov = BhavcopyProvider.from_csv_files([p], bars=bars, mkt=MKT)
    c = prov.get_chain("TEST", D, EX)
    assert c and abs(c.atm_iv() - 0.30) < 0.01 and prov.spot("TEST", D) == 100.0


def test_bhavcopy_unknown_header_fails_loudly():
    p = os.path.join(tempfile.mkdtemp(), "bad.csv")
    write(p, ["a", "b"], [[1, 2]])
    try:
        BhavcopyProvider.from_csv_files([p])
    except ValueError as e:
        assert "colmap" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_chain_from_records_paired_and_flat_styles():
    paired = [{"strikePrice": 100, "CE": {"lastPrice": px(100, "CE"), "impliedVolatility": 30.5, "openInterest": 1200, "bidprice": 5.9, "askPrice": 6.1},
               "PE": {"lastPrice": px(100, "PE"), "openInterest": 900}}]
    c = chain_from_records("TEST", D, 100.0, EX, paired, 500, MKT)
    ce, pe = c.get("CE", 100), c.get("PE", 100)
    assert abs(ce.iv - 0.305) < 1e-9 and ce.oi == 1200 and ce.bid == 5.9
    assert abs(pe.iv - 0.30) < 0.01                                  # missing IV was solved from the price
    flat = [{"strike": 100, "optionType": "CE", "ltp": px(100, "CE"), "iv": 0.3}, {"strike": 0, "optionType": "CE", "ltp": 1}, {"foo": 1}]
    assert len(chain_from_records("TEST", D, 100.0, EX, flat, 500, MKT).quotes) == 1


def test_synthetic_chain_is_arbitrage_consistent_and_spreads_are_positive():
    bars = make_synthetic_bars(300, seed=2)
    prov = SyntheticProvider("TEST", bars, MKT)
    asof = bars[-1]["date"]
    ex = prov.expiries("TEST", asof, 2)[1]
    c = prov.get_chain("TEST", asof, ex)
    calls = sorted((q for q in c.quotes if q.kind == "CE"), key=lambda q: q.strike)
    assert all(a.mid >= b.mid - 0.06 for a, b in zip(calls, calls[1:]))      # call prices fall as strike rises
    assert all(q.ask > q.bid > 0 for q in c.quotes)
    assert c.atm_iv() and 0.05 < c.atm_iv() < 1.6
    assert prov.get_chain("TEST", asof, asof) is None
    hist = prov.atm_iv_history("TEST", asof, 252)
    assert 30 < len(hist) <= 252


def test_static_provider_runs_the_selector_on_a_live_style_snapshot():
    from fno.config import CostModel, StrategyConfig
    from fno.data import StaticProvider
    from fno.selector import suggest
    bars = make_synthetic_bars(300, seed=6)
    syn = SyntheticProvider("TEST", bars, MKT)
    asof = bars[-1]["date"]
    ex = syn.expiries("TEST", asof, 3)[1]
    chain = syn.get_chain("TEST", asof, ex)
    live = StaticProvider("TEST", bars, {ex: chain}, iv_history=None)
    assert live.expiries("TEST", asof) == [ex] and live.get_chain("TEST", asof, ex) is chain
    assert live.get_chain("TEST", asof + dt.timedelta(days=1), ex) is None          # a snapshot is only valid for its own day
    cfg = StrategyConfig(min_score=0, futures_min_score=0, min_credit_to_width=0, min_pop_credit=0, max_theta_decay_pct_day=100,
                         min_rr_debit=0, max_spread_pct=100)
    res = suggest("TEST", bars, live, asof, cfg, MKT, CostModel(), override_trend="bullish")
    assert res.regime.iv_rank is None
    assert res.suggestions and any("No IV history" in w for w in res.suggestions[0].warnings)
