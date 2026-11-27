"""Research section API: shadow strategies from the research papers, their journal,
backtests, history download, weekly AI post-mortem and the lab studies."""

from __future__ import annotations

import threading
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/research", tags=["research"])


def _entry(key: str) -> dict:
    from backend.research.registry import get
    try:
        return get(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown research strategy {key}")


@router.get("/")
def research_overview():
    from backend.research.journal import last_runs, list_trades
    from backend.research.registry import all_entries
    from backend.research.stats import stats
    from backend.research import config as rcfg
    out = []
    runs = last_runs(200)
    for key, e in all_entries().items():
        rows = list_trades(key, limit=100_000) if e["kind"] == "trades" else []
        last = next((r for r in runs if r["strategy"] == key), None)
        out.append({"key": key, "label": e["label"], "enabled": e["enabled"], "page": e["page"], "kind": e["kind"],
                    "record": stats(rows) if rows else {"n": 0},
                    "open": sum(1 for r in rows if r["status"] == "open"),
                    "last_run": last})
    cfg = rcfg.load()
    return {"strategies": out, "live_start": rcfg.live_start(), "capital": cfg.get("capital"),
            "risk_pct": cfg.get("risk_pct"), "cost_bps": cfg.get("cost_bps")}


@router.get("/journal")
def research_journal(strategy: str | None = None, status: Literal["open", "closed"] | None = None,
                     limit: int = 1000):
    from backend.research.journal import list_trades
    from backend.research.stats import summary
    rows = list_trades(strategy, status, limit=min(max(limit, 1), 5000))
    return {"trades": rows, "summary": summary(rows)}


@router.get("/strategy/{key}")
def research_strategy(key: str):
    e = _entry(key)
    from backend.research.journal import list_trades
    from backend.research.stats import summary
    rows = list_trades(key, limit=5000) if e["kind"] == "trades" else []
    status = e["status"]() if e.get("status") else {}
    return {"key": key, "label": e["label"], "enabled": e["enabled"], "kind": e["kind"],
            "limitations": e.get("limitations", []), "status": status,
            "trades": rows, "summary": summary(rows) if rows else None}


@router.post("/strategy/{key}/run")
def research_run(key: str):
    """Re-run one strategy over the stored bars now and update its journal."""
    e = _entry(key)
    from backend.research.journal import log_run
    from backend.system_state import is_paused
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    res = e["live"](None)
    log_run(key, res.get("status", "ok"), {k: v for k, v in res.items() if k != "states"})
    return res


@router.post("/run-all")
def research_run_all(group: Literal["nse", "mcx"] = "nse"):
    from backend.research.registry import run_live
    return run_live(group)


# --- backtests ----------------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    strategy: str
    start_date: date
    end_date: date
    split_date: date | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    grid: dict[str, list[Any]] = Field(default_factory=dict)


@router.post("/backtests")
def research_backtest(req: BacktestRequest):
    e = _entry(req.strategy)
    if e["kind"] != "trades" and req.strategy != "risk_switch":
        raise HTTPException(status_code=400, detail="this research item has no backtest")
    if req.end_date <= req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    if sum(len(v) for v in req.grid.values()) > 24:
        raise HTTPException(status_code=400, detail="grid too large (at most 24 values in total)")
    from backend.research.backtests import start
    return start(req.strategy, req.model_dump(mode="json"))


@router.get("/backtests")
def research_backtests(strategy: str | None = None):
    from backend.research.backtests import list_runs
    return {"runs": list_runs(strategy)}


@router.get("/backtests/{run_id}")
def research_backtest_get(run_id: int, trades: bool = False):
    from backend.research.backtests import get_run
    run = get_run(run_id, with_trades=trades)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.delete("/backtests/{run_id}")
def research_backtest_delete(run_id: int):
    from backend.research.backtests import delete_run
    if not delete_run(run_id):
        raise HTTPException(status_code=409, detail="run is still running")
    return {"status": "deleted"}


# --- history ------------------------------------------------------------------------------------

_history_lock = threading.Lock()


@router.get("/history")
def research_history():
    from backend.research.data import PROGRESS, history_status
    return {"series": history_status(), "progress": dict(PROGRESS)}


@router.post("/history")
def research_history_download():
    """Download long daily history (MCX continuous + index spot) in the background."""
    from backend.brokers.kite import KiteConfigError, get_authenticated_client
    from backend.research.data import PROGRESS, download_history
    if PROGRESS.get("status") == "running":
        return {"status": "busy", "progress": dict(PROGRESS)}
    try:
        client = get_authenticated_client()
    except KiteConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Kite is not logged in: {exc}")

    def work():
        with _history_lock:
            try:
                download_history(client)
            except Exception as exc:
                PROGRESS.update(status="error", message=f"{type(exc).__name__}: {exc}"[:200])

    PROGRESS.update(status="running", done=0, total=None, current="starting")
    threading.Thread(target=work, name="research-history", daemon=True).start()
    return {"status": "started"}


# --- post-mortem --------------------------------------------------------------------------------

class PostmortemRequest(BaseModel):
    week_start: date | None = None
    with_ai: bool = True


@router.get("/postmortem")
def research_postmortems():
    from backend.research.postmortem import list_reports
    return {"reports": list_reports()}


@router.get("/postmortem/{week}")
def research_postmortem_get(week: date):
    from backend.research.postmortem import get_report, week_bounds
    report = get_report(week_bounds(week)[0].isoformat())
    if not report:
        raise HTTPException(status_code=404, detail="no report for that week")
    return report


@router.post("/postmortem/run")
def research_postmortem_run(req: PostmortemRequest):
    from backend.research.postmortem import run
    return run(req.week_start, with_ai=req.with_ai)


# --- lab ----------------------------------------------------------------------------------------

@router.get("/lab/trend-diagnostic")
def research_trend_diagnostic(include_stocks: bool = True):
    from backend.research.lab import trend_diagnostic
    return trend_diagnostic(include_stocks=include_stocks)


# --- risk switch (information only) -------------------------------------------------------------

class StudyRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None


@router.get("/risk-switch")
def research_risk_switch(days: int = 120):
    from backend.research.risk_switch import current
    return current(min(max(days, 10), 750))


@router.post("/risk-switch/study")
def research_risk_switch_study(req: StudyRequest):
    from backend.research.risk_switch import run_study
    return run_study(req.start_date.isoformat() if req.start_date else None,
                     req.end_date.isoformat() if req.end_date else None)


# --- win-probability model ----------------------------------------------------------------------

class WinModelRequest(BaseModel):
    source: Literal["journal", "backtest"] = "journal"
    run_id: int | None = None
    min_train: int = Field(default=300, ge=100, le=5000)


@router.get("/lab/win-model/sources")
def research_win_model_sources():
    from backend.research.win_model import sources
    return sources()


@router.post("/lab/win-model")
def research_win_model(req: WinModelRequest):
    from backend.research.win_model import run
    if req.source == "backtest" and not req.run_id:
        raise HTTPException(status_code=400, detail="choose a backtest run")
    return run(req.source, req.run_id, req.min_train)
