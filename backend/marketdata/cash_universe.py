"""Cash-strategy universe: F&O stocks + the NIFTY 500 (agreed 26 Nov 2026).

The futures strategies trade F&O stocks only. The daily cash strategies (cash swing, VCP, cash
reversal) only need daily bars, so they also scan the NIFTY 500:

* ``refresh_nifty500()`` — NSE's NIFTY 500 constituent list (CSV: Company Name, Industry, Symbol,
  Series, ISIN Code), downloaded at most once every ``refresh_days`` (28) into
  ``index_constituents``. A list with fewer than ``min_symbols`` rows is rejected and the old
  list kept. ``import_nifty500(text)`` stores an uploaded copy (NSE often blocks cloud servers).
* ``refresh_surveillance()`` — NSE's daily surveillance file (``REG1_IND<ddmmyy>.csv``: GSM and
  ASM stage per symbol, 100 = not under the measure). Stocks on a GSM/ASM stage are skipped by
  the cash scans. If the file can't be fetched the status is ``unknown`` and nothing is skipped
  (the candidates say so); ``import_surveillance(text)`` stores an uploaded copy.
* ``cash_universe()`` — F&O stocks (``fno: True``) plus NIFTY 500 stocks that are not in F&O and
  have an NSE spot instrument (``fno: False``), with their surveillance flag.
* ``min_turnover_for()`` — non-F&O stocks use ``min_turnover_cr_non_fno`` (₹20 cr).

Settings: ``instruments.cash_swing.cash_universe`` in ``config/decision_engine.yaml``.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db

log = logging.getLogger(__name__)

DEFAULTS = {
    "nifty500": True,
    "min_turnover_cr_non_fno": 20,
    "refresh_days": 28,
    "min_symbols": 400,
    "nifty500_urls": [
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ],
    "skip_surveillance": True,
    # not verifiable from the build sandbox; an uploaded file works the same way
    "surveillance_url": "https://nsearchives.nseindia.com/content/cm/REG1_IND{ddmmyy}.csv",
    "surveillance_max_age_days": 4,
}
INDEX = "NIFTY 500"


def settings() -> dict:
    from backend.decision.config import get_config
    inst = get_config().instruments.get("cash_swing")
    extra = ((inst.model_extra or {}).get("cash_universe") if inst else None) or {}
    return {**DEFAULTS, **extra}


def ensure_tables() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS index_constituents (
            index_name TEXT NOT NULL, symbol TEXT NOT NULL, company TEXT, industry TEXT, series TEXT,
            isin TEXT, refreshed_on TEXT NOT NULL, PRIMARY KEY (index_name, symbol))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS surveillance_list (
            trade_date TEXT NOT NULL, symbol TEXT NOT NULL, measures TEXT,
            PRIMARY KEY (trade_date, symbol))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS surveillance_files (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, flagged INTEGER, fetched_at TEXT, source TEXT)""")


def _download(url: str, limit: int = 3_000_000) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https URLs from the config
        return response.read(limit).decode("utf-8-sig", "replace")


# --- NIFTY 500 list -----------------------------------------------------------------------------

def parse_index_csv(text: str) -> list[dict]:
    """Rows of an NSE index constituent CSV (Company Name, Industry, Symbol, Series, ISIN Code)."""
    rows = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for r in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        symbol = r.get("symbol", "").upper()
        if not symbol or " " in symbol:
            continue
        rows.append({"symbol": symbol, "company": r.get("company name"), "industry": r.get("industry"),
                     "series": r.get("series") or "EQ", "isin": r.get("isin code")})
    return rows


def _store_list(rows: list[dict], today: date) -> None:
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM index_constituents WHERE index_name = ?", (INDEX,))
        conn.executemany(
            "INSERT OR REPLACE INTO index_constituents (index_name, symbol, company, industry, series, isin, refreshed_on) "
            "VALUES (?,?,?,?,?,?,?)",
            [(INDEX, r["symbol"], r["company"], r["industry"], r["series"], r["isin"], today.isoformat()) for r in rows])


def list_status() -> dict:
    ensure_tables()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n, MAX(refreshed_on) AS d FROM index_constituents WHERE index_name = ?",
                           (INDEX,)).fetchone()
    return {"index": INDEX, "symbols": row["n"], "refreshed_on": row["d"]}


def refresh_nifty500(fetch: Callable[[str], str] | None = None, force: bool = False,
                     today: date | None = None) -> dict:
    """Download the NIFTY 500 list if the stored one is older than ``refresh_days`` (or ``force``)."""
    ensure_tables()
    cfg = settings()
    today = today or date.today()
    if not cfg["nifty500"]:
        return {"status": "off"}
    st = list_status()
    if not force and st["refreshed_on"] and st["symbols"] >= cfg["min_symbols"] and \
            (today - date.fromisoformat(st["refreshed_on"])).days < int(cfg["refresh_days"]):
        return {"status": "fresh", **st}
    errors = []
    for url in cfg["nifty500_urls"]:
        try:
            rows = parse_index_csv((fetch or _download)(url))
        except Exception as exc:  # network: try the next URL, keep the old list
            errors.append(f"{url.split('/')[2]}: {type(exc).__name__}")
            continue
        if len(rows) < int(cfg["min_symbols"]):
            errors.append(f"{url.split('/')[2]}: only {len(rows)} symbols")
            continue
        _store_list(rows, today)
        return {"status": "ok", "source": url, **list_status()}
    return {"status": "error", "errors": errors, **st}


def import_nifty500(text: str, today: date | None = None) -> dict:
    """Store an uploaded NIFTY 500 CSV (from niftyindices.com) when the download is blocked."""
    ensure_tables()
    rows = parse_index_csv(text)
    if len(rows) < int(settings()["min_symbols"]):
        raise ValueError(f"only {len(rows)} symbols in the file; expected the NIFTY 500 list")
    _store_list(rows, today or date.today())
    return {"status": "ok", "source": "upload", **list_status()}


def nifty500_symbols() -> list[str]:
    ensure_tables()
    with get_db() as conn:
        return [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM index_constituents WHERE index_name = ? ORDER BY symbol", (INDEX,))]


# --- ASM / GSM surveillance ---------------------------------------------------------------------

def parse_surveillance(text: str) -> dict[str, list[str]]:
    """{symbol: [measures]} for symbols under any GSM or ASM stage. NSE's REG1_IND file has one row
    per symbol and stage columns where 100 means "not under this measure"."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [f for f in (reader.fieldnames or [])]
    sym_col = next((f for f in fields if f.strip().lower() == "symbol"), None)
    if not sym_col:
        raise ValueError("no Symbol column")
    measure_cols = [f for f in fields if any(k in f.lower().replace("_", " ") for k in
                                             ("gsm", "asm", "additional surveillance", "graded surveillance"))]
    if not measure_cols:
        raise ValueError("no GSM/ASM columns")
    out: dict[str, list[str]] = {}
    for r in reader:
        symbol = (r.get(sym_col) or "").strip().upper()
        if not symbol:
            continue
        hits = []
        for col in measure_cols:
            v = (r.get(col) or "").strip()
            if v and v not in ("100", "-", "NA", "N.A."):
                hits.append(f"{col.strip()} {v}")
        if hits:
            out[symbol] = hits
    return out


def _store_surveillance(trade_date: date, flagged: dict[str, list[str]], source: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM surveillance_list WHERE trade_date = ?", (trade_date.isoformat(),))
        conn.executemany("INSERT INTO surveillance_list (trade_date, symbol, measures) VALUES (?,?,?)",
                         [(trade_date.isoformat(), s, "; ".join(m)) for s, m in flagged.items()])
        conn.execute("INSERT OR REPLACE INTO surveillance_files (trade_date, status, flagged, fetched_at, source) "
                     "VALUES (?,?,?,?,?)", (trade_date.isoformat(), "ok", len(flagged),
                                           datetime.now().isoformat(timespec="seconds"), source))


def refresh_surveillance(fetch: Callable[[str], str] | None = None, today: date | None = None) -> dict:
    """Today's (or the last few days') NSE surveillance file. Failure leaves the status unknown."""
    ensure_tables()
    cfg = settings()
    today = today or date.today()
    errors = []
    for back in range(0, 4):
        d = today - timedelta(days=back)
        url = cfg["surveillance_url"].format(ddmmyy=d.strftime("%d%m%y"), ddmmyyyy=d.strftime("%d%m%Y"))
        try:
            flagged = parse_surveillance((fetch or _download)(url))
        except Exception as exc:
            errors.append(f"{d.isoformat()}: {type(exc).__name__}")
            continue
        _store_surveillance(d, flagged, url)
        return {"status": "ok", "trade_date": d.isoformat(), "flagged": len(flagged)}
    return {"status": "unknown", "errors": errors}


def import_surveillance(text: str, trade_date: date | None = None) -> dict:
    ensure_tables()
    flagged = parse_surveillance(text)
    d = trade_date or date.today()
    _store_surveillance(d, flagged, "upload")
    return {"status": "ok", "trade_date": d.isoformat(), "flagged": len(flagged)}


def surveillance_status(today: date | None = None) -> dict:
    """Flagged symbols from the newest stored file no older than ``surveillance_max_age_days``."""
    ensure_tables()
    today = today or date.today()
    oldest = (today - timedelta(days=int(settings()["surveillance_max_age_days"]))).isoformat()
    with get_db() as conn:
        f = conn.execute("SELECT trade_date FROM surveillance_files WHERE status = 'ok' AND trade_date >= ? "
                         "AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1", (oldest, today.isoformat())).fetchone()
        if not f:
            return {"status": "unknown", "symbols": {}}
        rows = conn.execute("SELECT symbol, measures FROM surveillance_list WHERE trade_date = ?",
                            (f["trade_date"],)).fetchall()
    return {"status": "ok", "trade_date": f["trade_date"], "symbols": {r["symbol"]: r["measures"] for r in rows}}


# --- the universe -------------------------------------------------------------------------------

def cash_universe(today: date | None = None) -> list[dict]:
    """F&O stocks plus the NIFTY 500 stocks outside F&O, each with ``fno`` and ``surveillance``."""
    from backend.marketdata.instruments import fno_universe, spot_instrument
    cfg = settings()
    surv = surveillance_status(today) if cfg["skip_surveillance"] else {"status": "off", "symbols": {}}
    flags = surv["symbols"]
    out = []
    seen = set()
    for s in fno_universe(today):
        seen.add(s["symbol"])
        out.append({**s, "fno": True, "surveillance": flags.get(s["symbol"]),
                    "surveillance_status": surv["status"]})
    if cfg["nifty500"]:
        for sym in nifty500_symbols():
            if sym in seen:
                continue
            spot = spot_instrument(sym)
            if not spot:
                continue
            out.append({"symbol": sym, "spot_token": spot["instrument_token"], "future_token": None,
                        "fno": False, "surveillance": flags.get(sym), "surveillance_status": surv["status"]})
    return out


def min_turnover_for(stock: dict, default_rupees: float) -> float:
    """₹ turnover floor: the strategy's own floor for F&O stocks, ``min_turnover_cr_non_fno`` for the rest."""
    if stock.get("fno", True):
        return default_rupees
    return float(settings()["min_turnover_cr_non_fno"]) * 1e7


def summary(today: date | None = None) -> dict:
    u = cash_universe(today)
    return {**list_status(), "universe": len(u), "fno": sum(1 for s in u if s["fno"]),
            "nifty500_extra": sum(1 for s in u if not s["fno"]),
            "surveillance": {k: v for k, v in surveillance_status(today).items() if k != "symbols"},
            "skipped_surveillance": sorted(s["symbol"] for s in u if s.get("surveillance"))}
