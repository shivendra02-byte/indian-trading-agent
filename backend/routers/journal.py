"""Trade journal and learning report routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from backend.journal.learning import report
from backend.journal.store import list_records
from backend.journal.tracker import track_open_records

router = APIRouter(prefix="/api/journal", tags=["journal"])

Decision = Literal["soft_rejected", "below_score", "not_selected", "shortlisted", "approved", "rejected_panel"]
Status = Literal["pending", "open", "closed", "no_fill"]


@router.get("")
def journal(
    segment: str | None = Query(None, pattern=r"^[a-z_]{1,32}$"),
    decision: Decision | None = None,
    status: Status | None = None,
    limit: int = Query(200, ge=1, le=2000),
):
    clauses, params = [], []
    for column, value in (("segment", segment), ("decision", decision), ("track_status", status)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    rows = list_records(" AND ".join(clauses) or "1=1", tuple(params), limit=limit)
    return {"count": len(rows), "records": rows}


@router.get("/learning")
def learning(segment: str | None = Query(None, pattern=r"^[a-z_]{1,32}$")):
    return report(segment)


@router.post("/track")
def track():
    return track_open_records()
