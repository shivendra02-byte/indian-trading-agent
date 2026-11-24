"""Momentum book API: ranking, positions, portfolio backtests."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/momentum", tags=["momentum"])


@router.get("/")
def momentum_status():
    from backend.momentum.book import status
    return status()


@router.post("/rank")
def momentum_rank():
    """Rebuild this week's ranking now."""
    from backend.momentum.book import run_ranking
    return run_ranking(force=True)


@router.post("/scan")
def momentum_scan():
    """Follow open positions and look for entries on the latest hourly bars."""
    from backend.momentum.book import hourly_run
    return hourly_run()


class SideRequest(BaseModel):
    enabled: bool | None = None          # None = back to the config default


@router.put("/sides/{side}")
def momentum_side(side: Literal["long", "short"], req: SideRequest):
    """Switch the long or short side of the momentum book on/off (new entries only)."""
    from backend.momentum.book import set_side
    return set_side(side, req.enabled)


class BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    sides: list[Literal["long", "short"]] = Field(default_factory=lambda: ["long", "short"], min_length=1)
    daily_only: bool = False
    split_date: date | None = None
    max_symbols: int | None = Field(default=None, ge=5, le=500)


@router.post("/backtests")
def momentum_backtest(req: BacktestRequest):
    from backend.momentum.backtest import start
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    params = req.model_dump(mode="json")
    return start(params)


@router.get("/backtests")
def momentum_backtests():
    from backend.momentum.backtest import list_runs
    return {"runs": list_runs()}


@router.get("/backtests/{run_id}")
def momentum_backtest_get(run_id: int, trades: bool = False):
    from backend.momentum.backtest import get_run
    run = get_run(run_id, with_trades=trades)
    if not run:
        raise HTTPException(status_code=404, detail="backtest not found")
    return run


@router.delete("/backtests/{run_id}")
def momentum_backtest_delete(run_id: int):
    from backend.momentum.backtest import delete_run
    if not delete_run(run_id):
        raise HTTPException(status_code=409, detail="backtest is still running")
    return {"status": "deleted"}
