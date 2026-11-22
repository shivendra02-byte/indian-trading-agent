"""Ideas tracker: trade ideas you see elsewhere (X posts, chats, your own chart
reading), tracked like shadow trades so their record can be judged by source
and by pattern.

An idea is: symbol, long/short, how it is entered (next open, at a price or
better, or on a break of a price), the level that makes it wrong, an optional
target, the source and a pattern tag. It is followed on daily bars from the
day after the idea: filled within ``FILL_DAYS``; then -1R at the invalid level
(checked first, also on the fill day), the target if given, otherwise the
close after ``horizon_days``. Prices come from the stored candles when the
symbol is an F&O stock or index, else from Yahoo Finance (``SYMBOL.NS``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db

FILL_DAYS = 5
DEFAULT_HORIZON = 40
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
ENTRY_TYPES = ("market", "limit", "breakout")
PATTERNS = ("trendline_bounce", "channel", "triangle_breakout", "trendline_break", "support_resistance",
            "divergence", "breakout", "pullback", "reversal", "other")
YAHOO_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN", "FINNIFTY": "NIFTY_FIN_SERVICE.NS"}
OUTCOME_FIELDS = ("status", "fill_date", "fill_price", "exit_date", "exit_price", "exit_reason", "result_r",
                  "mfe_r", "mae_r", "hit_1r", "hit_2r", "last_checked")


def ensure_table() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                idea_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,              -- long / short
                entry_type TEXT NOT NULL,             -- market / limit / breakout
                entry REAL, invalid REAL NOT NULL, target REAL,
                horizon_days INTEGER NOT NULL,
                source TEXT, pattern TEXT, note TEXT,
                status TEXT NOT NULL DEFAULT 'waiting', -- waiting / open / closed / not_filled / invalid / no_data
                fill_date TEXT, fill_price REAL, exit_date TEXT, exit_price REAL, exit_reason TEXT,
                result_r REAL, mfe_r REAL, mae_r REAL, hit_1r INTEGER, hit_2r INTEGER, last_checked TEXT
            );
        """)


def validate(data: dict) -> dict:
    symbol = str(data.get("symbol") or "").strip().upper().removesuffix(".NS")
    if not SYMBOL_RE.match(symbol):
        raise ValueError("symbol: letters, digits, & and - only (e.g. UNOMINDA, M&M, NIFTY)")
    direction = data.get("direction")
    if direction not in ("long", "short"):
        raise ValueError("direction must be long or short")
    entry_type = data.get("entry_type") or ("limit" if data.get("entry") else "market")
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"entry_type must be one of {', '.join(ENTRY_TYPES)}")
    entry = data.get("entry")
    if entry_type != "market" and not entry:
        raise ValueError("an entry price is needed for limit and breakout entries")
    invalid, target = data.get("invalid"), data.get("target")
    if not invalid or invalid <= 0:
        raise ValueError("the invalid level (stop) is required")
    long = direction == "long"
    if entry:
        if (invalid >= entry) if long else (invalid <= entry):
            raise ValueError(f"the invalid level must be {'below' if long else 'above'} the entry")
        if target and ((target <= entry) if long else (target >= entry)):
            raise ValueError(f"the target must be {'above' if long else 'below'} the entry")
    idea_date = data.get("idea_date") or date.today().isoformat()
    date.fromisoformat(str(idea_date))
    horizon = int(data.get("horizon_days") or DEFAULT_HORIZON)
    if not 3 <= horizon <= 250:
        raise ValueError("horizon_days must be 3-250")
    pattern = data.get("pattern") or "other"
    if pattern not in PATTERNS:
        raise ValueError(f"pattern must be one of {', '.join(PATTERNS)}")
    return {"idea_date": str(idea_date), "symbol": symbol, "direction": direction, "entry_type": entry_type,
            "entry": entry if entry_type != "market" else None, "invalid": invalid, "target": target or None,
            "horizon_days": horizon, "source": (data.get("source") or "").strip()[:80] or "unknown",
            "pattern": pattern, "note": (data.get("note") or "").strip()[:500] or None}


def add_idea(data: dict, fetch=None) -> dict:
    ensure_table()
    row = validate(data)
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO ideas ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                           list(row.values()))
        idea_id = cur.lastrowid
    refresh_ideas(ids=[idea_id], fetch=fetch)
    return get_idea(idea_id)


def get_idea(idea_id: int) -> dict | None:
    ensure_table()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    return dict(row) if row else None


def delete_idea(idea_id: int) -> bool:
    ensure_table()
    with get_db() as conn:
        return conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,)).rowcount > 0


def list_ideas(limit: int = 500) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM ideas ORDER BY idea_date DESC, id DESC LIMIT ?", (limit,))]


# --- prices ---------------------------------------------------------------------------------

def _stored_bars(symbol: str, since: date) -> list[dict] | None:
    try:
        from backend.marketdata.candles import get_candles
        from backend.marketdata.instruments import spot_instrument
        inst = spot_instrument(symbol)
    except Exception:
        return None
    if not inst:
        return None
    bars = [b for b in get_candles(inst["instrument_token"], "day", limit=1000) if str(b["ts"])[:10] > since.isoformat()]
    return bars or None


def _yahoo_bars(symbol: str, since: date) -> list[dict]:
    import yfinance as yf
    ticker = YAHOO_INDEX.get(symbol, f"{symbol}.NS")
    frame = yf.Ticker(ticker).history(start=(since + timedelta(days=1)).isoformat(), interval="1d", auto_adjust=False)
    bars = []
    for ts, r in frame.iterrows():
        if r.get("Close") != r.get("Close"):
            continue
        bars.append({"ts": ts.date().isoformat(), "open": float(r["Open"]), "high": float(r["High"]),
                     "low": float(r["Low"]), "close": float(r["Close"])})
    return bars


def daily_bars(symbol: str, since: date) -> list[dict]:
    """Daily bars after ``since``: stored candles first, else Yahoo Finance."""
    return _stored_bars(symbol, since) or _yahoo_bars(symbol, since)


# --- outcome --------------------------------------------------------------------------------

def evaluate(idea: dict, bars: list[dict]) -> dict:
    """Pure: the idea's state given the daily bars after its date."""
    long = idea["direction"] == "long"
    entry, invalid, target = idea.get("entry"), idea["invalid"], idea.get("target")
    fill_i = fill = None
    for i, b in enumerate(bars[:FILL_DAYS]):
        if idea["entry_type"] == "market":
            fill_i, fill = i, b["open"]
        elif idea["entry_type"] == "limit":
            if (b["low"] <= entry) if long else (b["high"] >= entry):
                fill_i, fill = i, (min(b["open"], entry) if long else max(b["open"], entry))
        elif (b["high"] >= entry) if long else (b["low"] <= entry):          # breakout
            fill_i, fill = i, (max(b["open"], entry) if long else min(b["open"], entry))
        if fill_i is not None:
            break
    if fill_i is None:
        return {"status": "not_filled" if len(bars) >= FILL_DAYS else "waiting"}
    risk = (fill - invalid) if long else (invalid - fill)
    if risk <= 0:
        return {"status": "invalid", "fill_date": str(bars[fill_i]["ts"])[:10], "fill_price": round(fill, 2),
                "exit_reason": "filled beyond the invalid level"}
    base = {"fill_date": str(bars[fill_i]["ts"])[:10], "fill_price": round(fill, 2)}
    mfe = mae = 0.0
    hit_1r = False
    last = min(len(bars) - 1, fill_i + idea["horizon_days"])
    for t in range(fill_i, last + 1):
        b = bars[t]
        if (b["low"] <= invalid) if long else (b["high"] >= invalid):
            if t > fill_i:      # a gap through the level exits at the open
                exit_price = min(b["open"], invalid) if long else max(b["open"], invalid)
            else:
                exit_price = invalid
            r = ((exit_price - fill) if long else (fill - exit_price)) / risk
            return {**base, "status": "closed", "exit_date": str(b["ts"])[:10], "exit_price": round(exit_price, 2),
                    "exit_reason": "invalid", "result_r": round(r, 2), "mfe_r": round(mfe, 2), "mae_r": 1.0,
                    "hit_1r": hit_1r, "hit_2r": False}
        fav = (b["high"] - fill) if long else (fill - b["low"])
        adv = (fill - b["low"]) if long else (b["high"] - fill)
        mfe, mae = max(mfe, fav / risk), max(mae, adv / risk)
        hit_1r = hit_1r or mfe >= 1
        if target and t > fill_i and ((b["high"] >= target) if long else (b["low"] <= target)):
            exit_price = max(b["open"], target) if long else min(b["open"], target)
            r = ((exit_price - fill) if long else (fill - exit_price)) / risk
            return {**base, "status": "closed", "exit_date": str(b["ts"])[:10], "exit_price": round(exit_price, 2),
                    "exit_reason": "target", "result_r": round(r, 2), "mfe_r": round(mfe, 2), "mae_r": round(mae, 2),
                    "hit_1r": hit_1r or r >= 1, "hit_2r": r >= 2}
    if last - fill_i < idea["horizon_days"]:
        return {**base, "status": "open", "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), "hit_1r": hit_1r}
    close = bars[last]["close"]
    r = ((close - fill) if long else (fill - close)) / risk
    return {**base, "status": "closed", "exit_date": str(bars[last]["ts"])[:10], "exit_price": round(close, 2),
            "exit_reason": "time", "result_r": round(r, 2), "mfe_r": round(mfe, 2), "mae_r": round(mae, 2),
            "hit_1r": hit_1r, "hit_2r": mfe >= 2}


def refresh_ideas(ids: list[int] | None = None, fetch: Callable[[str, date], list[dict]] | None = None) -> dict:
    """Re-evaluate waiting/open ideas (or the given ids) from daily bars."""
    ensure_table()
    fetch = fetch or daily_bars
    with get_db() as conn:
        if ids:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM ideas WHERE id IN ({', '.join('?' for _ in ids)})", ids)]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM ideas WHERE status IN ('waiting', 'open', 'no_data')")]
    counts: dict[str, int] = {}
    for idea in rows:
        try:
            bars = fetch(idea["symbol"], date.fromisoformat(idea["idea_date"]))
            result = evaluate(idea, bars) if bars else {"status": "no_data" if
                                                        date.fromisoformat(idea["idea_date"]) < date.today() - timedelta(days=7)
                                                        else "waiting"}
        except Exception:
            result = {"status": "no_data"}
        values = {f: result.get(f) for f in OUTCOME_FIELDS}
        values["last_checked"] = datetime.now().isoformat(timespec="seconds")
        for flag in ("hit_1r", "hit_2r"):
            if values[flag] is not None:
                values[flag] = int(bool(values[flag]))
        with get_db() as conn:
            conn.execute(f"UPDATE ideas SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                         [*values.values(), idea["id"]])
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {"status": "ok", "checked": len(rows), "counts": counts}


# --- record ---------------------------------------------------------------------------------

def record(rows: list[dict] | None = None) -> dict:
    from backend.signals.divergence import _summary
    rows = rows if rows is not None else list_ideas(limit=5000)
    closed = [r for r in rows if r["status"] == "closed"]

    def grouped(key):
        out: dict[str, list[dict]] = {}
        for r in closed:
            out.setdefault(r.get(key) or "unknown", []).append(r)
        return {k: _summary(v) for k, v in sorted(out.items())}
    return {"all": _summary(closed), "by_source": grouped("source"), "by_pattern": grouped("pattern"),
            "by_direction": grouped("direction"),
            "counts": {s: sum(1 for r in rows if r["status"] == s)
                       for s in ("waiting", "open", "closed", "not_filled", "invalid", "no_data")}}


def week_summary(start: date, end: date) -> dict:
    """For the weekly post-mortem: ideas closed this week and the record so far by source."""
    rows = list_ideas(limit=5000)
    week = [r for r in rows if r["status"] == "closed" and r.get("exit_date")
            and start.isoformat() <= r["exit_date"] <= end.isoformat()]
    rec = record(rows)
    return {"closed_this_week": [{k: r.get(k) for k in ("symbol", "direction", "source", "pattern", "exit_reason",
                                                         "result_r")} for r in week],
            "record_by_source": rec["by_source"], "record_by_pattern": rec["by_pattern"], "all": rec["all"]}
