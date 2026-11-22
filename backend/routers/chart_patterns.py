"""Chart pattern alerts (trendline bounce / break, triangle breakout): signals, scan, study."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from backend.signals import chart_patterns, divergence

router = APIRouter(prefix="/api/chart-patterns", tags=["chart-patterns"])


@router.get("")
def recent(limit: int = Query(200, ge=1, le=1000), status: Literal["open", "closed"] | None = None):
    rows = chart_patterns.list_signals(limit=limit, status=status)
    closed = [r for r in rows if r["status"] == "closed"]
    live: dict[str, dict] = {}
    for r in closed:
        live.setdefault(f"{r['pattern']} {r['direction']}", []).append(r)
    return {"signals": rows, "live_record": {k: divergence._summary(v) for k, v in sorted(live.items())},
            "settings": chart_patterns.settings().model_dump()}


@router.post("/run")
def run_now(group: Literal["nse", "mcx"] = "nse"):
    return chart_patterns.run_scan(group)


@router.get("/study")
def study():
    return {"latest": chart_patterns.latest_study() or {}, "progress": chart_patterns.study_progress()}


@router.post("/study")
def start_study():
    result = chart_patterns.start_study()
    if result["status"] == "busy":
        raise HTTPException(409, "a study is already running")
    return result
