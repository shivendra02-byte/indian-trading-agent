"""Weekly AI post-mortem of the research strategies (one AI call a week, separate from the
live post-mortem). Saturday from 08:30 (Sunday catches up); never changes any rule."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

from backend.db import get_db
from backend.research import config as rcfg
from backend.research.journal import closed_between, list_trades
from backend.research.stats import stats

log = logging.getLogger(__name__)
RUN_AT = time(8, 30)


def week_bounds(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def due(now: datetime, last_done: date | None) -> date | None:
    """Week start to review at ``now``: Saturday from 08:30 or Sunday, once per week."""
    wd = now.weekday()
    if wd < 5 or (wd == 5 and now.time() < RUN_AT):
        return None
    start, _ = week_bounds(now.date())
    return None if last_done == start else start


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_postmortems (
                week_start TEXT PRIMARY KEY,
                week_end TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                summary TEXT,
                ai_status TEXT,
                ai_model TEXT,
                ai_review TEXT
            )""")


def exists(week_start: date) -> bool:
    ensure_table()
    full_from = datetime.combine(week_start + timedelta(days=5), RUN_AT).isoformat(sep=" ")
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM research_postmortems WHERE week_start = ? AND created_at >= ?",
                            (week_start.isoformat(), full_from)).fetchone() is not None


def _latest_backtest(strategy: str) -> dict | None:
    from backend.research.backtests import list_runs
    for run in list_runs(strategy, limit=10):
        if run["status"] == "done" and run.get("summary"):
            s = run["summary"]
            return {"id": run["id"], "params": run.get("params"), "all": s.get("all"),
                    "by_period": s.get("by_period"), "bear": s.get("bear")}
    return None


def build_summary(start: date, end: date) -> dict:
    from backend.research.registry import all_entries
    min_trades = int((rcfg.load().get("postmortem") or {}).get("min_trades", 30))
    week_closed = closed_between(start.isoformat(), end.isoformat())
    out: dict = {"week": [start.isoformat(), end.isoformat()], "live_start": rcfg.live_start(),
                 "min_trades": min_trades, "strategies": {}}
    for key, e in all_entries().items():
        if e["kind"] != "trades":
            continue
        rows = list_trades(key, limit=100_000)
        out["strategies"][key] = {
            "label": e["label"], "enabled": e["enabled"],
            "this_week": stats([t for t in week_closed if t["strategy"] == key], min_trades),
            "since_live_start": stats(rows, min_trades),
            "open_positions": [{k: t.get(k) for k in ("symbol", "direction", "entry_date", "entry_price", "pnl_r")}
                               for t in rows if t["status"] == "open"][:20],
            "latest_backtest": _latest_backtest(key),
        }
    try:
        from backend.research.risk_switch import history
        out["risk_switch"] = history(start.isoformat(), end.isoformat())
    except Exception:
        out["risk_switch"] = None
    out["closed_this_week"] = len(week_closed)
    return out


def _prompt(summary: dict) -> str:
    return (
        "You are reviewing one week of RESEARCH strategies for an Indian F&O / MCX / cash trader. They are "
        "shadow only: no real or paper money, no alerts; each re-runs its rules over daily bars and records what "
        "it would have traded. Results are in R (1R = 1% of ₹10 lakh). The strategies come from research papers: "
        "slow trend following (EWMA 20/250 'American' system and time-series momentum, Sepp & Lucic 2026), "
        "pairs trading with graph matching (Qureshi & Zaman 2024) and GOLDM/SILVERM, Clenow cash momentum, and a "
        "NIFTY drawdown-risk switch (Berzal & García). 'latest_backtest' is the stored history test; 'bear' is the "
        "result in NIFTY's worst quarters.\n\n"
        "Write a short review in markdown with exactly these headings:\n"
        "## This week\n## Record so far vs backtest\n## What to watch\n## Proposed experiments\n\n"
        "Rules:\n"
        f"- Any strategy with fewer than {summary.get('min_trades', 30)} trades: say 'too early to judge' and draw "
        "no conclusion from its live record; you may compare it with its backtest.\n"
        "- Trend strategies lose often and win rarely; judge them on R, drawdown and the bear-quarter result, not "
        "win rate.\n"
        "- At most 3 proposed experiments, each naming the research.yaml key, the new value and the backtest to "
        "run. Never suggest moving a research strategy to live money; that is the owner's decision after enough "
        "trades.\n"
        "- Plain words, numbers with units, under 400 words.\n\n"
        f"SUMMARY (JSON):\n{json.dumps(summary, default=str)[:24000]}\n"
    )


def ai_review(summary: dict, llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    from backend.system_state import is_paused
    if is_paused():
        return {"status": "skipped: system paused (run again after resuming)"}
    if not any((s.get("since_live_start") or {}).get("n") or s.get("open_positions") or s.get("latest_backtest")
               for s in summary["strategies"].values()):
        return {"status": "skipped: no research trades or backtests yet"}
    try:
        if llm_call is None:
            from backend.journal.postmortem import _default_llm_call as llm_call
        text, model = llm_call(_prompt(summary))
    except Exception as exc:
        log.warning("research post-mortem AI review failed: %s", exc)
        return {"status": f"error: {type(exc).__name__}"}
    return {"status": "ok", "model": model, "review": text}


def run(week_start: date | None = None, with_ai: bool = True,
        llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    start, end = week_bounds(week_start or date.today())
    summary = build_summary(start, end)
    ai = ai_review(summary, llm_call) if with_ai else {"status": "skipped: not requested"}
    ensure_table()
    with get_db() as conn:
        conn.execute("""INSERT OR REPLACE INTO research_postmortems
                        (week_start, week_end, created_at, summary, ai_status, ai_model, ai_review)
                        VALUES (?, ?, datetime('now', 'localtime'), ?, ?, ?, ?)""",
                     (start.isoformat(), end.isoformat(), json.dumps(summary, default=str), ai.get("status"),
                      ai.get("model"), ai.get("review")))
    return {"status": "ok", "week_start": start.isoformat(), "closed": summary["closed_this_week"],
            "ai_status": ai["status"]}


def list_reports(limit: int = 52) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        rows = conn.execute("SELECT week_start, week_end, created_at, ai_status FROM research_postmortems "
                            "ORDER BY week_start DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_report(week_start: str) -> dict | None:
    ensure_table()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM research_postmortems WHERE week_start = ?", (week_start,)).fetchone()
    if not r:
        return None
    item = dict(r)
    item["summary"] = json.loads(item["summary"]) if item.get("summary") else None
    return item
