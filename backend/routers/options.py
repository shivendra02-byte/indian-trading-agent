"""End-of-day option data routes: status, backfill, daily update, upload, chain."""

from __future__ import annotations

import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from backend.options import bhavcopy, jobs, store

router = APIRouter(prefix="/api/options", tags=["options"])

MAX_UPLOAD_BYTES = 100_000_000


class BackfillRequest(BaseModel):
    start_date: date
    end_date: date
    force: bool = False


@router.get("/status")
def status():
    store.ensure_option_tables()
    cfg = jobs.settings()
    return {
        **store.summary(),
        "enabled": jobs.enabled(),
        "symbols": cfg.symbols,
        "risk_free_rate": cfg.risk_free_rate,
        "update_after": cfg.update_after,
        "max_history_days": cfg.max_history_days,
        "progress": jobs.progress_snapshot(),
    }


@router.get("/backfill")
def backfill_progress():
    return jobs.progress_snapshot()


@router.post("/backfill")
def start_backfill(data: BackfillRequest):
    """Download NSE/BSE option files for every trading day in the range (background)."""
    today = date.today()
    if data.end_date < data.start_date:
        raise HTTPException(400, "end_date is before start_date")
    if data.end_date > today:
        raise HTTPException(400, "end_date is in the future")
    if (data.end_date - data.start_date).days > jobs.settings().max_history_days:
        raise HTTPException(400, f"range is longer than {jobs.settings().max_history_days} days")
    result = jobs.start_backfill(data.start_date, data.end_date, force=data.force)
    if result["status"] == "busy":
        raise HTTPException(409, "an option data job is already running")
    return result


@router.post("/update")
def update_now():
    """Run the daily update now (last few trading days) in the background."""
    if jobs._lock.locked():
        raise HTTPException(409, "an option data job is already running")
    threading.Thread(target=jobs.daily_update, daemon=True, name="option-update").start()
    return {"status": "started"}


@router.post("/upload")
async def upload(request: Request, exchange: Literal["NSE", "BSE"] = Query(...)):
    """Import a bhavcopy file you downloaded yourself (raw .csv or .zip as the body),
    for days the server could not download."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    try:
        result = jobs.import_file(data, exchange, source="upload")
    except (bhavcopy.BhavcopyFormatError, ValueError) as exc:
        raise HTTPException(400, f"could not read the file: {exc}") from exc
    if result["status"] != "ok":
        raise HTTPException(400, result.get("reason") or "no rows imported")
    return result


@router.get("/chain")
def option_chain(symbol: str = Query("NIFTY", pattern=r"^[A-Z]{3,15}$"),
                 day: str | None = Query(None, alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
                 expiry: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Stored end-of-day chain: calls and puts by strike with IV and delta."""
    store.ensure_option_tables()
    day = day or store.latest_date(symbol)
    if not day:
        return {"symbol": symbol, "date": None, "expiries": [], "rows": []}
    expiries = store.expiries(symbol, day)
    if not expiries:
        return {"symbol": symbol, "date": day, "expiries": [], "rows": []}
    expiry = expiry if expiry in expiries else expiries[0]
    rows = store.chain(symbol, day, expiry)
    by_strike: dict[float, dict] = {}
    for row in rows:
        side = {k: row[k] for k in ("close", "oi", "chg_oi", "volume", "iv", "delta", "theta", "vega")}
        by_strike.setdefault(row["strike"], {"strike": row["strike"]})[row["opt_type"]] = side
    first = rows[0] if rows else {}
    forward = first.get("forward")
    atm = min(by_strike, key=lambda k: abs(k - forward)) if forward and by_strike else None
    return {
        "symbol": symbol, "date": day, "expiry": expiry, "expiries": expiries,
        "days_to_expiry": first.get("days_to_expiry"), "forward": forward,
        "forward_source": first.get("forward_source"), "spot": first.get("underlying"),
        "lot_size": first.get("lot_size"), "vix": store.vix_on(day), "atm_strike": atm,
        "rows": [by_strike[k] for k in sorted(by_strike)],
    }


# --- credit spreads -----------------------------------------------------------------------

class SpreadBacktestRequest(BaseModel):
    start_date: date
    end_date: date
    symbols: list[Literal["NIFTY", "BANKNIFTY", "SENSEX"]] | None = None
    split_date: date | None = None


@router.get("/spreads")
def spreads_status():
    from backend.options.spreads import record, settings, spreads
    cfg = settings()
    return {"enabled": cfg["enabled"], "settings": {k: cfg[k] for k in (
        "short_delta", "min_credit_ratio", "take_profit_pct", "stop_multiple", "max_vix", "max_open", "symbols",
        "regime_strategy")}, "open": spreads("open"), "closed": spreads("closed"), "record": record()}


@router.post("/spreads/run")
def spreads_run():
    """Follow open spreads and build today's candidates from the stored option files."""
    from backend.options.spreads import run_eod
    return run_eod()


@router.post("/spreads/backtest")
def spreads_backtest(req: SpreadBacktestRequest):
    from backend.options.spreads import backtest
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    result = backtest(req.start_date, req.end_date, req.symbols, req.split_date)
    result["trades"] = result["trades"][-300:]
    return result
