"""Your own futures trades with a system-calculated trailing stop."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend import my_trades

router = APIRouter(prefix="/api/my-trades", tags=["my-trades"])


class TradeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=25)
    exchange: Literal["NFO", "MCX"] = "NFO"
    contract: str | None = Field(default=None, max_length=40)
    direction: Literal["long", "short"]
    lots: int = Field(ge=1, le=1000)
    entry_price: float = Field(gt=0)
    entry_time: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
    initial_stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    interval: Literal["10minute", "60minute"] | None = None
    breakeven_at_r: float | None = Field(default=None, ge=0.25, le=5)
    breakeven_lock_r: float | None = Field(default=None, ge=0, le=2)
    trail_start_r: float | None = Field(default=None, ge=0.25, le=10)
    trail_atr: float | None = Field(default=None, ge=0.5, le=10)
    note: str | None = Field(default=None, max_length=300)


class CloseRequest(BaseModel):
    exit_price: float = Field(gt=0)
    exit_time: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


@router.get("")
def list_trades():
    return {"trades": my_trades.list_trades(), "settings": my_trades.settings().model_dump()}


@router.get("/contracts")
def contracts(symbol: str = Query(..., min_length=1, max_length=25), exchange: Literal["NFO", "MCX"] = "NFO"):
    return {"contracts": my_trades.contracts(symbol, exchange)}


@router.post("")
def add(data: TradeRequest):
    try:
        return my_trades.add_trade(data.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/refresh")
def refresh():
    return my_trades.update_trades()


@router.post("/{trade_id}/close")
def close(trade_id: int, data: CloseRequest):
    try:
        return my_trades.close_trade(trade_id, data.exit_price, data.exit_time)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, str(exc)) from exc


@router.delete("/{trade_id}")
def delete(trade_id: int):
    if not my_trades.delete_trade(trade_id):
        raise HTTPException(404, "trade not found")
    return {"status": "deleted"}
