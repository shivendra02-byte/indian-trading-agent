"""Option data jobs: history backfill, the daily evening update and India VIX.

Downloads are data only (no scans, signals or AI calls), so they also run while
the system is paused. One job at a time; progress is kept in memory for the page.
"""

from __future__ import annotations

import os
import threading
import time as time_mod
from datetime import date, datetime, time, timedelta
from typing import Callable

from backend.decision.config import get_config
from backend.options import bhavcopy
from backend.options.store import ensure_option_tables, file_status, record_file, save_day, save_vix
from tradingagents.utils.market_calendar import IST, is_trading_day

EXCHANGES = ("NSE", "BSE")
VIX_TICKER = "^INDIAVIX"

_lock = threading.Lock()
_progress: dict = {"status": "idle"}


def settings():
    return get_config().global_.option_data


def enabled() -> bool:
    return os.environ.get("TRADINGAGENTS_OPTION_DATA", "on").lower() != "off" and settings().enabled


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def progress_snapshot() -> dict:
    return dict(_progress)


def _set(**fields) -> None:
    _progress.update(fields)


def trading_days(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _symbols(exchange: str) -> list[str]:
    return list(settings().symbols.get(exchange, []))


def _url(exchange: str, d: date) -> str:
    cfg = settings()
    return bhavcopy.file_url(cfg.nse_url if exchange == "NSE" else cfg.bse_url, d)


def import_file(data: bytes, exchange: str, source: str = "upload", expected: date | None = None) -> dict:
    """Parse one bhavcopy file, add IV/Greeks and store it."""
    ensure_option_tables()
    symbols = _symbols(exchange)
    if not symbols:
        return {"status": "skipped", "reason": f"no {exchange} symbols configured"}
    trade_date, rows = bhavcopy.parse(bhavcopy.to_text(data), symbols)
    trade_date = trade_date or expected
    if not trade_date:
        raise bhavcopy.BhavcopyFormatError("no trade date in the file")
    if expected and trade_date != expected:
        raise bhavcopy.BhavcopyFormatError(f"file is for {trade_date}, expected {expected}")
    if not rows:
        record_file(trade_date, exchange, "error", rows=0, source=source, detail=f"no rows for {', '.join(symbols)}")
        return {"status": "error", "trade_date": trade_date.isoformat(), "reason": "no matching rows"}
    bhavcopy.enrich(rows, trade_date, settings().risk_free_rate)
    saved = save_day(trade_date, exchange, rows, source)
    with_iv = sum(1 for r in rows if r.get("iv") is not None)
    return {"status": "ok", "trade_date": trade_date.isoformat(), "exchange": exchange, "rows": saved,
            "with_iv": with_iv}


def load_day(d: date, exchange: str, fetch: Callable[[str], bytes | None] | None = None,
             force: bool = False) -> dict:
    """Download and store one exchange's file for one day (skips days already stored)."""
    if not force and file_status(d, exchange) == "ok":
        return {"status": "exists"}
    fetch = fetch or bhavcopy.download
    try:
        data = fetch(_url(exchange, d))
        if data is None:
            record_file(d, exchange, "missing", detail="no file published (404)")
            return {"status": "missing"}
        return import_file(data, exchange, source="download", expected=d)
    except Exception as exc:  # one bad day must not stop a backfill
        detail = f"{type(exc).__name__}: {exc}"[:300]
        record_file(d, exchange, "error", detail=detail)
        return {"status": "error", "reason": detail}


def update_vix(start: date, end: date, download: Callable | None = None) -> dict:
    """India VIX daily closes from Yahoo Finance (^INDIAVIX)."""
    try:
        if download is None:
            import yfinance as yf
            frame = yf.download(VIX_TICKER, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                interval="1d", progress=False, auto_adjust=False)
        else:
            frame = download(start, end)
        rows = []
        if frame is not None and len(frame):
            if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
                frame = frame.droplevel(-1, axis=1)
            for ts, rec in frame.iterrows():
                close = rec.get("Close")
                if close is None or close != close:   # NaN
                    continue
                rows.append({"trade_date": ts.date().isoformat(), "open": _f(rec.get("Open")),
                             "high": _f(rec.get("High")), "low": _f(rec.get("Low")), "close": float(close)})
        return {"status": "ok", "days": save_vix(rows)}
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _f(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def backfill(start: date, end: date, exchanges: tuple[str, ...] = EXCHANGES, fetch=None, vix_download=None,
             force: bool = False, pause_seconds: float | None = None, label: str = "backfill") -> dict:
    """Download every trading day in [start, end]. Returns busy if a job is running."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        ensure_option_tables()
        pause = settings().request_pause_seconds if pause_seconds is None else pause_seconds
        days = trading_days(start, end)
        tasks = [(d, ex) for d in days for ex in exchanges if _symbols(ex)]
        counts = {"ok": 0, "exists": 0, "missing": 0, "error": 0, "skipped": 0}
        errors: list[str] = []
        started = time_mod.monotonic()
        _set(status="running", job=label, started_at=_now().isoformat(timespec="seconds"), finished_at=None,
             start=start.isoformat(), end=end.isoformat(), done=0, total=len(tasks), current=None,
             counts=counts, errors=errors, seconds_left=None, vix=None)
        for i, (d, exchange) in enumerate(tasks, 1):
            _set(current=f"{exchange} {d.isoformat()}")
            result = load_day(d, exchange, fetch=fetch, force=force)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] == "error":
                errors.append(f"{exchange} {d}: {result.get('reason')}")
            elapsed = time_mod.monotonic() - started
            _set(done=i, counts=counts, errors=errors[-20:],
                 seconds_left=round(elapsed / i * (len(tasks) - i)) if i else None)
            if pause and result["status"] not in ("exists",) and i < len(tasks):
                time_mod.sleep(pause)
        vix = update_vix(start, end, download=vix_download)
        result = {"status": "done", "days": len(days), "counts": counts, "errors": errors[-20:], "vix": vix}
        _set(status="done", current=None, finished_at=_now().isoformat(timespec="seconds"), vix=vix,
             seconds_left=0)
        return result
    except Exception as exc:
        _set(status="error", message=f"{type(exc).__name__}: {exc}"[:200],
             finished_at=_now().isoformat(timespec="seconds"))
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        _lock.release()


def start_backfill(start: date, end: date, force: bool = False) -> dict:
    if _lock.locked():
        return {"status": "busy"}
    thread = threading.Thread(target=backfill, args=(start, end), kwargs={"force": force},
                              name="option-backfill", daemon=True)
    thread.start()
    return {"status": "started", "start": start.isoformat(), "end": end.isoformat()}


def daily_update(now: datetime | None = None, fetch=None, vix_download=None, pause_seconds: float | None = None) -> dict:
    """Fill the last ``recent_days`` trading days (today only once files are due).
    ``complete`` is true when today's files are stored for every exchange."""
    now = now or _now()
    cfg = settings()
    today = now.date()
    include_today = is_trading_day(today) and now.time() >= _hhmm(cfg.update_after)
    end = today if include_today else today - timedelta(days=1)
    days = trading_days(end - timedelta(days=cfg.recent_days * 2 + 10), end)[-cfg.recent_days:]
    if not days:
        return {"status": "done", "complete": True, "days": 0}
    result = backfill(days[0], days[-1], fetch=fetch, vix_download=vix_download,
                      pause_seconds=pause_seconds, label="daily update")
    if result.get("status") == "busy":
        return {"status": "busy", "complete": False}
    complete = (not include_today) or all(
        file_status(today, ex) == "ok" for ex in EXCHANGES if _symbols(ex))
    return {**result, "complete": complete}


def eod_due(now: datetime, done_on: date | None, last_attempt: datetime | None) -> bool:
    """Pure: should the scheduler run the daily option update now?"""
    if not enabled():
        return False
    cfg = settings()
    today = now.date()
    if done_on == today or not is_trading_day(today):
        return False
    if not (_hhmm(cfg.update_after) <= now.time() <= _hhmm(cfg.update_until)):
        return False
    return last_attempt is None or now - last_attempt >= timedelta(minutes=cfg.retry_minutes)
