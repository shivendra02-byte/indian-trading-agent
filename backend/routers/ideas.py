"""Ideas tracker: log trade ideas from anywhere and see their record by source and pattern."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import ideas

router = APIRouter(prefix="/api/ideas", tags=["ideas"])


class IdeaRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=25)
    direction: Literal["long", "short"]
    entry_type: Literal["market", "limit", "breakout"] | None = None
    entry: float | None = Field(default=None, gt=0)
    invalid: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    idea_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    horizon_days: int | None = Field(default=None, ge=3, le=250)
    source: str | None = Field(default=None, max_length=80)
    pattern: str | None = None
    note: str | None = Field(default=None, max_length=500)


@router.get("")
def list_ideas():
    rows = ideas.list_ideas()
    return {"ideas": rows, "record": ideas.record(rows), "patterns": ideas.PATTERNS}


@router.post("")
def add(data: IdeaRequest):
    try:
        return ideas.add_idea(data.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{idea_id}")
def delete(idea_id: int):
    if not ideas.delete_idea(idea_id):
        raise HTTPException(404, "idea not found")
    return {"status": "deleted"}


@router.post("/refresh")
def refresh():
    return ideas.refresh_ideas()
