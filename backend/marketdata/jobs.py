"""Market data jobs: morning instrument refresh and candle sync.

The scheduler (next step) calls these at fixed times; the API can also trigger
them by hand. Both respect the system pause switch and need today's Kite login.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from typing import Callable

from backend.brokers.kite import KiteAuthExpired, KiteConfigError, _is_auth_error, get_authenticated_client
from backend.db import get_db
from backend.marketdata.candles import sync_candles
from backend.marketdata.instruments import (
    active_future, fno_universe, refresh_ban_list, refresh_instruments, spot_instrument,
)
from backend.marketdata.store import MCX_UNDERLYINGS, SCANNED_INDICES, ensure_marketdata_tables
from backend.system_state import is_paused
from tradingagents.utils.market_calendar import IST, MCX, NSE

_job_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def build_sync_plan(today: date | None = None) -> list[dict]:
    """Every (instrument, interval) the scanner needs, with backfill depth.

    Stocks: spot 1h (4H is resampled) + daily; the ORB stocks' active future
    also 10-minute with OI. Indices: spot 1h, daily and
    10m (for options), plus the active future's 1h with OI. MCX: active mini
    future 1h with OI, and continuous daily for long-range context.
    """
    plan: list[dict] = []

    def add(label, row, interval, exchange, lookback, continuous=False, oi=False):
        if row:
            plan.append({
                "label": label, "token": row["instrument_token"], "interval": interval,
                "exchange": exchange, "lookback_days": lookback,
                "continuous": continuous, "oi": oi,
            })

    for stock in fno_universe(today):
        if stock["spot_token"]:
            row = {"instrument_token": stock["spot_token"]}
            add(stock["symbol"], row, "60minute", NSE, 120)
            add(stock["symbol"], row, "day", NSE, 400)
        if stock["future_token"]:  # traded contract for the 1h futures scan, with OI
            add(f"{stock['symbol']} FUT", {"instrument_token": stock["future_token"]}, "60minute", NSE, 60, oi=True)
    try:  # ORB: 10-minute bars (with OI) of the most liquid stocks' active futures
        from backend.signals.orb import orb_universe, settings as orb_settings
        if orb_settings()["enabled"]:
            for stock in orb_universe(today or date.today()):
                add(f"{stock['symbol']} FUT 10m", stock["contract"], "10minute", NSE, 35, oi=True)
    except Exception:  # never let the ORB list break the main sync plan
        pass
    for name in SCANNED_INDICES:
        spot = spot_instrument(name)
        add(name, spot, "60minute", NSE, 120)
        add(name, spot, "day", NSE, 400)
        add(name, spot, "10minute", NSE, 30)
        fut = active_future(name, today=today)
        add(f"{name} FUT", fut, "60minute", NSE, 60, oi=True)
    for name in MCX_UNDERLYINGS:
        fut = active_future(name, segment="MCX-FUT", today=today)
        add(f"{name} FUT", fut, "60minute", MCX, 90, oi=True)
        add(f"{name} FUT", fut, "day", MCX, 400, continuous=True)
    try:  # your own trades: keep their contract's bars current (any expiry, any interval they trail on)
        from backend.my_trades import open_contracts
        have = {(p["token"], p["interval"]) for p in plan}
        for t in open_contracts():
            if (t["instrument_token"], t["interval"]) not in have:
                add(f"{t['contract']} (my trade)", t, t["interval"], MCX if t["exchange"] == "MCX" else NSE, 30)
    except Exception:  # never let it break the main sync plan
        pass
    return plan


def _record(job: str, started: datetime, status: str, detail: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO marketdata_runs (job, started_at, finished_at, status, detail) VALUES (?, ?, ?, ?, ?)",
            (job, started.isoformat(timespec="seconds"), _now().isoformat(timespec="seconds"),
             status, json.dumps(detail)[:4000]),
        )


def _run(job: str, body: Callable[[object], dict], client=None) -> dict:
    ensure_marketdata_tables()
    started = _now()
    if is_paused():
        result = {"status": "skipped", "reason": "paused"}
        _record(job, started, "skipped", result)
        return result
    if not _job_lock.acquire(blocking=False):
        return {"status": "busy", "reason": "another market data job is running"}
    try:
        client = client or get_authenticated_client()
        detail = body(client)
        status = "error" if detail.get("errors") else "ok"
        _record(job, started, status, detail)
        return {"status": status, **detail}
    except (KiteConfigError, KiteAuthExpired) as exc:
        result = {"status": "error", "reason": str(exc)}
        _record(job, started, "error", result)
        return result
    finally:
        _job_lock.release()


def morning_refresh(client=None) -> dict:
    """Instrument master + F&O ban list + results dates. Run before 09:15 on trading days."""
    def body(c):
        summary = refresh_instruments(c)
        ban = refresh_ban_list()
        try:
            from backend.marketdata.instruments import fno_stock_names
            from backend.signals.events import refresh_results
            results = refresh_results(fno_stock_names())
        except Exception as exc:  # results dates are re-checked later; never block the refresh
            results = {"error": type(exc).__name__}
        return {"instruments": summary, "ban_list": {k: v for k, v in ban.items() if k != "symbols"},
                "results_dates": results}
    return _run("morning_refresh", body, client)


def candle_sync(client=None, intervals: tuple[str, ...] | None = None,
                exchanges: tuple[str, ...] | None = None, now: datetime | None = None) -> dict:
    """Bring stored candles up to date for the whole sync plan."""
    def body(c):
        saved, errors, done = 0, [], 0
        for item in build_sync_plan():
            if intervals and item["interval"] not in intervals:
                continue
            if exchanges and item["exchange"] not in exchanges:
                continue
            try:
                saved += sync_candles(
                    c, item["token"], item["interval"], item["exchange"], item["lookback_days"],
                    now=now, continuous=item["continuous"], oi=item["oi"],
                )
                done += 1
            except Exception as exc:
                if _is_auth_error(exc):
                    raise KiteAuthExpired("Kite session expired. Please connect Kite again.") from exc
                errors.append(f"{item['label']} {item['interval']}: {type(exc).__name__}")
                if len(errors) >= 20:
                    break
        return {"series_synced": done, "candles_saved": saved, "errors": errors}
    return _run("candle_sync", body, client)


def recent_runs(limit: int = 10) -> list[dict]:
    ensure_marketdata_tables()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT job, started_at, finished_at, status, detail FROM marketdata_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item["detail"] or "{}")
        except ValueError:
            pass
        out.append(item)
    return out


def history_backfill(client=None) -> dict:
    """Download older daily/hourly spot history for backtests (Backtest page button)."""
    from backend.backtest.data import backfill_for_backtests
    from backend.marketdata.instruments import fno_universe

    def body(c):
        refreshed = None
        if not fno_universe():  # first run (e.g. a weekend before any morning refresh)
            refreshed = refresh_instruments(c)
        return {**backfill_for_backtests(c), "instruments_refreshed": refreshed}
    return _run("history_backfill", body, client)
