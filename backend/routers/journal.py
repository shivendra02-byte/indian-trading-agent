"""Trade journal and learning report routes."""

from __future__ import annotations

from typing import Literal

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.journal import postmortem
from backend.journal.learning import report
from backend.journal.store import list_records, set_user_action
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


class UserAction(BaseModel):
    action: Literal["taken", "skipped"] | None = None
    fill_price: float | None = Field(None, gt=0)
    exit_price: float | None = Field(None, gt=0)
    note: str | None = Field(None, max_length=500)


@router.put("/{record_id}/action")
def mark_action(record_id: int, data: UserAction):
    rec = set_user_action(record_id, data.action, data.fill_price, data.exit_price, data.note)
    if rec is None:
        raise HTTPException(404, "journal record not found")
    return {k: rec.get(k) for k in ("id", "user_action", "user_fill_price", "user_exit_price", "user_note")}


# --- weekly post-mortem --------------------------------------------------------------------

class PostmortemRun(BaseModel):
    week_start: date | None = None     # any date in the week; default: this week
    with_ai: bool = True


@router.get("/postmortem")
def postmortem_list():
    return {"weeks": postmortem.list_postmortems()}


@router.get("/postmortem/{week_start}")
def postmortem_get(week_start: date):
    item = postmortem.get_postmortem(postmortem.week_bounds(week_start)[0].isoformat())
    if item is None:
        raise HTTPException(404, "no post-mortem for that week yet")
    return item


@router.post("/postmortem/run")
def postmortem_run(data: PostmortemRun):
    return postmortem.run_weekly_postmortem(data.week_start, with_ai=data.with_ai)


# --- AI panel ------------------------------------------------------------------------------

@router.post("/panel/run")
def panel_run():
    """Review pending shortlisted trades now (the scans also do this after every run)."""
    from backend.journal.panel import review_pending
    return review_pending()
