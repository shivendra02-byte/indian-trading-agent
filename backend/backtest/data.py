"""History backfill for backtests: extends stored candles backwards in time.

The live sync only moves forward from the last stored bar. Backtests need
older bars, so this fetches [start, first stored bar) per series, in chunks
within Kite's per-request limits, and stores them in the same candles table.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from typing import Callable

from backend.db import get_db
from backend.marketdata.candles import INTERVALS, REQUEST_GAP_SECONDS, _naive_ist, completed, save_candles
from backend.marketdata.instruments import fno_universe, spot_instrument
from tradingagents.utils.market_calendar import IST, NSE

HISTORY_DAILY_YEARS = 3
HISTORY_HOURLY_MONTHS = 12


def first_stored(token: int, interval: str) -> datetime | None:
    with get_db() as conn:
        row = conn.execute("SELECT MIN(ts) AS ts FROM candles WHERE instrument_token = ? AND interval = ?",
                           (int(token), interval)).fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def backfill_series(client, token: int, interval: str, start: datetime, now: datetime | None = None,
                    sleep: Callable[[float], None] = _time.sleep) -> int:
    now = _naive_ist(now or datetime.now(IST))
    end = first_stored(token, interval) or now
    if start >= end:
        return 0
    chunk = timedelta(days=INTERVALS[interval][1])
    saved, cursor = 0, start
    while cursor < end:
        stop = min(cursor + chunk, end)
        rows = client.historical_data(token, cursor, stop, interval, continuous=False, oi=False)
        saved += save_candles(token, interval, completed(rows or [], interval, NSE, now))
        cursor = stop
        sleep(REQUEST_GAP_SECONDS)
    return saved


def backfill_for_backtests(client, daily_years: float = HISTORY_DAILY_YEARS,
                           hourly_months: int = HISTORY_HOURLY_MONTHS,
                           progress: Callable[[str], None] | None = None) -> dict:
    """Daily and hourly spot history for every F&O stock and NIFTY.

    Daily: ``daily_years`` (the first ~300 sessions are indicator warm-up).
    Hourly: ``hourly_months``. About 2 requests per series per year at 3/s;
    ~200 series take roughly 5-10 minutes the first time, seconds afterwards."""
    now = _naive_ist(datetime.now(IST))
    daily_start = now - timedelta(days=int(daily_years * 365))
    hourly_start = now - timedelta(days=hourly_months * 31)
    tokens = []
    nifty = spot_instrument("NIFTY")
    if nifty:
        tokens.append(("NIFTY", nifty["instrument_token"]))
    tokens += [(s["symbol"], s["spot_token"]) for s in fno_universe() if s["spot_token"]]
    saved, errors = 0, []
    for i, (name, token) in enumerate(tokens, 1):
        try:
            saved += backfill_series(client, token, "day", daily_start, now)
            saved += backfill_series(client, token, "60minute", hourly_start, now)
        except Exception as exc:
            from backend.brokers.kite import KiteAuthExpired, _is_auth_error
            if _is_auth_error(exc):
                raise KiteAuthExpired("Kite session expired. Please connect Kite again.") from exc
            errors.append(f"{name}: {type(exc).__name__}")
        if progress:
            progress(f"history {i}/{len(tokens)}")
    return {"series": len(tokens), "candles_saved": saved, "errors": errors[:20]}
