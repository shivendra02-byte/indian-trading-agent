"""End-of-day option data routes: status, backfill, daily update, upload, chain."""

from __future__ import annotations

import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

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
    # compare strategies (asked 24 Nov 2026): each listed strategy its regime allows, own book each
    strategies: list[Literal["bull_put", "bear_call", "iron_condor", "bull_call", "bear_put"]] | None = None
    condor_in_unstable: bool = False
    min_credit_ratio_single: float | None = Field(None, ge=0, le=1)
    debit_below_vix: float | None = Field(None, ge=0, le=100)   # 0 = never use debit spreads


@router.get("/spreads")
def spreads_status():
    from backend.options.spreads import record, settings, spreads
    cfg = settings()
    return {"enabled": cfg["enabled"], "settings": {k: cfg[k] for k in (
        "short_delta", "min_credit_ratio", "take_profit_pct", "stop_multiple", "max_vix", "max_open", "symbols",
        "regime_strategy", "mode", "intraday", "debit_below_iv_pct", "debit_below_vix")},
        "open": spreads("open"), "closed": spreads("closed"), "record": record()}


@router.get("/spreads/alignment")
def spreads_alignment():
    """Daily · 4H · 1H · 15m state per index for the intraday spread entry (no Kite call)."""
    from backend.options.spreads import alignment
    return alignment()


@router.get("/spreads/scans")
def spreads_scans(day: str | None = None):
    """Every intraday spread scan of a day and its outcome per index (newest first)."""
    from datetime import date as _date
    from backend.options.spreads import scan_log
    try:
        d = _date.fromisoformat(day) if day else None
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    return {"day": (d or _date.today()).isoformat(), "scans": scan_log(d)}


@router.post("/spreads/run")
def spreads_run():
    """Intraday mode: run the multi-timeframe scan now (market hours, Kite login). Evening mode:
    follow open spreads and build candidates from the latest stored option files."""
    from backend.options.spreads import run_now
    return run_now()


@router.post("/spreads/backtest")
def spreads_backtest(req: SpreadBacktestRequest):
    from backend.options.spreads import backtest
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    result = backtest(req.start_date, req.end_date, req.symbols, req.split_date, strategies=req.strategies,
                      condor_in_unstable=req.condor_in_unstable, min_credit_ratio_single=req.min_credit_ratio_single,
                      debit_below_vix=req.debit_below_vix)
    result["trades"] = result["trades"][-300:]
    return result


class CreditCompareRequest(BaseModel):
    start_date: date
    end_date: date
    ratios: list[float] = Field(default_factory=lambda: [0.10, 0.12, 0.15], min_length=2, max_length=5)
    split_date: date | None = None
    symbols: list[Literal["NIFTY", "BANKNIFTY", "SENSEX"]] | None = None


@router.post("/spreads/compare-credit")
def spreads_compare_credit(req: CreditCompareRequest):
    """Asked 26 Nov 2026: the live rules at several minimum credits for bull put / bear call
    spreads, with costs, side by side — and the trades a lower minimum adds."""
    from backend.options.spread_research import compare_min_credit
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    if any(not 0 < r < 1 for r in req.ratios):
        raise HTTPException(status_code=400, detail="ratios must be between 0 and 1")
    return compare_min_credit(req.start_date, req.end_date, req.ratios, req.split_date, req.symbols)


class TriggerVariant(BaseModel):
    name: str = Field(max_length=60)
    pullback_touch_atr: float = Field(gt=0, le=3)
    pullback_bars: int = Field(ge=1, le=30)
    regime_confirm_days: int | None = Field(default=None, ge=1, le=10)
    adx_trend_exit: float | None = Field(default=None, gt=0, lt=60)
    slow_trend_filter: bool = False
    use_invalidation: bool = True


class TriggerBacktestRequest(BaseModel):
    start_date: date
    end_date: date
    split_date: date | None = None
    variants: list[TriggerVariant] | None = Field(default=None, max_length=6)
    preset: Literal["pullback", "research"] = "pullback"   # used when no variants are given


@router.post("/spreads/trigger-backtest")
def spreads_trigger_backtest(req: TriggerBacktestRequest):
    """Asked 27 Nov 2026: replay the intraday entry trigger with other 1H pullback settings."""
    from backend.options import mtf_backtest
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    variants = [v.model_dump(exclude_defaults=True) | {"name": v.name, "pullback_touch_atr": v.pullback_touch_atr,
                                                       "pullback_bars": v.pullback_bars}
                for v in req.variants] if req.variants else mtf_backtest.PRESETS[req.preset]
    return mtf_backtest.start_job(req.start_date, req.end_date, variants, req.split_date)


@router.get("/spreads/trigger-backtest")
def spreads_trigger_backtest_status():
    from backend.options import mtf_backtest
    return mtf_backtest.status()


# --- research: strategy eligibility, exits, out-of-sample (asked 24 Nov 2026) --------------------

class ResearchRequest(BaseModel):
    start_date: date
    end_date: date
    split_date: date
    symbols: list[Literal["NIFTY", "BANKNIFTY", "SENSEX"]] | None = None
    min_credit_ratio_single: float | None = Field(None, ge=0, le=1)
    costs: bool = True


@router.post("/spreads/research")
def spreads_research(req: ResearchRequest):
    """Experiments A (exits), B (strategy per regime) and C (combined, unseen) in the background."""
    from backend.options import spread_research
    if not (req.start_date < req.split_date < req.end_date):
        raise HTTPException(400, "need start < split < end")
    return spread_research.start(req.model_dump(mode="json"))


@router.get("/spreads/research")
def spreads_research_status():
    from backend.options import spread_research
    return spread_research.status()


@router.get("/spreads/research/{run_id}")
def spreads_research_get(run_id: int, trades: bool = False):
    from backend.options import spread_research
    rec = spread_research.get(run_id)
    if not rec:
        raise HTTPException(404, "no such research run")
    if rec.get("result") and not trades:
        rec["result"] = {k: v for k, v in rec["result"].items() if k != "trades"}
    return rec


@router.get("/spreads/research/{run_id}/trades.csv")
def spreads_research_csv(run_id: int, which: Literal["baseline", "all_strategies"] = "baseline"):
    from fastapi.responses import Response
    from backend.options import spread_research
    text = spread_research.trades_csv(run_id, which)
    if text is None:
        raise HTTPException(404, "no finished research run with that id")
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="spread-research-{run_id}-{which}.csv"'})
