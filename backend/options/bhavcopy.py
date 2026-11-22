"""Exchange F&O bhavcopy files (UDiFF format): download, parse, add IV/Greeks.

NSE: ``BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip`` (NIFTY, BANKNIFTY).
BSE: ``BhavCopy_BSE_FO_0_0_0_<YYYYMMDD>_F_0000.CSV`` (SENSEX).
Both use the SEBI UDiFF columns (TckrSymb, FinInstrmTp, XpryDt, StrkPric,
OptnTp, ClsPric, SttlmPric, UndrlygPric, OpnIntrst, TtlTradgVol, NewBrdLotQty ...).
Only index options and index futures of the configured symbols are kept.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime

from backend.options import black76

MAX_FILE_BYTES = 200_000_000
OPTION_TYPES = {"IDO", "STO", "OPTIDX", "OPTSTK"}
FUTURE_TYPES = {"IDF", "STF", "FUTIDX", "FUTSTK"}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d %b %Y", "%d-%B-%Y")


class BhavcopyFormatError(ValueError):
    """The file is not a UDiFF F&O bhavcopy we can read."""


def file_url(template: str, d: date) -> str:
    return template.format(yyyymmdd=d.strftime("%Y%m%d"), ddmmyyyy=d.strftime("%d%m%Y"))


def download(url: str, timeout: int = 60) -> bytes | None:
    """File bytes, or None when the exchange has no file for that day (404)."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URL from config
            data = response.read(MAX_FILE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(data) > MAX_FILE_BYTES:
        raise BhavcopyFormatError("file too large")
    return data


def to_text(data: bytes) -> str:
    """CSV text from a zip (first .csv inside) or a plain CSV file."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
            if not names:
                raise BhavcopyFormatError("empty zip")
            data = zf.read(names[0])
            if len(data) > MAX_FILE_BYTES:
                raise BhavcopyFormatError("file too large")
    return data.decode("utf-8-sig", "replace")


def parse_date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _num(value) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if text in ("", "-", "NA", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse(text: str, symbols: list[str]) -> tuple[date | None, list[dict]]:
    """(trade date, rows) for index options and futures of ``symbols``."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BhavcopyFormatError("no header")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    for needed in ("tckrsymb", "xprydt", "clspric"):
        if needed not in fields:
            raise BhavcopyFormatError(f"missing column {needed}")

    def col(row, name):
        key = fields.get(name.lower())
        return row.get(key) if key else None

    wanted = {s.upper() for s in symbols}
    trade_date = None
    rows = []
    for raw in reader:
        symbol = str(col(raw, "TckrSymb") or "").strip().upper()
        if symbol not in wanted:
            continue
        inst = str(col(raw, "FinInstrmTp") or "").strip().upper()
        opt = str(col(raw, "OptnTp") or "").strip().upper()
        if inst in OPTION_TYPES or opt in ("CE", "PE"):
            if opt not in ("CE", "PE"):
                continue
            kind = opt
        elif inst in FUTURE_TYPES or not opt or opt == "XX":
            kind = "FUT"
        else:
            continue
        expiry = parse_date(col(raw, "FininstrmActlXpryDt")) or parse_date(col(raw, "XpryDt"))
        traded = parse_date(col(raw, "TradDt")) or parse_date(col(raw, "BizDt"))
        if not expiry:
            continue
        trade_date = trade_date or traded
        close = _num(col(raw, "ClsPric"))
        rows.append({
            "symbol": symbol,
            "expiry": expiry.isoformat(),
            "strike": (_num(col(raw, "StrkPric")) or 0.0) if kind != "FUT" else 0.0,
            "opt_type": kind,
            "open": _num(col(raw, "OpnPric")),
            "high": _num(col(raw, "HghPric")),
            "low": _num(col(raw, "LwPric")),
            "close": close,
            "settle": _num(col(raw, "SttlmPric")),
            "prev_close": _num(col(raw, "PrvsClsgPric")),
            "underlying": _num(col(raw, "UndrlygPric")),
            "oi": _num(col(raw, "OpnIntrst")),
            "chg_oi": _num(col(raw, "ChngInOpnIntrst")),
            "volume": _num(col(raw, "TtlTradgVol")),
            "turnover": _num(col(raw, "TtlTrfVal")),
            "lot_size": _num(col(raw, "NewBrdLotQty")),
        })
    return trade_date, rows


def _premium(row: dict) -> float | None:
    value = row.get("close") or row.get("settle")
    return value if value and value > 0 else None


def forward_for(chain: list[dict], future: dict | None, T: float, r: float) -> tuple[float | None, str]:
    """Forward for one expiry: put-call parity at the strike where call and put
    are closest (both traded if possible), else the same-expiry future, else spot."""
    by_strike: dict[float, dict] = defaultdict(dict)
    for row in chain:
        by_strike[row["strike"]][row["opt_type"]] = row
    best = None
    for traded_only in (True, False):
        for strike, pair in by_strike.items():
            call, put = pair.get("CE"), pair.get("PE")
            if not call or not put:
                continue
            c, p = _premium(call), _premium(put)
            if not c or not p:
                continue
            if traded_only and not ((call.get("volume") or 0) > 0 and (put.get("volume") or 0) > 0):
                continue
            gap = abs(c - p)
            if best is None or gap < best[0]:
                best = (gap, strike, c, p)
        if best:
            break
    if best and T > 0:
        return black76.forward_from_parity(best[2], best[3], best[1], T, r), "parity"
    if future and _premium(future):
        return _premium(future), "future"
    spot = next((row["underlying"] for row in chain if row.get("underlying")), None)
    return spot, "spot"


def enrich(rows: list[dict], trade_date: date, r: float) -> list[dict]:
    """Add days to expiry, forward, implied volatility and Greeks to option rows."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    futures: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["symbol"], row["expiry"])
        if row["opt_type"] == "FUT":
            futures[key] = row
        else:
            groups[key].append(row)
    for key, chain in groups.items():
        days = (date.fromisoformat(key[1]) - trade_date).days
        T = max(days, 0) / 365.0
        forward, source = forward_for(chain, futures.get(key), T, r)
        for row in chain:
            row.update({"days_to_expiry": days, "forward": forward, "forward_source": source,
                        "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None})
            premium = _premium(row)
            if not forward or not premium or T <= 0 or row["strike"] <= 0:
                continue
            iv = black76.implied_vol(premium, forward, row["strike"], T, r, row["opt_type"])
            if iv is None:
                continue
            row["iv"] = iv
            row.update(black76.greeks(forward, row["strike"], T, r, iv, row["opt_type"]))
    for key, row in futures.items():
        row.update({"days_to_expiry": (date.fromisoformat(key[1]) - trade_date).days, "forward": row.get("close"),
                    "forward_source": "future", "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None})
    return rows
