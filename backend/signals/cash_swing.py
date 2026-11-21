"""Daily cash swing scan (F&O-list stocks, delivery, long only).

Runs once per NSE trading day after the daily candle is final (~15:45).
Each stock passes the hard filters (data quality, liquidity, events/results),
then the regime gate and setups A/B/C (long only), stop/target geometry and the
100-point score. Every stock gets at least one row in ``scan_candidates`` (valid
setups, or the reason it was rejected). The top 2 valid setups scoring 60+ are
shortlisted for the AI panel. Signals are for entry on the next session.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Callable

from backend.db import get_db
from backend.journal.store import add_records as journal_add
from backend.journal.tracker import track_open_records
from backend.marketdata.candles import get_candles, resample_weekly
from backend.marketdata.instruments import fno_universe, spot_instrument
from backend.signals import events
from backend.signals.regime import current_regime
from backend.signals.setups import SHORTLIST_SCORE, evaluate
from backend.system_state import is_paused
from tradingagents.utils.market_calendar import IST, is_trading_day, previous_trading_day

SEGMENT = "cash_swing"
SCAN_AFTER = time(15, 45)
HOLD_DAYS = 7
MIN_TURNOVER = 50e7          # ₹50 crore 20-day average
MAX_STOP_PCT = 0.08
SHORTLIST_SIZE = 2
DAILY_BARS = 300


def ensure_scan_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS scan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                segment TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup TEXT,
                direction TEXT,
                valid INTEGER NOT NULL,
                reject_reason TEXT,
                score REAL,
                factors TEXT,
                entry REAL,
                stop REAL,
                target REAL,
                rr REAL,
                regime TEXT,
                shortlisted INTEGER NOT NULL DEFAULT 0,
                extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scan_candidates_run ON scan_candidates (run_id);
            CREATE INDEX IF NOT EXISTS idx_scan_candidates_day ON scan_candidates (segment, signal_date);
        """)


def expected_last_daily_bar(now: datetime) -> date:
    today = now.date()
    if is_trading_day(today) and now.time() >= time(15, 30):
        return today
    return previous_trading_day(today)


def data_quality(bars: list[dict], expected_last: date, min_bars: int = 250) -> str | None:
    """None if OK, else the reason. Checks freshness, gaps in the last 20 days, volume and depth."""
    if len(bars) < min_bars:
        return f"only {len(bars)} daily bars (need {min_bars})"
    last = date.fromisoformat(bars[-1]["ts"][:10])
    if last != expected_last:
        return f"stale data: last daily bar {last.isoformat()}, expected {expected_last.isoformat()}"
    days, d = [], expected_last
    while len(days) < 20:
        if is_trading_day(d):
            days.append(d.isoformat())
        d = date.fromordinal(d.toordinal() - 1)
    have = {b["ts"][:10] for b in bars[-40:]}
    missing = [x for x in days if x not in have]
    if missing:
        return f"missing daily bars ({len(missing)} of the last 20 trading days)"
    if not bars[-1].get("volume"):
        return "no volume on the last bar"
    return None


def avg_turnover(bars: list[dict], n: int = 20) -> float:
    window = bars[-n:]
    return sum(b["close"] * (b.get("volume") or 0) for b in window) / len(window)


def _save(run_id: int, signal_date: str, rows: list[dict], segment: str = SEGMENT) -> None:
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO scan_candidates (run_id, segment, signal_date, symbol, setup, direction, valid,
               reject_reason, score, factors, entry, stop, target, rr, regime, shortlisted, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, segment, signal_date, r["symbol"], r.get("setup"), r.get("direction"),
                 int(bool(r.get("valid"))), r.get("reject_reason"), r.get("score"),
                 json.dumps(r["factors"]) if r.get("factors") else None, r.get("entry"), r.get("stop"),
                 r.get("target"), r.get("rr"), r.get("regime"), int(bool(r.get("shortlisted"))),
                 json.dumps({k: r.get(k) for k in ("stop_atr", "next_level", "vol_ratio", "htf_trend",
                                                     "rs_vs_nifty_pct", "risk_mult", "turnover_cr")}))
                for r in rows
            ],
        )


def _start_run(signal_date: str, now: datetime, segment: str = SEGMENT) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO scan_runs (segment, signal_date, started_at, status) VALUES (?, ?, ?, 'running')",
            (segment, signal_date, now.isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def _finish_run(run_id: int, status: str, detail: dict) -> dict:
    with get_db() as conn:
        conn.execute("UPDATE scan_runs SET status = ?, detail = ? WHERE id = ?",
                     (status, json.dumps(detail), run_id))
    return {"run_id": run_id, "status": status, **detail}


def _journal_row(r: dict, signal_date: date, first: date, last: date) -> dict:
    if not r.get("valid"):
        decision = "soft_rejected"
    elif r.get("shortlisted"):
        decision = "shortlisted"
    elif r["score"] >= SHORTLIST_SCORE:
        decision = "not_selected"
    else:
        decision = "below_score"
    return {
        "segment": SEGMENT, "symbol": r["symbol"], "instrument_token": r["token"], "exchange": "NSE",
        "signal_date": signal_date.isoformat(), "setup": r["setup"], "direction": r["direction"],
        "regime": r.get("regime"), "score": r.get("score"), "factors": r.get("factors"),
        "soft_checks": r.get("soft_checks"), "decision": decision, "reject_reason": r.get("reject_reason"),
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": r.get("atr"),
        "entry_date": first.isoformat(), "fill_until": f"{first.isoformat()}T11:00",
        "time_stop_date": last.isoformat(),
        "extra": {k: r.get(k) for k in ("stop_atr", "next_level", "vol_ratio", "htf_trend", "rs_vs_nifty_pct",
                                         "risk_mult", "turnover_cr")},
    }


def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    today = now.date()
    # Outcome tracking is pure Python on stored candles: it runs even while paused.
    tracked = track_open_records()
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if not is_trading_day(today):
        return {"status": "skipped", "reason": "not an NSE trading day"}
    if now.time() < SCAN_AFTER and not force:
        return {"status": "skipped", "reason": "before 15:45"}

    signal_date = expected_last_daily_bar(now)
    run_id = _start_run(signal_date.isoformat(), now)
    window = events.trading_days_after(signal_date, HOLD_DAYS)
    first, last = window[0], window[-1]

    # Market-level checks — fail closed for the whole scan.
    nifty = spot_instrument("NIFTY")
    nifty_bars = get_candles(nifty["instrument_token"], "day", limit=DAILY_BARS) if nifty else []
    problem = data_quality(nifty_bars, signal_date) if nifty else "NIFTY instrument missing"
    if problem:
        return _finish_run(run_id, "blocked", {"reason": f"NIFTY data: {problem}"})
    market = events.market_events_between(first, last, kinds=("RBI", "BUDGET"))
    if not market["verified"]:
        return _finish_run(run_id, "blocked", {"reason": "event calendar not verified for the holding window"})
    if market["events"]:
        names = ", ".join(f"{e['event']} {e['date']}" for e in market["events"])
        return _finish_run(run_id, "blocked", {"reason": f"event inside the holding window: {names}"})
    regime = current_regime(nifty_bars)

    universe = fno_universe(today)
    if not universe:
        return _finish_run(run_id, "blocked", {"reason": "F&O universe is empty (instrument master missing)"})
    events.refresh_results([u["symbol"] for u in universe], fetch=results_fetch, today=today)

    rows: list[dict] = []
    for stock in universe:
        symbol = stock["symbol"]
        if not stock["spot_token"]:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": "no NSE spot instrument"})
            continue
        bars = get_candles(stock["spot_token"], "day", limit=DAILY_BARS)
        problem = data_quality(bars, signal_date)
        if problem:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": problem})
            continue
        turnover = avg_turnover(bars)
        if turnover < MIN_TURNOVER:
            rows.append({"symbol": symbol, "valid": False,
                         "reject_reason": f"turnover ₹{turnover / 1e7:.0f} cr < ₹50 cr"})
            continue
        ok, why = events.results_clear(symbol, first, last, today)
        if not ok:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": why})
            continue
        candidates = evaluate(bars, resample_weekly(bars), regime["regime"], directions=("long",),
                              is_stock=True, benchmark=nifty_bars, max_stop_pct=MAX_STOP_PCT)
        trackable = [c for c in candidates if c.get("trackable")]
        if trackable:
            for c in trackable:
                rows.append({**c, "symbol": symbol, "token": stock["spot_token"],
                             "turnover_cr": round(turnover / 1e7, 1)})
        else:
            reasons = "; ".join(sorted({c["reject_reason"] for c in candidates if c.get("reject_reason")}))
            rows.append({"symbol": symbol, "valid": False, "regime": regime["regime"], "reject_reason": reasons})

    ranked = sorted((r for r in rows if r.get("valid") and r["score"] >= SHORTLIST_SCORE),
                    key=lambda r: r["score"], reverse=True)
    for r in ranked[:SHORTLIST_SIZE]:
        r["shortlisted"] = True
    _save(run_id, signal_date.isoformat(), rows)
    journal_add([_journal_row(r, signal_date, first, last) for r in rows if r.get("trackable")])
    return _finish_run(run_id, "ok", {
        "signal_date": signal_date.isoformat(), "entry_window": first.isoformat(), "time_stop": last.isoformat(),
        "regime": regime, "stocks": len(universe), "valid_setups": sum(1 for r in rows if r.get("valid")),
        "above_60": len(ranked), "shortlisted": [r["symbol"] for r in ranked[:SHORTLIST_SIZE]],
        "journal_records": sum(1 for r in rows if r.get("trackable")), "tracking": tracked,
    })


def latest_run(signal_date: str | None = None, segment: str = SEGMENT) -> dict | None:
    ensure_scan_tables()
    with get_db() as conn:
        if signal_date:
            run = conn.execute("SELECT * FROM scan_runs WHERE segment = ? AND signal_date = ? ORDER BY id DESC LIMIT 1",
                               (segment, signal_date)).fetchone()
        else:
            run = conn.execute("SELECT * FROM scan_runs WHERE segment = ? ORDER BY id DESC LIMIT 1",
                               (segment,)).fetchone()
        if not run:
            return None
        cands = conn.execute(
            """SELECT * FROM scan_candidates WHERE run_id = ?
               ORDER BY shortlisted DESC, valid DESC, score DESC, symbol""", (run["id"],)).fetchall()
    out = dict(run)
    out["detail"] = json.loads(out["detail"] or "{}")
    out["candidates"] = []
    for c in cands:
        item = dict(c)
        item["factors"] = json.loads(item["factors"]) if item["factors"] else None
        item["extra"] = json.loads(item["extra"]) if item["extra"] else {}
        out["candidates"].append(item)
    return out
