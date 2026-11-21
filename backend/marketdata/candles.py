"""Kite historical candles, cached in SQLite, plus 4H resampling.

Only completed candles are stored: a bar is kept once its end (bar start +
interval, capped at the session close) has passed. Each sync re-reads from
the last stored bar, so a restart or a missed hour just catches up.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from backend.db import get_db
from backend.marketdata.store import ensure_marketdata_tables
from tradingagents.utils.market_calendar import IST, MCX, NSE, session_windows

# Interval -> (bar length, max days per Kite request; kept under Kite's limits).
INTERVALS: dict[str, tuple[timedelta, int]] = {
    "10minute": (timedelta(minutes=10), 90),
    "60minute": (timedelta(hours=1), 365),
    "day": (timedelta(days=1), 1800),
}
# Kite allows 3 historical requests per second.
REQUEST_GAP_SECONDS = 0.35
SESSION_ANCHOR = {NSE: (9, 15), MCX: (9, 0)}


def _naive_ist(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    return value


def candle_end(start: datetime, interval: str, exchange: str) -> datetime:
    """When a bar is complete: start + length, capped at its session's close."""
    if interval == "day":
        windows = session_windows(exchange, start.date())
        close = windows[-1][1] if windows else datetime.max.time()
        return datetime.combine(start.date(), close)
    end = start + INTERVALS[interval][0]
    for open_t, close_t in session_windows(exchange, start.date()):
        if open_t <= start.time() < close_t:
            return min(end, datetime.combine(start.date(), close_t))
    return end


def completed(rows: Iterable[dict], interval: str, exchange: str, now: datetime) -> list[dict]:
    now = _naive_ist(now)
    return [r for r in rows if candle_end(_naive_ist(r["date"]), interval, exchange) <= now]


def save_candles(token: int, interval: str, rows: Iterable[dict]) -> int:
    values = [
        (
            int(token), interval, _naive_ist(r["date"]).isoformat(timespec="seconds"),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            int(r.get("volume") or 0), int(r["oi"]) if r.get("oi") is not None else None,
        )
        for r in rows
    ]
    if values:
        with get_db() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO candles
                   (instrument_token, interval, ts, open, high, low, close, volume, oi)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
    return len(values)


def last_stored(token: int, interval: str) -> datetime | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM candles WHERE instrument_token = ? AND interval = ?",
            (int(token), interval),
        ).fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def sync_candles(client, token: int, interval: str, exchange: str, lookback_days: int,
                 now: datetime | None = None, continuous: bool = False, oi: bool = False,
                 sleep: Callable[[float], None] = _time.sleep) -> int:
    """Fetch missing completed candles for one instrument. Returns rows saved."""
    if interval not in INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    ensure_marketdata_tables()
    now = _naive_ist(now or datetime.now(IST))
    last = last_stored(token, interval)
    start = last if last else now - timedelta(days=lookback_days)
    chunk = timedelta(days=INTERVALS[interval][1])
    saved = 0
    while start < now:
        stop = min(start + chunk, now)
        rows = client.historical_data(token, start, stop, interval, continuous=continuous, oi=oi)
        saved += save_candles(token, interval, completed(rows or [], interval, exchange, now))
        start = stop
        sleep(REQUEST_GAP_SECONDS)
    return saved


def get_candles(token: int, interval: str, since: datetime | None = None,
                limit: int | None = None) -> list[dict]:
    """Stored candles, oldest first. ``limit`` keeps the most recent N."""
    query = "SELECT ts, open, high, low, close, volume, oi FROM candles WHERE instrument_token = ? AND interval = ?"
    params: list[Any] = [int(token), interval]
    if since:
        query += " AND ts >= ?"
        params.append(_naive_ist(since).isoformat(timespec="seconds"))
    query += " ORDER BY ts DESC"
    if limit:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(query, params)]
    rows.reverse()
    return rows


def resample_4h(rows: list[dict], exchange: str) -> list[dict]:
    """Group hourly bars into session-anchored 4-hour bars.

    NSE: 09:15–13:15 and 13:15–15:30. MCX: 09:00–13:00, 13:00–17:00,
    17:00–21:00, 21:00–close. Bars never span two days.
    """
    anchor_h, anchor_m = SESSION_ANCHOR[exchange.upper()]
    buckets: dict[tuple[str, int], dict] = {}
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        anchor = ts.replace(hour=anchor_h, minute=anchor_m, second=0)
        key = (ts.date().isoformat(), int((ts - anchor).total_seconds() // (4 * 3600)))
        b = buckets.get(key)
        if b is None:
            buckets[key] = {
                "ts": (anchor + timedelta(hours=4 * key[1])).isoformat(timespec="seconds"),
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "volume": r.get("volume") or 0, "oi": r.get("oi"),
            }
        else:
            b["high"] = max(b["high"], r["high"])
            b["low"] = min(b["low"], r["low"])
            b["close"] = r["close"]
            b["volume"] += r.get("volume") or 0
            b["oi"] = r.get("oi")
    return [buckets[k] for k in sorted(buckets)]
