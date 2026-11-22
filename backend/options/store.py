"""SQLite tables and queries for end-of-day option data."""

from __future__ import annotations

from datetime import date

from backend.db import get_db

OPTION_COLUMNS = (
    "trade_date", "exchange", "symbol", "expiry", "strike", "opt_type", "open", "high", "low", "close",
    "settle", "prev_close", "underlying", "oi", "chg_oi", "volume", "turnover", "lot_size",
    "days_to_expiry", "forward", "forward_source", "iv", "delta", "gamma", "theta", "vega",
)


def ensure_option_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS option_eod (
                trade_date TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                expiry TEXT NOT NULL,
                strike REAL NOT NULL,
                opt_type TEXT NOT NULL,          -- CE / PE / FUT
                open REAL, high REAL, low REAL, close REAL, settle REAL, prev_close REAL,
                underlying REAL,                 -- spot index close
                oi REAL, chg_oi REAL, volume REAL, turnover REAL, lot_size REAL,
                days_to_expiry INTEGER,
                forward REAL, forward_source TEXT,
                iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
                PRIMARY KEY (trade_date, symbol, expiry, strike, opt_type)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_option_eod_contract ON option_eod (symbol, expiry, strike, opt_type, trade_date);

            CREATE TABLE IF NOT EXISTS option_files (
                trade_date TEXT NOT NULL,
                exchange TEXT NOT NULL,
                status TEXT NOT NULL,            -- ok / missing / error
                rows INTEGER,
                source TEXT,                     -- download / upload
                detail TEXT,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (trade_date, exchange)
            );

            CREATE TABLE IF NOT EXISTS india_vix (
                trade_date TEXT PRIMARY KEY,
                open REAL, high REAL, low REAL, close REAL NOT NULL
            );
        """)


def save_day(trade_date: date, exchange: str, rows: list[dict], source: str) -> int:
    """Replace one exchange's rows for a day (idempotent) and mark the file ok."""
    day = trade_date.isoformat()
    values = [tuple([day, exchange] + [row.get(c) for c in OPTION_COLUMNS[2:]]) for row in rows]
    placeholders = ", ".join("?" for _ in OPTION_COLUMNS)
    with get_db() as conn:
        conn.execute("DELETE FROM option_eod WHERE trade_date = ? AND exchange = ?", (day, exchange))
        conn.executemany(
            f"INSERT OR REPLACE INTO option_eod ({', '.join(OPTION_COLUMNS)}) VALUES ({placeholders})", values)
    record_file(trade_date, exchange, "ok", rows=len(values), source=source)
    return len(values)


def record_file(trade_date: date, exchange: str, status: str, rows: int | None = None,
                source: str = "download", detail: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO option_files (trade_date, exchange, status, rows, source, detail, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
            (trade_date.isoformat(), exchange, status, rows, source, (detail or None) and detail[:300]),
        )


def file_status(trade_date: date, exchange: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT status FROM option_files WHERE trade_date = ? AND exchange = ?",
                           (trade_date.isoformat(), exchange)).fetchone()
    return row["status"] if row else None


def save_vix(rows: list[dict]) -> int:
    with get_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO india_vix (trade_date, open, high, low, close) VALUES (?, ?, ?, ?, ?)",
            [(r["trade_date"], r.get("open"), r.get("high"), r.get("low"), r["close"]) for r in rows],
        )
    return len(rows)


def vix_on(day: str) -> float | None:
    """India VIX close on ``day``, or the last close before it."""
    with get_db() as conn:
        row = conn.execute("SELECT close FROM india_vix WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                           (day,)).fetchone()
    return row["close"] if row else None


def summary() -> dict:
    with get_db() as conn:
        files = conn.execute("""
            SELECT exchange, status, COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last,
                   SUM(COALESCE(rows, 0)) AS rows
            FROM option_files GROUP BY exchange, status
        """).fetchall()
        recent = conn.execute(
            "SELECT trade_date, exchange, status, rows, source, detail, fetched_at FROM option_files "
            "ORDER BY trade_date DESC, exchange LIMIT 12").fetchall()
        vix = conn.execute("SELECT COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last FROM india_vix"
                           ).fetchone()
        latest_vix = conn.execute("SELECT trade_date, close FROM india_vix ORDER BY trade_date DESC LIMIT 1").fetchone()
    exchanges: dict[str, dict] = {}
    for row in files:
        entry = exchanges.setdefault(row["exchange"], {"ok": 0, "missing": 0, "error": 0, "first": None,
                                                       "last": None, "rows": 0})
        entry[row["status"]] = row["n"]
        if row["status"] == "ok":
            entry.update(first=row["first"], last=row["last"], rows=row["rows"])
    return {
        "exchanges": exchanges,
        "recent_files": [dict(r) for r in recent],
        "vix": {"days": vix["n"], "first": vix["first"], "last": vix["last"],
                "latest": dict(latest_vix) if latest_vix else None},
    }


def latest_date(symbol: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM option_eod WHERE symbol = ? AND opt_type != 'FUT'",
                           (symbol,)).fetchone()
    return row["d"] if row else None


def expiries(symbol: str, day: str) -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT expiry FROM option_eod WHERE symbol = ? AND trade_date = ? AND opt_type != 'FUT' "
            "ORDER BY expiry", (symbol, day)).fetchall()
    return [r["expiry"] for r in rows]


def chain(symbol: str, day: str, expiry: str) -> list[dict]:
    """Option rows of one expiry on one day (calls and puts), by strike."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM option_eod WHERE symbol = ? AND trade_date = ? AND expiry = ? AND opt_type != 'FUT' "
            "ORDER BY strike, opt_type", (symbol, day, expiry)).fetchall()
    return [dict(r) for r in rows]


def contract_history(symbol: str, expiry: str, strike: float, opt_type: str,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    """Daily rows of one option contract (for the spread backtest)."""
    sql = "SELECT * FROM option_eod WHERE symbol = ? AND expiry = ? AND strike = ? AND opt_type = ?"
    args: list = [symbol, expiry, strike, opt_type]
    if start:
        sql += " AND trade_date >= ?"
        args.append(start)
    if end:
        sql += " AND trade_date <= ?"
        args.append(end)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY trade_date", args).fetchall()]
