"""Today's P&L across your futures trades and the system's paper trades."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/pnl", tags=["pnl"])


@router.get("/today")
def pnl_today(day: date | None = Query(None, description="defaults to today")):
    from backend.pnl_today import today_summary
    return today_summary(day)
