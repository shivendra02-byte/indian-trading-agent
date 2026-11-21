"""Trade journal: one row per trackable candidate (valid or soft-rejected).

Every record is followed bar by bar whether or not it becomes a paper trade,
so rejected ideas get outcomes too ("shadow" trades). ``decision`` records how
far the idea got:
  soft_rejected  — failed a soft rule (regime permission, volume, RSI turn)
  below_score    — all rules passed, score < 60
  not_selected   — 60+ but not in the top N for the AI panel
  shortlisted    — sent to the AI panel (Phase 4 sets approved / rejected_panel)
  approved / rejected_panel
``is_paper`` is set only for panel-approved trades.
"""

from __future__ import annotations

import json
from typing import Any

from backend.db import get_db

JSON_FIELDS = ("factors", "soft_checks", "panel", "trail", "extra", "stages")


def _migrate(conn) -> None:
    """v1 (cash swing only) had no signal_ts and was unique per day; rebuild it so
    hourly scans can record several signals per day."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_journal)")]
    if cols and "signal_ts" not in cols:
        conn.execute("DROP INDEX IF EXISTS idx_journal_track")
        conn.execute("DROP INDEX IF EXISTS idx_journal_signal")
        conn.execute("ALTER TABLE trade_journal RENAME TO trade_journal_v1")


def _copy_v1(conn) -> None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_journal_v1'").fetchone()
    if not exists:
        return
    old = [r[1] for r in conn.execute("PRAGMA table_info(trade_journal_v1)")]
    shared = [c for c in old if c != "id"]
    conn.execute(f"INSERT OR IGNORE INTO trade_journal ({', '.join(shared)}, signal_ts) "
                 f"SELECT {', '.join(shared)}, signal_date || 'T15:30:00' FROM trade_journal_v1")
    conn.execute("DROP TABLE trade_journal_v1")


def ensure_journal_table() -> None:
    with get_db() as conn:
        _migrate(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                segment TEXT NOT NULL,
                symbol TEXT NOT NULL,
                instrument_token INTEGER NOT NULL,
                exchange TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                signal_ts TEXT NOT NULL,
                setup TEXT NOT NULL,
                direction TEXT NOT NULL,
                regime TEXT,
                score REAL,
                factors TEXT,
                soft_checks TEXT,
                decision TEXT NOT NULL,
                reject_reason TEXT,
                panel TEXT,
                panel_decision TEXT,
                is_paper INTEGER NOT NULL DEFAULT 0,
                entry REAL NOT NULL,
                stop REAL NOT NULL,
                target REAL NOT NULL,
                rr REAL,
                atr REAL,
                entry_date TEXT NOT NULL,
                fill_until TEXT NOT NULL,
                time_stop_date TEXT NOT NULL,
                track_status TEXT NOT NULL DEFAULT 'pending',
                no_fill_reason TEXT,
                fill_ts TEXT,
                fill_price REAL,
                exit_ts TEXT,
                exit_price REAL,
                exit_reason TEXT,
                pnl_r REAL,
                mfe_r REAL,
                mae_r REAL,
                qty INTEGER,
                charges REAL,
                pnl_rupees REAL,
                trail TEXT,
                extra TEXT,
                stages TEXT,
                rule_version TEXT,
                updated_at TEXT,
                UNIQUE (segment, symbol, signal_ts, setup, direction)
            );
            CREATE INDEX IF NOT EXISTS idx_journal_track ON trade_journal (track_status);
            CREATE INDEX IF NOT EXISTS idx_journal_signal ON trade_journal (segment, signal_date);
        """)
        _copy_v1(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_journal)")]
        for col in ("stages", "rule_version"):
            if col not in cols:
                conn.execute(f"ALTER TABLE trade_journal ADD COLUMN {col} TEXT")


def add_records(rows: list[dict]) -> int:
    ensure_journal_table()
    cols = ("segment", "symbol", "instrument_token", "exchange", "signal_date", "signal_ts", "setup", "direction", "regime",
            "score", "factors", "soft_checks", "decision", "reject_reason", "entry", "stop", "target", "rr", "atr",
            "entry_date", "fill_until", "time_stop_date", "extra", "stages", "rule_version")
    values = []
    for r in rows:
        r = {**r, "signal_ts": r.get("signal_ts") or f"{r['signal_date']}T15:30:00"}
        values.append(tuple(json.dumps(r.get(c)) if c in JSON_FIELDS and r.get(c) is not None else r.get(c)
                            for c in cols))
    with get_db() as conn:
        cur = conn.executemany(
            f"INSERT OR IGNORE INTO trade_journal ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)
        return cur.rowcount


def _decode(row) -> dict:
    item = dict(row)
    for f in JSON_FIELDS:
        if item.get(f):
            try:
                item[f] = json.loads(item[f])
            except ValueError:
                pass
    return item


def list_records(where: str = "1=1", params: tuple = (), limit: int = 500) -> list[dict]:
    ensure_journal_table()
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM trade_journal WHERE {where} ORDER BY signal_date DESC, score DESC LIMIT ?",
                            (*params, limit)).fetchall()
    return [_decode(r) for r in rows]


def update_record(record_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = datetime('now', 'localtime')"
    values = [json.dumps(v) if k in JSON_FIELDS and v is not None else v for k, v in fields.items()]
    with get_db() as conn:
        conn.execute(f"UPDATE trade_journal SET {sets} WHERE id = ?", (*values, record_id))
