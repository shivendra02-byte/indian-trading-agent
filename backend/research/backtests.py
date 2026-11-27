"""Backtests of research strategies: one background run at a time, stored in
``research_backtests`` (summary, optional settings grid, trades)."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from backend.db import get_db

_lock = threading.Lock()
_running: dict = {"id": None}
MAX_TRADES_STORED = 20000


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_backtests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                finished_at TEXT,
                status TEXT NOT NULL,
                progress TEXT,
                params TEXT,
                summary TEXT,
                grid TEXT,
                trades TEXT
            )""")
        # a run left 'running' by a restart can't finish any more
        conn.execute("UPDATE research_backtests SET status = 'interrupted' WHERE status = 'running' AND id != ?",
                     (_running["id"] if _running["id"] is not None else -1,))


def _update(run_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE research_backtests SET {cols} WHERE id = ?", (*fields.values(), run_id))


def run_sync(strategy: str, params: dict, run_id: int | None = None) -> dict:
    """Run now in this thread (tests, or called by the background thread)."""
    from backend.research.registry import get
    from backend.research.stats import summary
    entry = get(strategy)
    progress = (lambda msg: _update(run_id, progress=msg[:200])) if run_id else None
    result = entry["backtest"](params, progress)
    trades = result.get("trades") or []
    index_bars = []
    try:
        from backend.research.data import daily_bars
        index_bars = daily_bars("NIFTY")
    except Exception:
        pass
    summ = summary(trades, params.get("split_date"), index_bars)
    summ["limitations"] = entry.get("limitations", [])
    for k in ("extra", "settings"):
        if result.get(k) is not None:
            summ[k] = result[k]
    return {"summary": summ, "grid": result.get("grid") or [], "trades": trades}


def start(strategy: str, params: dict) -> dict:
    ensure_table()
    with _lock:
        if _running["id"] is not None:
            return {"status": "busy", "running_id": _running["id"]}
        with get_db() as conn:
            cur = conn.execute("INSERT INTO research_backtests (strategy, status, params) VALUES (?, 'running', ?)",
                               (strategy, json.dumps(params, default=str)))
            run_id = cur.lastrowid
        _running["id"] = run_id

    def work():
        try:
            out = run_sync(strategy, params, run_id)
            _update(run_id, status="done", summary=json.dumps(out["summary"], default=str),
                    grid=json.dumps(out["grid"], default=str),
                    trades=json.dumps(out["trades"][-MAX_TRADES_STORED:], default=str),
                    finished_at=datetime.now().isoformat(sep=" ", timespec="seconds"), progress=None)
        except Exception as exc:   # stored on the run, never raised into the server
            _update(run_id, status="error", progress=f"{type(exc).__name__}: {exc}"[:300],
                    finished_at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        finally:
            _running["id"] = None

    threading.Thread(target=work, name=f"research-backtest-{run_id}", daemon=True).start()
    return {"status": "started", "id": run_id}


def _row(r, with_trades: bool = False) -> dict:
    item = dict(r)
    for f in ("params", "summary", "grid"):
        item[f] = json.loads(item[f]) if item.get(f) else None
    if with_trades:
        item["trades"] = json.loads(item["trades"]) if item.get("trades") else []
    else:
        item.pop("trades", None)
    return item


def list_runs(strategy: str | None = None, limit: int = 30) -> list[dict]:
    ensure_table()
    sql = "SELECT * FROM research_backtests" + (" WHERE strategy = ?" if strategy else "") + " ORDER BY id DESC LIMIT ?"
    with get_db() as conn:
        rows = conn.execute(sql, ((strategy, limit) if strategy else (limit,))).fetchall()
    return [_row(r) for r in rows]


def get_run(run_id: int, with_trades: bool = False) -> dict | None:
    ensure_table()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM research_backtests WHERE id = ?", (run_id,)).fetchone()
    return _row(r, with_trades) if r else None


def delete_run(run_id: int) -> bool:
    ensure_table()
    if _running["id"] == run_id:
        return False
    with get_db() as conn:
        conn.execute("DELETE FROM research_backtests WHERE id = ?", (run_id,))
    return True
