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
HISTORY_10MIN_MONTHS = 24        # ORB: the most liquid stocks + NIFTY only

# Live progress of the history download, read by GET /api/rule-backtests/history.
PROGRESS: dict = {"status": "idle"}


def _set_progress(**fields) -> None:
    PROGRESS.update(fields)


def progress_snapshot() -> dict:
    """Progress with elapsed time and a rough time left."""
    out = dict(PROGRESS)
    started = out.get("started_at")
    if started and out.get("status") == "running":
        elapsed = (datetime.now(IST).replace(tzinfo=None) - datetime.fromisoformat(started)).total_seconds()
        out["elapsed_s"] = int(elapsed)
        done, total = out.get("done") or 0, out.get("total") or 0
        out["eta_s"] = int(elapsed / done * (total - done)) if done and total else None
    return out


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
    """Daily and hourly spot history for every F&O stock and NIFTY; daily only for the NIFTY 500
    stocks outside F&O (cash backtests).

    Daily: ``daily_years`` (the first ~300 sessions are indicator warm-up).
    Hourly: ``hourly_months``. 10-minute: 24 months for the ORB stocks and NIFTY
    (the most liquid stocks by today's turnover). About 2 requests per series per year at 3/s;
    ~200 series take roughly 5-10 minutes the first time, seconds afterwards."""
    now = _naive_ist(datetime.now(IST))
    daily_start = now - timedelta(days=int(daily_years * 365))
    hourly_start = now - timedelta(days=hourly_months * 31)
    tokens = []
    for index in ("NIFTY", "BANKNIFTY"):          # BANKNIFTY for the index futures backtest
        inst = spot_instrument(index)
        if inst:
            tokens.append((index, inst["instrument_token"]))
    tokens += [(s["symbol"], s["spot_token"]) for s in fno_universe() if s["spot_token"]]
    daily_only = set()
    try:   # NIFTY 500 stocks outside F&O: daily history only, for the cash backtests (26 Nov 2026)
        from backend.marketdata.cash_universe import cash_universe
        extra = [(s["symbol"], s["spot_token"]) for s in cash_universe() if not s["fno"] and s["spot_token"]]
        tokens += extra
        daily_only = {t for _, t in extra}
    except Exception:
        pass
    ten_min = set()
    try:
        from backend.signals.orb import orb_universe, settings as orb_settings
        if orb_settings()["enabled"]:
            ten_min = {s["spot_token"] for s in orb_universe(now.date())}
            nifty = spot_instrument("NIFTY")
            if nifty:
                ten_min.add(nifty["instrument_token"])
    except Exception:
        pass
    five_min = set()
    try:  # NIFTY option buying: 5-minute index history (same depth as the 10-minute history)
        from backend.signals.nifty_options import settings as opt_settings
        if opt_settings()["enabled"]:
            inst = spot_instrument(opt_settings()["underlying"])
            if inst:
                five_min.add(inst["instrument_token"])
    except Exception:
        pass
    ten_min_start = now - timedelta(days=HISTORY_10MIN_MONTHS * 31)
    saved, errors = 0, []
    _set_progress(status="running", started_at=now.isoformat(timespec="seconds"), finished_at=None,
                  done=0, total=len(tokens), current=None, candles_saved=0, errors=[], message=None)
    try:
        for i, (name, token) in enumerate(tokens, 1):
            _set_progress(current=name)
            try:
                saved += backfill_series(client, token, "day", daily_start, now)
                if token not in daily_only:
                    saved += backfill_series(client, token, "60minute", hourly_start, now)
                if token in ten_min:
                    saved += backfill_series(client, token, "10minute", ten_min_start, now)
                if token in five_min:
                    saved += backfill_series(client, token, "5minute", ten_min_start, now)
            except Exception as exc:
                from backend.brokers.kite import KiteAuthExpired, _is_auth_error
                if _is_auth_error(exc):
                    raise KiteAuthExpired("Kite session expired. Please connect Kite again.") from exc
                errors.append(f"{name}: {type(exc).__name__}")
            _set_progress(done=i, candles_saved=saved, errors=errors[-20:])
            if progress:
                progress(f"history {i}/{len(tokens)}")
    except Exception as exc:
        _set_progress(status="error", message=str(exc)[:200],
                      finished_at=datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"))
        raise
    _set_progress(status="done", current=None,
                  finished_at=datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"))
    return {"series": len(tokens), "candles_saved": saved, "errors": errors[:20]}
