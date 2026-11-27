"""The research journal: what each research strategy would have traded (shadow only).

One table, ``research_trades``, separate from the live ``trade_journal`` so research results
never reach the live books' stats, limits, Today's P&L or the live post-mortem.

Strategies are re-run statelessly every evening over the stored bars (like the live journal
tracker): ``sync()`` replaces a strategy's rows from ``live_start`` with the fresh result and
keeps when each trade was first seen. A data repair can therefore change history; that is
on purpose — the journal always shows what the rules say on the bars we have.
"""

from __future__ import annotations

import json
from datetime import datetime

from backend.db import get_db

FIELDS = ("symbol", "direction", "entry_date", "entry_price", "stop", "target", "exit_date", "exit_price",
          "exit_reason", "status", "pnl_r", "pnl_rupees", "lots")


def ensure_tables() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                trade_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT,
                entry_date TEXT,
                entry_price REAL,
                stop REAL,
                exit_date TEXT,
                exit_price REAL,
                exit_reason TEXT,
                status TEXT NOT NULL,
                pnl_r REAL,
                pnl_rupees REAL,
                lots TEXT,
                legs TEXT,
                extra TEXT,
                first_seen TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE (strategy, trade_key)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_research_trades_strategy ON research_trades (strategy, status)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(research_trades)")}
        for col, typ in (("target", "REAL"), ("entry_ts", "TEXT"), ("exit_ts", "TEXT"), ("timeframe", "TEXT")):
            if col not in cols:      # added 27 Nov 2026 with the live (hourly) research runs
                conn.execute(f"ALTER TABLE research_trades ADD COLUMN {col} {typ}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                run_at TEXT DEFAULT (datetime('now', 'localtime')),
                status TEXT,
                detail TEXT
            )""")


def trade_key(t: dict) -> str:
    return t.get("key") or f"{t['symbol']}|{t.get('direction')}|{t.get('entry_date')}"


def sync(strategy: str, trades: list[dict], since: str, symbols: list[str] | None = None) -> dict:
    """Replace ``strategy``'s rows entered on/after ``since`` with ``trades`` (those entered
    on/after ``since``). ``symbols``: only replace rows of these instruments (an hourly run of one
    exchange leaves the other's rows alone). Returns counts."""
    ensure_tables()
    keep = [t for t in trades if str(t.get("entry_date") or "") >= since
            and (symbols is None or t["symbol"] in symbols)]
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with get_db() as conn:
        seen = {r["trade_key"]: r["first_seen"] for r in conn.execute(
            "SELECT trade_key, first_seen FROM research_trades WHERE strategy = ?", (strategy,))}
        if symbols is None:
            conn.execute("DELETE FROM research_trades WHERE strategy = ? AND entry_date >= ?", (strategy, since))
        else:
            for sym in symbols:
                conn.execute("DELETE FROM research_trades WHERE strategy = ? AND entry_date >= ? AND symbol = ?",
                             (strategy, since, sym))
        for t in keep:
            key = trade_key(t)
            conn.execute(
                """INSERT OR REPLACE INTO research_trades
                   (strategy, trade_key, symbol, direction, entry_date, entry_price, stop, target, exit_date,
                    exit_price, exit_reason, status, pnl_r, pnl_rupees, lots, legs, extra, first_seen, updated_at,
                    entry_ts, exit_ts, timeframe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (strategy, key, t["symbol"], t.get("direction"), t.get("entry_date"), t.get("entry_price"),
                 t.get("stop"), t.get("target"), t.get("exit_date"), t.get("exit_price"), t.get("exit_reason"),
                 t.get("status", "closed"), t.get("pnl_r"), t.get("pnl_rupees"),
                 json.dumps(t.get("lots")) if t.get("lots") is not None else None,
                 json.dumps(t.get("legs")) if t.get("legs") else None,
                 json.dumps(t.get("extra") or {}, default=str), seen.get(key, now), now,
                 t.get("entry_ts"), t.get("exit_ts"), t.get("timeframe")))
    return {"trades": len(keep), "open": sum(1 for t in keep if t.get("status") == "open"),
            "new_keys": [trade_key(t) for t in keep if trade_key(t) not in seen][:20],
            "new": sum(1 for t in keep if trade_key(t) not in seen)}


def _row(r) -> dict:
    item = dict(r)
    for f in ("legs", "extra", "lots"):
        if item.get(f):
            try:
                item[f] = json.loads(item[f])
            except (TypeError, ValueError):
                pass
    return item


def list_trades(strategy: str | None = None, status: str | None = None, limit: int = 2000) -> list[dict]:
    ensure_tables()
    where, params = [], []
    if strategy:
        where.append("strategy = ?")
        params.append(strategy)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM research_trades" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY COALESCE(exit_date, '9999') DESC, entry_date DESC LIMIT ?"
    with get_db() as conn:
        return [_row(r) for r in conn.execute(sql, (*params, int(limit)))]


def closed_between(first: str, last: str) -> list[dict]:
    ensure_tables()
    with get_db() as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM research_trades WHERE status = 'closed' AND exit_date >= ? AND exit_date <= ? "
            "ORDER BY exit_date", (first, last))]


def log_run(strategy: str, status: str, detail: dict) -> None:
    ensure_tables()
    with get_db() as conn:
        conn.execute("INSERT INTO research_runs (strategy, status, detail) VALUES (?, ?, ?)",
                     (strategy, status, json.dumps(detail, default=str)[:4000]))
        conn.execute("DELETE FROM research_runs WHERE id NOT IN (SELECT id FROM research_runs ORDER BY id DESC LIMIT 500)")


def last_runs(limit: int = 20) -> list[dict]:
    ensure_tables()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item["detail"] or "{}")
        except ValueError:
            pass
        out.append(item)
    return out
