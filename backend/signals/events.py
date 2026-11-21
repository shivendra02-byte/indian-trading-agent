"""Verified event calendar for the hard event filter. Fails closed.

Market-wide dates below are taken from official sources (RBI press release of
23 Mar 2026 for the 2026-27 MPC schedule; the Federal Reserve FOMC calendar,
incl. the revised 2027 schedule of 12 Nov 2026). Any window that runs past
``VERIFIED_THROUGH`` is treated as unverifiable → no trade.

Company results come from Yahoo Finance (via yfinance), cached per symbol.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db
from tradingagents.utils.market_calendar import IST, is_trading_day

VERIFIED_THROUGH = date(2027, 3, 31)

# RBI MPC decision days (last day of each meeting).
RBI_DECISIONS = [date(2026, 2, 6), date(2026, 4, 8), date(2026, 6, 5), date(2026, 8, 5),
                 date(2026, 10, 7), date(2026, 12, 4), date(2027, 2, 5)]
UNION_BUDGET = [date(2026, 2, 1), date(2027, 2, 1)]
# FOMC decisions land after the Indian close (~00:00 IST), so the Indian
# market reacts the next day; these are the US decision dates.
FOMC_DECISIONS = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
                  date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
                  date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28), date(2027, 6, 9)]

RESULTS_RECHECK_DAYS = 7
# Indian companies report quarterly; a result within the last 45 days means the
# next one is roughly 2 months away, which clears a 7-trading-day window.
RECENT_RESULT_DAYS = 45


def _today() -> date:
    return datetime.now(IST).date()


def trading_days_after(start: date, n: int) -> list[date]:
    """The next ``n`` NSE trading days after ``start``."""
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


def market_events_between(first: date, last: date, kinds=("RBI", "BUDGET")) -> dict:
    """Market-wide events in [first, last]. ``verified`` is False past VERIFIED_THROUGH."""
    events = []
    if "RBI" in kinds:
        events += [{"date": d.isoformat(), "event": "RBI policy"} for d in RBI_DECISIONS if first <= d <= last]
    if "BUDGET" in kinds:
        events += [{"date": d.isoformat(), "event": "Union Budget"} for d in UNION_BUDGET if first <= d <= last]
    if "FOMC" in kinds:
        events += [{"date": (d + timedelta(days=1)).isoformat(), "event": "US FOMC decision"}
                   for d in FOMC_DECISIONS if first <= d + timedelta(days=1) <= last]
    return {"verified": last <= VERIFIED_THROUGH, "events": sorted(events, key=lambda e: e["date"])}


# --- company results -------------------------------------------------------------

def ensure_results_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results_dates (
                symbol TEXT PRIMARY KEY,
                next_date TEXT,
                last_date TEXT,
                checked_on TEXT NOT NULL
            )
        """)


def _yahoo_results_date(symbol: str) -> date | None:
    from backend.calendar_data import fetch_earnings_for_ticker
    found = fetch_earnings_for_ticker(symbol)
    if not found:
        return None
    try:
        return date.fromisoformat(found["event_date"][:10])
    except ValueError:
        return None


def refresh_results(symbols: list[str], fetch: Callable[[str], date | None] | None = None,
                    today: date | None = None) -> dict:
    """Re-check symbols not checked in RESULTS_RECHECK_DAYS, or with results coming soon."""
    ensure_results_table()
    fetch = fetch or _yahoo_results_date
    today = today or _today()
    with get_db() as conn:
        rows = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM results_dates")}
    due = []
    for symbol in symbols:
        row = rows.get(symbol)
        if row:
            age = (today - date.fromisoformat(row["checked_on"])).days
            soon = row["next_date"] and date.fromisoformat(row["next_date"]) <= today + timedelta(days=21)
            if age < RESULTS_RECHECK_DAYS and not soon:
                continue
        due.append(symbol)

    def safe_fetch(symbol):
        try:
            return symbol, fetch(symbol)
        except Exception:
            return symbol, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        found_all = list(pool.map(safe_fetch, due))
    for symbol, found in found_all:
        row = rows.get(symbol)
        next_date = found.isoformat() if found and found >= today else None
        last_date = found.isoformat() if found and found < today else (row or {}).get("last_date")
        if row and not next_date and row.get("next_date") and date.fromisoformat(row["next_date"]) < today:
            last_date = row["next_date"]  # the date we knew about has passed
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO results_dates (symbol, next_date, last_date, checked_on) VALUES (?, ?, ?, ?)",
                (symbol, next_date, last_date, today.isoformat()),
            )
    checked = len(due)
    return {"checked": checked}


def results_clear(symbol: str, first: date, last: date, today: date | None = None) -> tuple[bool, str]:
    """True if the symbol verifiably has no results in [first, last]."""
    ensure_results_table()
    today = today or _today()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM results_dates WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return False, "results date not checked"
    if (today - date.fromisoformat(row["checked_on"])).days > RESULTS_RECHECK_DAYS:
        return False, "results date check is stale"
    if row["next_date"]:
        nd = date.fromisoformat(row["next_date"])
        if first <= nd <= last:
            return False, f"results on {nd.isoformat()} inside the holding window"
        if nd > last:
            return True, ""
    if row["last_date"] and (today - date.fromisoformat(row["last_date"])).days <= RECENT_RESULT_DAYS:
        return True, ""
    return False, "results date unverified"
