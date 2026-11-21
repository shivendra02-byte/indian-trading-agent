"""Instrument master and the daily F&O universe, built from Kite's instrument dump.

Refreshed once per trading morning (before 09:15). Only the rows the scanner
needs are kept:

* NFO futures — every stock and index future (defines the F&O stock list)
* NFO options — scanned indices only (NIFTY, BANKNIFTY)
* NSE cash rows — spot equity for F&O stocks, plus the spot indices
* MCX futures — the mini contracts in ``MCX_UNDERLYINGS``
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from backend.db import get_db
from backend.marketdata.store import (
    INDEX_SPOT_SYMBOLS, INDEX_UNDERLYINGS, MCX_UNDERLYINGS, SCANNED_INDICES,
    ensure_marketdata_tables,
)
from tradingagents.utils.market_calendar import IST, is_trading_day

# A full NFO dump has several hundred futures. Fewer means Kite returned a
# partial or empty file; keep yesterday's master rather than wiping it.
MIN_NFO_FUTURES = 100
DEFAULT_ROLLOVER_DAYS = 3
BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"


class InstrumentMasterError(RuntimeError):
    """Raised when the instrument dump looks incomplete."""


def _today() -> date:
    return datetime.now(IST).date()


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value or None


def select_rows(nfo: Iterable[dict], nse: Iterable[dict], mcx: Iterable[dict]) -> list[dict]:
    """Pick the instrument rows the scanner needs from the raw Kite dumps."""
    nfo = list(nfo)
    futures = [r for r in nfo if r.get("segment") == "NFO-FUT"]
    if len(futures) < MIN_NFO_FUTURES:
        raise InstrumentMasterError(
            f"Kite returned only {len(futures)} NFO futures; keeping the previous instrument master"
        )
    stock_names = {r["name"] for r in futures if r.get("name") and r["name"] not in INDEX_UNDERLYINGS}
    spot_indices = {INDEX_SPOT_SYMBOLS[n] for n in SCANNED_INDICES if n in INDEX_SPOT_SYMBOLS}

    selected = list(futures)
    selected += [r for r in nfo if r.get("segment") == "NFO-OPT" and r.get("name") in SCANNED_INDICES]
    for r in nse:
        if r.get("segment") == "NSE" and r.get("instrument_type") == "EQ" and r.get("tradingsymbol") in stock_names:
            selected.append(r)
        elif r.get("segment") == "INDICES" and r.get("tradingsymbol") in spot_indices:
            selected.append(r)
    selected += [r for r in mcx if r.get("segment") == "MCX-FUT" and r.get("name") in MCX_UNDERLYINGS]
    return selected


def refresh_instruments(client) -> dict:
    """Download today's instrument dump from Kite and replace the master."""
    ensure_marketdata_tables()
    rows = select_rows(client.instruments("NFO"), client.instruments("NSE"), client.instruments("MCX"))
    today = _today().isoformat()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM instruments")
        conn.executemany(
            """INSERT OR REPLACE INTO instruments
               (instrument_token, exchange, segment, tradingsymbol, name, instrument_type,
                expiry, strike, lot_size, tick_size, refreshed_on)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    int(r["instrument_token"]), r.get("exchange") or "", r.get("segment") or "",
                    r.get("tradingsymbol") or "", r.get("name") or r.get("tradingsymbol"),
                    r.get("instrument_type"), _iso(r.get("expiry")), float(r.get("strike") or 0),
                    int(r.get("lot_size") or 0), float(r.get("tick_size") or 0), today,
                )
                for r in rows
            ],
        )
    return instrument_summary()


def instrument_summary() -> dict:
    ensure_marketdata_tables()
    with get_db() as conn:
        counts = {
            row["segment"]: row["n"]
            for row in conn.execute("SELECT segment, COUNT(*) AS n FROM instruments GROUP BY segment")
        }
        refreshed = conn.execute("SELECT MAX(refreshed_on) AS d FROM instruments").fetchone()["d"]
    return {
        "refreshed_on": refreshed,
        "fresh_today": refreshed == _today().isoformat(),
        "counts": counts,
        "fno_stocks": len(fno_stock_names()),
    }


def fno_stock_names() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT name FROM instruments WHERE segment = 'NFO-FUT'
                AND name NOT IN ({",".join("?" * len(INDEX_UNDERLYINGS))}) ORDER BY name""",
            INDEX_UNDERLYINGS,
        ).fetchall()
    return [r["name"] for r in rows]


def trading_days_until(expiry: date, today: date) -> int:
    """Trading days from ``today`` (exclusive) to ``expiry`` (inclusive)."""
    count, d = 0, today
    while d < expiry:
        d += timedelta(days=1)
        if is_trading_day(d):
            count += 1
    return count


def futures_for(name: str, segment: str = "NFO-FUT", today: date | None = None) -> list[dict]:
    """Unexpired futures for an underlying, nearest expiry first."""
    today = today or _today()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM instruments WHERE segment = ? AND name = ? AND expiry >= ?
               ORDER BY expiry""",
            (segment, name, today.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def active_future(name: str, segment: str = "NFO-FUT", today: date | None = None,
                  rollover_days: int = DEFAULT_ROLLOVER_DAYS) -> dict | None:
    """The contract to trade: nearest expiry, rolled to the next one when the
    nearest has ``rollover_days`` trading days or fewer left."""
    today = today or _today()
    chain = futures_for(name, segment, today)
    if not chain:
        return None
    near = chain[0]
    if len(chain) > 1 and trading_days_until(date.fromisoformat(near["expiry"]), today) <= rollover_days:
        return chain[1]
    return near


def spot_instrument(name: str) -> dict | None:
    """NSE spot row for an F&O stock or a scanned index."""
    symbol = INDEX_SPOT_SYMBOLS.get(name, name)
    segment = "INDICES" if name in INDEX_SPOT_SYMBOLS else "NSE"
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM instruments WHERE segment = ? AND tradingsymbol = ?", (segment, symbol)
        ).fetchone()
    return dict(row) if row else None


def option_expiries(name: str, today: date | None = None) -> list[str]:
    today = today or _today()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT expiry FROM instruments WHERE segment = 'NFO-OPT' AND name = ?
               AND expiry >= ? ORDER BY expiry""",
            (name, today.isoformat()),
        ).fetchall()
    return [r["expiry"] for r in rows]


# --- F&O ban list --------------------------------------------------------------

def parse_ban_list(text: str) -> tuple[str | None, list[str]]:
    """Parse NSE's fo_secban.csv: a 'Securities in Ban For Trade Date DD-MMM-YYYY'
    header followed by 'n,SYMBOL' rows."""
    trade_date = None
    match = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})", text.splitlines()[0] if text else "")
    if match:
        trade_date = datetime.strptime(match.group(1), "%d-%b-%Y").date().isoformat()
    symbols = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().isdigit() and row[1].strip():
            symbols.append(row[1].strip().upper())
    return trade_date, symbols


def refresh_ban_list(fetch=None) -> dict:
    """Fetch today's F&O ban list. NSE often blocks cloud IPs; on failure the
    status is 'unknown' and stock-future signals must not open paper trades."""
    ensure_marketdata_tables()
    try:
        if fetch is None:
            request = urllib.request.Request(BAN_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https URL
                text = response.read(200_000).decode("utf-8", "replace")
        else:
            text = fetch()
        trade_date, symbols = parse_ban_list(text)
    except Exception as exc:  # network/parse failures must not break the morning refresh
        return {"status": "unknown", "error": type(exc).__name__}
    if not trade_date:
        return {"status": "unknown", "error": "unrecognised_format"}
    with get_db() as conn:
        conn.execute("DELETE FROM fo_ban_list WHERE trade_date = ?", (trade_date,))
        conn.executemany(
            "INSERT OR IGNORE INTO fo_ban_list (trade_date, symbol) VALUES (?, ?)",
            [(trade_date, s) for s in (symbols or ["__NONE__"])],
        )
    return {"status": "ok", "trade_date": trade_date, "symbols": symbols}


def ban_status(today: date | None = None) -> dict:
    """Today's ban list, or status 'unknown' if it was not fetched for today."""
    today = (today or _today()).isoformat()
    with get_db() as conn:
        rows = conn.execute("SELECT symbol FROM fo_ban_list WHERE trade_date = ?", (today,)).fetchall()
        known = conn.execute("SELECT 1 FROM fo_ban_list WHERE trade_date = ? LIMIT 1", (today,)).fetchone()
    # An empty ban list is valid but leaves no row; refresh_ban_list records that
    # case with a placeholder so "known and empty" differs from "never fetched".
    symbols = sorted(r["symbol"] for r in rows if r["symbol"] != "__NONE__")
    return {"status": "ok" if known else "unknown", "trade_date": today, "symbols": symbols}


def fno_universe(today: date | None = None) -> list[dict]:
    """Stocks to scan: every F&O stock with its spot token, active future and ban flag."""
    ban = ban_status(today)
    banned = set(ban["symbols"])
    out = []
    for name in fno_stock_names():
        spot = spot_instrument(name)
        fut = active_future(name, today=today)
        out.append({
            "symbol": name,
            "spot_token": spot["instrument_token"] if spot else None,
            "future_token": fut["instrument_token"] if fut else None,
            "future_symbol": fut["tradingsymbol"] if fut else None,
            "expiry": fut["expiry"] if fut else None,
            "lot_size": fut["lot_size"] if fut else None,
            "ban": "unknown" if ban["status"] != "ok" else (name in banned),
        })
    return out
