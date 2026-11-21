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
