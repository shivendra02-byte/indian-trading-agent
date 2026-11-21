"""Rule backtester routes: download history, run a rule set over it, compare runs."""

from __future__ import annotations

import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.backtest import engine
from backend.decision.config import available_rule_sets
from backend.marketdata import jobs

router = APIRouter(prefix="/api/rule-backtests", tags=["rule-backtests"])


class RunRequest(BaseModel):
    segment: Literal["cash_swing", "cup_handle", "stock_futures_proxy", "index_futures_proxy"]
    rule_set: str = Field(max_length=60, pattern=r"^[A-Za-z0-9_\-]+$")
    start_date: date
    end_date: date
    max_symbols: int | None = Field(default=None, ge=1, le=500)
    daily_only: bool = False   # model every entry on daily bars (compare periods on equal terms)


@router.get("/rule-sets")
def rule_sets():
    return {"rule_sets": sorted(available_rule_sets()), "segments": list(engine.SEGMENTS)}


@router.get("/history")
def history_progress():
    """Progress of the history download (stocks done/total, candles saved, time left, failures)."""
    from backend.backtest.data import progress_snapshot
    return progress_snapshot()


@router.post("/history")
def download_history():
    """Start the history download in the background; poll GET /history for progress."""
    from backend.backtest.data import PROGRESS
    if jobs._job_lock.locked():
        return {"status": "busy"}

    def run():
        result = jobs.history_backfill()
        if result.get("status") in ("error", "skipped", "busy") and PROGRESS.get("status") not in ("done", "error"):
            PROGRESS.update(status="error", message=result.get("reason") or "history download failed")

    PROGRESS.update(status="starting", done=0, total=0, message=None, errors=[])
    threading.Thread(target=run, daemon=True, name="history-backfill").start()
    return {"status": "started"}


@router.post("")
def start(data: RunRequest):
    try:
        run_id = engine.start_backtest(data.segment, data.rule_set, data.start_date, data.end_date,
                                       data.max_symbols, data.daily_only)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": run_id, "status": "running"}


@router.get("")
def runs(limit: int = Query(30, ge=1, le=200)):
    return {"runs": engine.list_runs(limit)}


@router.get("/compare")
def compare(ids: str = Query(..., pattern=r"^\d+(,\d+){0,5}$")):
    return engine.compare([int(i) for i in ids.split(",")])


@router.get("/{run_id}")
def run(run_id: int, trades: bool = False):
    result = engine.get_run(run_id, with_trades=trades)
    if not result:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return result
