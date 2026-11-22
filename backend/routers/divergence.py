"""RSI divergence alerts: recent signals with outcomes, scan now, history study."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from backend.signals import divergence

router = APIRouter(prefix="/api/divergence", tags=["divergence"])


@router.get("")
def recent(limit: int = Query(200, ge=1, le=1000), status: Literal["open", "closed"] | None = None):
    rows = divergence.list_signals(limit=limit, status=status)
    closed = [r for r in rows if r["status"] == "closed"]
    live = {kind: divergence._summary([r for r in closed if r["kind"] == kind]) for kind in ("bullish", "bearish")}
    return {"signals": rows, "live_record": live, "settings": divergence.settings().model_dump()}


@router.post("/run")
def run_now(group: Literal["nse", "mcx"] = "nse"):
    return divergence.run_scan(group)


@router.get("/study")
def study():
    return divergence.latest_study() or {}


@router.post("/study")
def run_study():
    return divergence.run_study()
