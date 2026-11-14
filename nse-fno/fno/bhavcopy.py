"""
NSE F&O bhavcopy connector.

    store = BhavcopyStore("data/bhavcopy")
    store.sync(date(2026, 9, 1), date(2026, 10, 23))      # download what is missing
    store.import_path("fo_bhavcopy_downloaded_in_browser.zip")
    provider = BhavcopyProvider.from_store(store, symbols={"TCS"}, bars={"TCS": bars})

Why a store and not just "read the CSV": NSE's raw F&O file is large (index and stock contracts, ~35 columns), its
layout has changed before (the UDiFF format replaced the old foDDMMMYYYYbhav layout in July 2024) and NSE is still
changing how bhavcopy is distributed. So every file is converted ON INGEST to one compact canonical schema and kept
gzipped, one file per trading day:

    date, symbol, inst (OPT|FUT), expiry, strike, kind (CE|PE), price, oi, volume, under, lot

Everything downstream reads only that schema. If NSE changes a layout again, only detect_schema / FIELD_ALIASES here
changes, and a file that cannot be read fails loudly with its header instead of producing silent garbage.

STATUS OF THE URL AND COLUMN NAMES: the public CSV URL pattern for F&O below follows the pattern NSE uses for the cash
market (confirmed) and for derivatives (widely used, NOT confirmed by me against the live site). The UDiFF column names
were written from memory and likewise unverified. Both are configurable, and `python -m fno bhav schema FILE` prints
how a real file maps. NSE sometimes refuses non-browser clients: manual import is the supported fallback.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import http.cookiejar
import io
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

# ---------------------------------------------------------------- where the files live (configurable)
DEFAULT_URL_TEMPLATES = [
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",           # UDiFF, current layout
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{MON}/fo{dd}{MON}{yyyy}bhav.csv.zip",  # legacy layout (older dates)
]
WARMUP_URL = "https://www.nseindia.com/all-reports-derivatives"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------- schema
# Candidate header names per field, UDiFF first then the legacy layout. Matching is case-insensitive.
FIELD_ALIASES: dict[str, list[str]] = {
    "symbol": ["TckrSymb", "SYMBOL"],
    "instrument": ["FinInstrmTp", "INSTRUMENT"],
    "expiry": ["FininstrmActlXpryDt", "XpryDt", "EXPIRY_DT"],
    "strike": ["StrkPric", "STRIKE_PR"],
    "opt": ["OptnTp", "OPTION_TYP"],
    "close": ["ClsPric", "CLOSE"],
    "settle": ["SttlmPric", "SETTLE_PR"],
    "oi": ["OpnIntrst", "OPEN_INT"],
    "volume": ["TtlTradgVol", "CONTRACTS"],
    "date": ["TradDt", "TIMESTAMP"],
    "under": ["UndrlygPric"],
    "lot": ["NewBrdLotQty"],
}
REQUIRED = ("symbol", "instrument", "expiry", "strike", "opt", "date")
STOCK_OPT, STOCK_FUT = {"STO", "OPTSTK"}, {"STF", "FUTSTK"}
INDEX_OPT, INDEX_FUT = {"IDO", "OPTIDX"}, {"IDF", "FUTIDX"}
CANONICAL = ["date", "symbol", "inst", "expiry", "strike", "kind", "price", "oi", "volume", "under", "lot"]


class SchemaError(ValueError):
    """A file whose header does not match any known bhavcopy layout (or that has no usable contracts)."""

    def __init__(self, message: str, header: Optional[list[str]] = None, missing: Optional[list[str]] = None):
        super().__init__(message)
        self.header, self.missing = header or [], missing or []


def detect_schema(header: Iterable[str], colmap: Optional[dict[str, str]] = None) -> tuple[dict[str, str], list[str]]:
    """(field -> header name, missing required fields). colmap overrides: {"symbol": "MySymbolColumn"}."""
    lookup = {h.strip().lower(): h.strip() for h in header if h}
    found: dict[str, str] = {}
    for field, cands in FIELD_ALIASES.items():
        pick = lookup.get(colmap[field].strip().lower()) if colmap and colmap.get(field) else None
        for c in cands if pick is None else ():
            if c.lower() in lookup:
                pick = lookup[c.lower()]
                break
        if pick:
            found[field] = pick
    return found, [f for f in REQUIRED if f not in found]


def parse_date(s: Any) -> Optional[dt.date]:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s[:11] if fmt == "%d-%b-%Y" else s, fmt).date()
        except ValueError:
            continue
    return None


def num(v: Any) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "").strip())
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def normalise_rows(raw_rows: Iterable[dict[str, Any]], schema: dict[str, str], symbols: Optional[set[str]] = None,
                   include_index: bool = False) -> Iterator[dict[str, Any]]:
    """Raw bhavcopy rows -> canonical rows. Skips other instruments, contracts with no price, and malformed rows."""
    opt_types = STOCK_OPT | (INDEX_OPT if include_index else set())
    fut_types = STOCK_FUT | (INDEX_FUT if include_index else set())
    g = lambda r, f: r.get(schema[f]) if f in schema else None      # noqa: E731
    for raw in raw_rows:
        r = {str(k).strip(): v for k, v in raw.items() if k}
        inst = str(g(r, "instrument") or "").strip().upper()
        is_opt = inst in opt_types
        if not is_opt and inst not in fut_types:
            continue
        sym = str(g(r, "symbol") or "").strip().upper()
        if not sym or (symbols and sym not in symbols):
            continue
        d, ex = parse_date(g(r, "date")), parse_date(g(r, "expiry"))
        price = num(g(r, "settle")) or num(g(r, "close"))
        if d is None or ex is None or not price or price <= 0:
            continue
        kind, strike = "", None
        if is_opt:
            kind, strike = str(g(r, "opt") or "").strip().upper(), num(g(r, "strike"))
            if kind not in ("CE", "PE") or not strike or strike <= 0:
                continue
        yield {"date": d, "symbol": sym, "inst": "OPT" if is_opt else "FUT", "expiry": ex, "strike": strike, "kind": kind,
               "price": price, "oi": int(num(g(r, "oi")) or 0), "volume": int(num(g(r, "volume")) or 0),
               "under": num(g(r, "under")), "lot": int(num(g(r, "lot")) or 0) or None}


def read_raw(data: bytes, colmap: Optional[dict[str, str]] = None, symbols: Optional[set[str]] = None,
             include_index: bool = False) -> list[dict[str, Any]]:
    """Bytes of a raw bhavcopy (a zip holding a CSV, or the CSV itself) -> canonical rows. Raises SchemaError."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise SchemaError("the zip contains no .csv file")
            data = z.read(names[0])
    text = io.StringIO(data.decode("utf-8-sig", errors="replace"), newline="")
    reader = csv.DictReader(text)
    header = [h.strip() for h in (reader.fieldnames or []) if h]
    schema, missing = detect_schema(header, colmap)
    if missing:
        raise SchemaError(f"unrecognised bhavcopy layout: no column found for {missing}. Header is {header}. "
                          f"Add the names to FIELD_ALIASES or pass colmap.", header, missing)
    rows = list(normalise_rows(reader, schema, symbols, include_index))
    if not rows:
        raise SchemaError("the file has a known layout but no usable stock option or futures contracts", header)
    return rows


# ---------------------------------------------------------------- canonical files
def write_canonical(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CANONICAL)
            for r in sorted(rows, key=lambda x: (x["symbol"], x["inst"], x["expiry"], x["strike"] or 0, x["kind"])):
                w.writerow([r["date"].isoformat(), r["symbol"], r["inst"], r["expiry"].isoformat(),
                            "" if r["strike"] is None else r["strike"], r["kind"], r["price"], r["oi"], r["volume"],
                            "" if r["under"] is None else r["under"], r["lot"] or ""])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_canonical(path: Path, symbols: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Rows of one day file. With `symbols`, lines for other stocks are dropped before any CSV parsing (a day file holds
    ~200 stocks; loading one symbol across a year of files would otherwise parse almost everything for nothing)."""
    out: list[dict[str, Any]] = []
    needles = [f",{x}," for x in symbols] if symbols else None
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        header = next(csv.reader([f.readline()]))
        lines = (ln for ln in f if needles is None or any(n in ln for n in needles))
        for r in csv.DictReader(lines, fieldnames=header):
            if symbols and r["symbol"] not in symbols:           # the needle can also match inside another column
                continue
            out.append({"date": dt.date.fromisoformat(r["date"]), "symbol": r["symbol"], "inst": r["inst"],
                        "expiry": dt.date.fromisoformat(r["expiry"]), "strike": num(r["strike"]), "kind": r["kind"],
                        "price": float(r["price"]), "oi": int(float(r["oi"] or 0)), "volume": int(float(r["volume"] or 0)),
                        "under": num(r["under"]), "lot": int(float(r["lot"])) if r["lot"] else None})
    return out


# ---------------------------------------------------------------- the store
@dataclass
class FetchResult:
    date: Optional[dt.date]
    status: str            # "cached" | "downloaded" | "imported" | "no_file" | "error"
    path: Optional[Path]
    message: str = ""


class _NotFound(Exception):
    pass


class _Blocked(Exception):
    pass


class BhavcopyStore:
    """A folder of canonical per-day files, plus the means to fill it (download or import)."""

    def __init__(self, root: str | Path, url_templates: Optional[list[str]] = None, timeout: float = 30.0, retries: int = 2,
                 pause: float = 0.7, warmup_url: Optional[str] = WARMUP_URL, include_index: bool = False):
        self.root = Path(root) / "fo"
        self.url_templates = url_templates or list(DEFAULT_URL_TEMPLATES)
        self.timeout, self.retries, self.pause, self.warmup_url = timeout, retries, pause, warmup_url
        self.include_index = include_index
        self._opener: Optional[urllib.request.OpenerDirector] = None
        self._symbols_cache: tuple[Optional[Path], float, list[str]] = (None, 0.0, [])

    # -- what is on disk
    def path_for(self, d: dt.date) -> Path:
        return self.root / f"{d.isoformat()}.csv.gz"

    def dates(self) -> list[dt.date]:
        out = []
        for p in self.root.glob("*.csv.gz"):
            try:
                out.append(dt.date.fromisoformat(p.name[:10]))
            except ValueError:
                continue
        return sorted(out)

    def coverage(self) -> dict[str, Any]:
        ds = self.dates()
        return {"days": len(ds), "first": ds[0].isoformat() if ds else None, "last": ds[-1].isoformat() if ds else None}

    def read_day(self, d: dt.date, symbols: Optional[set[str]] = None) -> list[dict[str, Any]]:
        return read_canonical(self.path_for(d), symbols)

    def symbols(self) -> list[str]:
        """Stocks that have options on the latest stored day."""
        ds = self.dates()
        if not ds:
            return []
        p = self.path_for(ds[-1])
        if self._symbols_cache[0] == p and self._symbols_cache[1] == p.stat().st_mtime:
            return self._symbols_cache[2]
        syms = sorted({r["symbol"] for r in read_canonical(p) if r["inst"] == "OPT"})
        self._symbols_cache = (p, p.stat().st_mtime, syms)
        return syms

    # -- filling it
    def urls_for(self, d: dt.date) -> list[str]:
        mon = d.strftime("%b").upper()
        return [t.format(yyyymmdd=d.strftime("%Y%m%d"), yyyy=d.year, MON=mon, dd=f"{d.day:02d}", mm=f"{d.month:02d}")
                for t in self.url_templates]

    def _get_opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            jar = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            if self.warmup_url:                       # NSE wants the cookies a browser gets from a normal page visit
                try:
                    self._opener.open(urllib.request.Request(self.warmup_url, headers=self._headers()), timeout=self.timeout).read(1024)
                except Exception:                     # noqa: BLE001 - the file request will say what is wrong
                    pass
        return self._opener

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/all-reports-derivatives"}

    def _download(self, url: str) -> bytes:
        op = self._get_opener()
        for attempt in range(self.retries + 1):
            try:
                with op.open(urllib.request.Request(url, headers=self._headers()), timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise _NotFound() from e
                if e.code in (401, 403):
                    raise _Blocked(f"NSE refused the download (HTTP {e.code}); it sometimes blocks scripts. Download the file in a browser "
                                   f"(nseindia.com > Market Data > All Reports > Derivatives) and import it instead.") from e
                if e.code >= 500 and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")

    def ingest(self, data: bytes, expect: Optional[dt.date] = None, colmap: Optional[dict[str, str]] = None) -> FetchResult:
        """Raw file bytes -> canonical day file. The trade date comes from the file itself."""
        rows = read_raw(data, colmap, None, self.include_index)
        d = Counter(r["date"] for r in rows).most_common(1)[0][0]
        if expect and d != expect:
            return FetchResult(expect, "error", None, f"the file is for {d}, not {expect}")
        rows = [r for r in rows if r["date"] == d]
        write_canonical(self.path_for(d), rows)
        return FetchResult(d, "downloaded", self.path_for(d), f"{len(rows):,} contracts")

    def fetch(self, d: dt.date, force: bool = False) -> FetchResult:
        p = self.path_for(d)
        if p.exists() and not force:
            return FetchResult(d, "cached", p)
        if d.weekday() >= 5:
            return FetchResult(d, "no_file", None, "weekend")
        why = ""
        for url in self.urls_for(d):
            try:
                data = self._download(url)
            except _NotFound:
                why = "not published at the expected address (holiday, or the address changed)"
                continue
            except _Blocked as e:
                return FetchResult(d, "error", None, str(e))
            except Exception as e:                    # noqa: BLE001
                why = f"{type(e).__name__}: {e}"
                continue
            try:
                return self.ingest(data, expect=d)
            except SchemaError as e:
                return FetchResult(d, "error", None, str(e))
        return FetchResult(d, "no_file", None, why or "no URL configured")

    def sync(self, start: dt.date, end: dt.date, progress: Optional[Callable[[FetchResult], None]] = None) -> list[FetchResult]:
        """Download every missing trading day in [start, end]. Weekends are skipped; holidays come back as no_file."""
        results, d = [], start
        while d <= end:
            if d.weekday() < 5:
                r = self.fetch(d)
                results.append(r)
                if progress:
                    progress(r)
                if r.status == "downloaded" and self.pause:
                    time.sleep(self.pause)
                if r.status == "error":
                    break                             # a block or a schema problem will repeat on every day
            d += dt.timedelta(days=1)
        return results

    def import_bytes(self, data: bytes, colmap: Optional[dict[str, str]] = None) -> FetchResult:
        try:
            r = self.ingest(data, None, colmap)
            return FetchResult(r.date, "imported", r.path, r.message)
        except SchemaError as e:
            return FetchResult(None, "error", None, str(e))

    def import_path(self, path: str | Path, colmap: Optional[dict[str, str]] = None) -> FetchResult:
        return self.import_bytes(Path(path).read_bytes(), colmap)


def describe_file(path: str | Path, colmap: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """How a real file maps onto the schema, without importing it. The check to run on a new download."""
    data = Path(path).read_bytes()
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv")))
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace"), newline=""))
    header = [h.strip() for h in (reader.fieldnames or []) if h]
    schema, missing = detect_schema(header, colmap)
    sample = list(normalise_rows((r for _, r in zip(range(200000), reader)), schema)) if not missing else []
    return {"header": header, "mapped": schema, "missing_required": missing,
            "unmapped_optional": [f for f in FIELD_ALIASES if f not in schema and f not in REQUIRED],
            "contracts_read": len(sample), "instruments_seen": sorted(Counter(r["inst"] for r in sample)),
            "symbols_seen": len({r["symbol"] for r in sample}), "dates_seen": sorted({r["date"].isoformat() for r in sample})[:3]}
