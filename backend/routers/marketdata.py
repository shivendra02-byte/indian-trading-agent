"""Market data layer, market calendar and pause-switch routes."""

from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.brokers.kite import get_kite_status
from backend.marketdata import jobs
from backend.marketdata.candles import INTERVALS, get_candles, resample_4h, resample_weekly
from backend.marketdata.instruments import ban_status, fno_universe, instrument_summary
from backend.marketdata.store import ensure_marketdata_tables
from backend.system_state import get_pause_state, set_paused
from backend.db import get_db
from tradingagents.utils.market_calendar import EXCHANGES, exchange_status, upcoming_closures

router = APIRouter(tags=["marketdata"])


class PauseRequest(BaseModel):
    paused: bool
    reason: str | None = Field(default=None, max_length=200)


@router.get("/api/system/pause")
def pause_state():
    return get_pause_state()


@router.post("/api/system/pause")
def update_pause(data: PauseRequest):
    return set_paused(data.paused, data.reason)


@router.get("/api/scheduler/status")
def scheduler_status():
    from backend.scheduler import scheduler
    return scheduler.status()


@router.get("/api/scans/cash-swing")
def cash_swing_latest(signal_date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    from backend.signals.cash_swing import latest_run
    return latest_run(signal_date) or {"status": "none", "candidates": []}


@router.post("/api/scans/cash-swing/run")
def cash_swing_run():
    """Run the daily cash swing scan now (uses the latest completed daily bars)."""
    from backend.signals.cash_swing import run_scan
    return run_scan(force=True)


@router.get("/api/scans/cup-handle")
def cup_handle_latest():
    from backend.signals.cash_swing import latest_run
    from backend.signals.cup_handle import SEGMENT
    return latest_run(segment=SEGMENT) or {"status": "none", "candidates": []}


@router.post("/api/scans/cup-handle/run")
def cup_handle_run():
    """Run the cup-and-handle scan now on the latest completed daily bars."""
    from backend.signals.cup_handle import run_scan
    return run_scan(force=True)


@router.get("/api/scans/wolfe")
def wolfe_latest():
    from backend.signals.cash_swing import latest_run
    from backend.signals.wolfe import SEGMENT
    return latest_run(segment=SEGMENT) or {"status": "none", "candidates": []}


@router.post("/api/scans/wolfe/run")
def wolfe_run():
    """Run the Wolfe Wave scan now on the latest completed daily bars."""
    from backend.signals.wolfe import run_scan
    return run_scan(force=True)


@router.get("/api/scans/nifty-options")
def nifty_options_latest():
    """Latest 5-minute NIFTY option-buying scan and today's signals with their outcome."""
    from datetime import date
    from backend.journal.store import list_records
    from backend.signals.cash_swing import latest_run
    from backend.signals.nifty_options import SCAN_SEGMENT, SEGMENT, option_premium_move, settings
    recs = list_records("segment = ?", (SEGMENT,), limit=60)
    for r in recs:
        r["premium"] = option_premium_move(r) if r.get("fill_ts") else None
    cfg = settings()
    return {"latest_run": latest_run(segment=SCAN_SEGMENT), "records": recs, "today": date.today().isoformat(),
            "settings": {k: cfg[k] for k in ("enabled", "setups", "first_signal", "last_signal", "exit_time",
                                               "target_r", "expiry_day_cutoff", "max_signals_per_setup_day")},
            "shadow": "nifty_options" in __import__("backend.decision.config", fromlist=["get_config"])
            .get_config().global_.shadow_strategies}


@router.post("/api/scans/nifty-options/run")
def nifty_options_run():
    """Run the NIFTY option-buying scan now on the latest stored 5-minute bar."""
    from backend.signals.nifty_options import run_scan
    return run_scan(force=True)


@router.get("/api/scans/orb")
def orb_latest():
    from backend.signals.cash_swing import latest_run
    from backend.signals.orb import SCAN_SEGMENT
    return latest_run(segment=SCAN_SEGMENT) or {"status": "none", "candidates": []}


@router.post("/api/scans/orb/run")
def orb_run():
    """Run the opening range breakout scan now on the latest completed 10-minute bar."""
    from backend.signals.orb import run_orb_scan
    return run_orb_scan(force=True)


@router.get("/api/scans/hourly/{group}")
def hourly_latest(group: Literal["nse", "mcx"]):
    from backend.signals.cash_swing import latest_run
    from backend.signals.fno import GROUPS
    return latest_run(segment=GROUPS[group]) or {"status": "none", "candidates": []}


@router.post("/api/scans/hourly/{group}/run")
def hourly_run(group: Literal["nse", "mcx"]):
    """Run the hourly scan now on the latest completed 1h bar."""
    from backend.signals.fno import run_hourly_scan
    return run_hourly_scan(group, force=True)


@router.get("/api/decision-engine/config")
def decision_config():
    """The active decision-engine configuration (read-only) and its rule version."""
    from backend.decision.config import config_path, get_config
    cfg = get_config()
    return {"rule_version": cfg.rule_version, "path": config_path().name,
            "config": cfg.model_dump(by_alias=True, exclude={"rule_version"})}


@router.get("/api/market-calendar/status")
def calendar_status():
    return {
        "exchanges": [exchange_status(ex) for ex in EXCHANGES],
        "upcoming": {ex: upcoming_closures(ex, days=60) for ex in EXCHANGES},
    }


@router.get("/api/marketdata/status")
def status():
    ensure_marketdata_tables()
    kite = get_kite_status()
    return {
        "kite_connected_today": kite["connected_today"],
        "pause": get_pause_state(),
        "instruments": instrument_summary(),
        "ban_list": {k: v for k, v in ban_status().items()},
        "recent_runs": jobs.recent_runs(10),
    }


@router.post("/api/marketdata/refresh-instruments")
def refresh_instruments():
    result = jobs.morning_refresh()
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("reason") or "Instrument refresh failed")
    return result


@router.post("/api/marketdata/sync-candles")
def sync_candles(interval: Literal["10minute", "60minute", "day"] | None = None):
    """Start a candle sync in the background; check /status for the result."""
    if jobs._job_lock.locked():
        return {"status": "busy"}
    intervals = (interval,) if interval else None
    threading.Thread(target=jobs.candle_sync, kwargs={"intervals": intervals}, daemon=True).start()
    return {"status": "started"}


@router.get("/api/marketdata/universe")
def universe():
    ensure_marketdata_tables()
    stocks = fno_universe()
    return {"count": len(stocks), "ban_list": ban_status(), "stocks": stocks}


@router.get("/api/marketdata/candles")
def candles(
    token: int = Query(..., ge=1),
    interval: Literal["10minute", "60minute", "day", "4h", "week"] = "60minute",
    limit: int = Query(200, ge=1, le=5000),
):
    ensure_marketdata_tables()
    with get_db() as conn:
        inst = conn.execute(
            "SELECT tradingsymbol, exchange, segment FROM instruments WHERE instrument_token = ?", (token,)
        ).fetchone()
    if not inst:
        raise HTTPException(status_code=404, detail="Unknown instrument token")
    exchange = "MCX" if inst["exchange"] == "MCX" else "NSE"
    if interval == "4h":
        rows = resample_4h(get_candles(token, "60minute", limit=limit * 4 + 8), exchange)[-limit:]
    elif interval == "week":
        rows = resample_weekly(get_candles(token, "day", limit=limit * 5 + 5))[-limit:]
    else:
        assert interval in INTERVALS
        rows = get_candles(token, interval, limit=limit)
    return {"token": token, "symbol": inst["tradingsymbol"], "interval": interval, "candles": rows}
