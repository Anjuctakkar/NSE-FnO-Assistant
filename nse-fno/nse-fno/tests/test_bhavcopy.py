import csv
import datetime as dt
import gzip
import io
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fno import greeks as bs
from fno.bhavcopy import (BhavcopyStore, SchemaError, describe_file, detect_schema, read_canonical, read_raw)
from fno.config import MarketConfig
from fno.data import BhavcopyProvider

MKT = MarketConfig()
UDIFF_HEADER = ["TradDt", "BizDt", "Sgmt", "FinInstrmTp", "TckrSymb", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
                "FinInstrmNm", "OpnPric", "ClsPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "TtlTradgVol", "NewBrdLotQty"]


def udiff_csv(day: dt.date, expiry: dt.date, symbols=("TEST",), spot=100.0, sigma=0.30) -> bytes:
    T = (expiry - day).days / 365
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(UDIFF_HEADER)
    for sym in symbols:
        for k in (90, 95, 100, 105, 110):
            for kind in ("CE", "PE"):
                p = round(bs.bs_price(spot, k, T, MKT.risk_free, 0.0, sigma, kind), 2)
                w.writerow([day.isoformat(), day.isoformat(), "FO", "STO", sym, expiry.isoformat(), expiry.isoformat(), k, kind, "x",
                            p, p, spot, p, 1000 + k, 40 + k, 250])
        w.writerow([day.isoformat(), day.isoformat(), "FO", "STF", sym, expiry.isoformat(), expiry.isoformat(), "", "", "x", 100, 100.4, spot, 100.4, 9000, 500, 250])
    w.writerow([day.isoformat(), day.isoformat(), "FO", "IDO", "NIFTY", expiry.isoformat(), expiry.isoformat(), 25000, "CE", "x", 5, 5, 25000, 5, 1, 1, 75])
    w.writerow([day.isoformat(), day.isoformat(), "FO", "STO", "DEAD", expiry.isoformat(), expiry.isoformat(), 50, "CE", "x", 0, 0, 50, 0, 0, 0, 100])   # no price: dropped
    return out.getvalue().encode()


def as_zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


class FakeNse:
    """Local stand-in for NSE's archive: needs a browser-like User-Agent and the cookie set by the warm-up page."""

    def __init__(self, days: dict, block=False):
        self.days, self.block, self.hits, self.cookie_seen = days, block, [], False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/warm":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "nsit=abc; Path=/")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                outer.hits.append(self.path)
                if outer.block or "Mozilla" not in self.headers.get("User-Agent", "") or "nsit=abc" not in self.headers.get("Cookie", ""):
                    self.send_response(403)
                    self.end_headers()
                    return
                key = self.path.rsplit("_", 3)[-3] if "BhavCopy" in self.path else None
                body = outer.days.get(key)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def store(self, root=None) -> BhavcopyStore:
        t = f"http://127.0.0.1:{self.port}/content/fo/BhavCopy_NSE_FO_0_0_0_{{yyyymmdd}}_F_0000.csv.zip"
        return BhavcopyStore(root or tempfile.mkdtemp(), [t], pause=0, retries=0, warmup_url=f"http://127.0.0.1:{self.port}/warm")

    def close(self):
        self.srv.shutdown()


D1, D2 = dt.date(2026, 10, 1), dt.date(2026, 10, 5)          # Thu, Mon
EX = dt.date(2026, 10, 27)


def nse(**kw):
    days = {x.strftime("%Y%m%d"): as_zip(f"BhavCopy_NSE_FO_0_0_0_{x:%Y%m%d}_F_0000.csv", udiff_csv(x, EX)) for x in (D1, D2)}
    return FakeNse(days, **kw)


def test_download_needs_the_cookie_warmup_and_stores_a_compact_canonical_file():
    n = nse()
    try:
        s = n.store()
        r = s.fetch(D1)
        assert r.status == "downloaded" and "contracts" in r.message, r
        rows = read_canonical(s.path_for(D1))
        assert {x["symbol"] for x in rows} == {"TEST"}                      # index and unpriced rows are dropped
        assert sum(x["inst"] == "OPT" for x in rows) == 10 and sum(x["inst"] == "FUT" for x in rows) == 1
        assert rows[0]["lot"] == 250 and rows[0]["date"] == D1
        assert s.path_for(D1).stat().st_size < 3000                         # tiny next to the raw file
        assert s.coverage() == {"days": 1, "first": "2026-10-01", "last": "2026-10-01"} and s.symbols() == ["TEST"]
    finally:
        n.close()


def test_cached_days_are_not_downloaded_again_and_force_refetches():
    n = nse()
    try:
        s = n.store()
        s.fetch(D1)
        before = len(n.hits)
        assert s.fetch(D1).status == "cached" and len(n.hits) == before
        assert s.fetch(D1, force=True).status == "downloaded" and len(n.hits) == before + 1
    finally:
        n.close()


def test_holiday_weekend_and_blocked_are_reported_distinctly():
    n = nse()
    try:
        s = n.store()
        assert s.fetch(dt.date(2026, 10, 2)).status == "no_file"            # Friday with no file served: a holiday
        hits = len(n.hits)
        r = s.fetch(dt.date(2026, 10, 3))                                    # Saturday
        assert r.status == "no_file" and r.message == "weekend" and len(n.hits) == hits
    finally:
        n.close()
    b = nse(block=True)
    try:
        r = b.store().fetch(D1)
        assert r.status == "error" and "import" in r.message and "403" in r.message
    finally:
        b.close()


def test_sync_fills_gaps_skips_weekends_and_stops_on_an_error():
    n = nse()
    try:
        s = n.store()
        res = s.sync(dt.date(2026, 10, 1), dt.date(2026, 10, 6))
        by = {r.date: r.status for r in res}
        assert by[D1] == "downloaded" and by[D2] == "downloaded" and by[dt.date(2026, 10, 2)] == "no_file"
        assert dt.date(2026, 10, 3) not in by and dt.date(2026, 10, 4) not in by
        assert s.dates() == [D1, D2]
        again = {r.date: r.status for r in s.sync(D1, D2)}
        assert again == {D1: "cached", dt.date(2026, 10, 2): "no_file", D2: "cached"}     # a second sync only retries the gap
    finally:
        n.close()
    b = nse(block=True)
    try:
        res = b.store().sync(D1, dt.date(2026, 10, 9))
        assert len(res) == 1 and res[0].status == "error"                    # it does not hammer NSE once refused
    finally:
        b.close()


def test_a_file_for_the_wrong_day_and_unknown_layouts_are_rejected_loudly():
    s = BhavcopyStore(tempfile.mkdtemp(), [], warmup_url=None)
    r = s.ingest(udiff_csv(D1, EX), expect=D2)
    assert r.status == "error" and "2026-10-01" in r.message and not s.dates()
    bad = b"colA,colB\n1,2\n"
    r = s.import_bytes(bad)
    assert r.status == "error" and "colA" in r.message and "colmap" in r.message
    try:
        read_raw(bad)
    except SchemaError as e:
        assert e.header == ["colA", "colB"] and "symbol" in e.missing
    else:
        raise AssertionError("expected SchemaError")


def test_legacy_layout_is_normalised_to_the_same_schema():
    legacy = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"
              "OPTSTK,TEST,27-OCT-2026,100,CE,5,6,4,5.5,5.5,30,1,800,0,01-OCT-2026\n"
              "FUTSTK,TEST,27-OCT-2026,0,XX,100,101,99,100.4,100.4,50,1,5000,0,01-OCT-2026\n"
              "OPTIDX,NIFTY,27-OCT-2026,25000,CE,5,6,4,5.5,5.5,30,1,800,0,01-OCT-2026\n").encode()
    s = BhavcopyStore(tempfile.mkdtemp(), [], warmup_url=None)
    r = s.import_bytes(as_zip("fo01OCT2026bhav.csv", legacy))
    assert r.status == "imported" and r.date == D1
    rows = read_canonical(s.path_for(D1))
    opt = next(x for x in rows if x["inst"] == "OPT")
    assert (opt["strike"], opt["kind"], opt["price"], opt["oi"], opt["volume"], opt["lot"]) == (100.0, "CE", 5.5, 800, 30, None)
    assert [x["inst"] for x in rows].count("FUT") == 1 and all(x["symbol"] == "TEST" for x in rows)


def test_provider_from_store_rebuilds_chains_lots_and_iv():
    n = nse()
    try:
        s = n.store()
        s.sync(D1, D2)
    finally:
        n.close()
    prov = BhavcopyProvider.from_store(s, symbols={"TEST"}, mkt=MKT)
    assert prov.symbols() == ["TEST"] and prov.expiries("TEST", D1) == [EX]
    c = prov.get_chain("TEST", D1, EX)
    assert c.lot_size == 250 and c.spot == 100.0 and c.futures_price == 100.4
    assert abs(c.atm_iv() - 0.30) < 0.01
    assert c.get("CE", 105).volume == 145 and c.get("CE", 105).oi == 1105
    assert prov.trading_dates("TEST", D1) == [D2]
    assert len(prov.atm_iv_history("TEST", D2)) == 2


def test_describe_file_shows_the_mapping_for_a_new_download():
    p = Path(tempfile.mkdtemp()) / "fo.csv.zip"
    p.write_bytes(as_zip("x.csv", udiff_csv(D1, EX)))
    d = describe_file(p)
    assert d["missing_required"] == [] and d["mapped"]["symbol"] == "TckrSymb" and d["mapped"]["lot"] == "NewBrdLotQty"
    assert d["contracts_read"] == 11 and d["instruments_seen"] == ["FUT", "OPT"]
    assert detect_schema(["tckrsymb", "FINinstrmTP"])[1] != []              # case-insensitive, and reports what is still missing
