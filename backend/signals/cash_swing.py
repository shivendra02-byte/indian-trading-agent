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
from backend.decision.config import get_config
from backend.marketdata.candles import bars_for_timeframe, get_candles
from backend.marketdata.instruments import fno_universe, spot_instrument
from backend.signals import events
from backend.signals.regime import current_regime
from backend.signals.selection import choose_shortlist
from backend.signals.setups import evaluate, labelled_reasons, min_score, stage
from backend.system_state import is_paused
from tradingagents.utils.market_calendar import IST, holiday_name, is_trading_day, previous_trading_day

SEGMENT = "cash_swing"
DAILY_BARS = 300


def settings() -> dict:
    """Cash swing settings from config/decision_engine.yaml (instruments.cash_swing)."""
    inst = get_config().instrument(SEGMENT)
    extra = inst.model_extra or {}
    h, m = (int(x) for x in extra.get("scan_after", "15:45").split(":"))
    return {
        "inst": inst,
        "enabled": inst.enabled,
        "scan_after": time(h, m),
        "hold_days": inst.hold_trading_days,
        "min_turnover": float(extra.get("min_turnover_cr", 50)) * 1e7,
        "max_stop_pct": float(extra.get("max_stop_pct", 8.0)) / 100,
        "shortlist": inst.shortlist_per_scan,
        "entry_window": extra.get("entry_window", ["09:30", "11:00"]),
        "min_bars": inst.min_primary_bars,
    }


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


def data_quality(bars: list[dict], expected_last: date, min_bars: int = 250,
                 need_volume: bool = True) -> str | None:
    """None if OK, else the reason. Checks freshness, gaps in the last 20 days, volume and depth.

    ``need_volume=False`` for index spot (NIFTY, BANKNIFTY ...): Kite returns volume 0 for
    indices, so a volume check there would block every scan."""
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
    if need_volume and not bars[-1].get("volume"):
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
                                                     "rs_vs_nifty_pct", "risk_mult", "turnover_cr", "pattern",
                                                     "penalties", "tradingsymbol", "lots", "oi_change_pct",
                                                     "or_high", "or_low", "stop_type", "range_atr", "token")}))
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


def _journal_row(r: dict, signal_date: date, first: date, last: date, fill_until: str = "11:00") -> dict:
    if not r.get("valid"):
        decision = "soft_rejected"
    elif r.get("shortlisted"):
        decision = "shortlisted"
    elif r["score"] >= min_score():
        decision = "not_selected"
    else:
        decision = "below_score"
    return {
        "segment": SEGMENT, "symbol": r["symbol"], "instrument_token": r["token"], "exchange": "NSE",
        "signal_date": signal_date.isoformat(), "setup": r["setup"], "direction": r["direction"],
        "regime": r.get("regime"), "score": r.get("score"), "factors": r.get("factors"),
        "soft_checks": r.get("soft_checks"), "decision": decision, "reject_reason": r.get("reject_reason"),
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": r.get("atr"),
        "entry_date": first.isoformat(), "fill_until": f"{first.isoformat()}T{fill_until}",
        "time_stop_date": last.isoformat(),
        "extra": {k: r.get(k) for k in ("stop_atr", "next_level", "vol_ratio", "htf_trend", "rs_vs_nifty_pct",
                                         "risk_mult", "turnover_cr", "confirmations")},
        "stages": r.get("stages"), "rule_version": r.get("rule_version"),
    }


def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    today = now.date()
    # Outcome tracking is pure Python on stored candles: it runs even while paused.
    tracked = track_open_records()
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "cash swing disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if not is_trading_day(today):
        if not force:
            return {"status": "skipped", "reason": "not an NSE trading day"}
        # "Run now" on a holiday/weekend (asked 24 Nov 2026): scan the last session's daily bars
        # (entries are for the next session) unless that session's scan already ran.
        last = expected_last_daily_bar(now).isoformat()
        done = _last_ok_run(last)
        if done:
            return {"status": "skipped", "reason": f"market closed today ({holiday_name(today) or 'weekend'}); "
                    f"the scan for {last} already ran at {done['started_at'][11:16]} — results below",
                    "signal_date": last, "run_id": done["id"]}
    if now.time() < cfg["scan_after"] and not force:
        return {"status": "skipped", "reason": f"before {cfg['scan_after']:%H:%M}"}

    signal_date = expected_last_daily_bar(now)
    run_id = _start_run(signal_date.isoformat(), now)
    window = events.trading_days_after(signal_date, cfg["hold_days"])
    first, last = window[0], window[-1]          # events/results are checked over this window
    inst = cfg["inst"]
    # No time stop (agreed 23 Nov 2026): exit at target / stop / trailing stop; safety exit after
    # max_hold_trading_days (shares have no expiry).
    exit_by = last if inst.time_stop else events.trading_days_after(signal_date, inst.max_hold_trading_days)[-1]

    # Market-level checks — fail closed for the whole scan.
    nifty = spot_instrument("NIFTY")
    nifty_bars = get_candles(nifty["instrument_token"], "day", limit=DAILY_BARS) if nifty else []
    problem = data_quality(nifty_bars, signal_date, need_volume=False) if nifty else "NIFTY instrument missing"
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

    inst = cfg["inst"]
    rows: list[dict] = []

    def reject(symbol: str, reason: str) -> None:
        rows.append({"symbol": symbol, "valid": False, "reject_reason": reason,
                     "stages": [stage("eligibility", "fail", reason)]})

    for stock in universe:
        symbol = stock["symbol"]
        if not stock["spot_token"]:
            reject(symbol, "no NSE spot instrument")
            continue
        bars = get_candles(stock["spot_token"], "day", limit=DAILY_BARS)
        problem = data_quality(bars, signal_date, min_bars=cfg["min_bars"])
        if problem:
            reject(symbol, problem)
            continue
        turnover = avg_turnover(bars)
        if turnover < cfg["min_turnover"]:
            reject(symbol, f"turnover ₹{turnover / 1e7:.0f} cr < ₹{cfg['min_turnover'] / 1e7:.0f} cr")
            continue
        ok, why = events.results_clear(symbol, first, last, today)
        if not ok:
            reject(symbol, why)
            continue
        htf = bars_for_timeframe(stock["spot_token"], inst.scored_htf or "week", "NSE")
        confirmations = {tf: bars_for_timeframe(stock["spot_token"], tf, "NSE") for tf in inst.confirmation_tfs}
        eligibility = stage("eligibility", "pass", turnover_cr=round(turnover / 1e7, 1))
        candidates = evaluate(bars, htf, regime["regime"], directions=tuple(inst.directions),
                              is_stock=True, benchmark=nifty_bars, max_stop_pct=cfg["max_stop_pct"],
                              confirmations=confirmations, min_primary_bars=cfg["min_bars"],
                              stock_regime=current_regime(bars)["regime"])
        for c in candidates:
            c["stages"] = [eligibility, *c.get("stages", [])]
        trackable = [c for c in candidates if c.get("trackable")]
        if trackable:
            for c in trackable:
                rows.append({**c, "symbol": symbol, "token": stock["spot_token"],
                             "turnover_cr": round(turnover / 1e7, 1)})
        else:
            reasons = labelled_reasons(candidates)
            rows.append({"symbol": symbol, "valid": False, "regime": regime["regime"], "reject_reason": reasons,
                         "stages": candidates[0].get("stages") if len(candidates) == 1 else [eligibility]})

    ranked = sorted((r for r in rows if r.get("valid") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    shortlist = choose_shortlist(ranked, cfg["shortlist"], lambda r: SEGMENT)
    for r in rows:
        if r.get("trackable"):
            selected = bool(r.get("shortlisted"))
            r["stages"] = [*r["stages"], stage("ai_panel", "pending" if selected else "not_sent"),
                           stage("final_approval", "pending" if selected else "not_applicable")]
    _save(run_id, signal_date.isoformat(), rows)
    journal_add([_journal_row(r, signal_date, first, exit_by, cfg["entry_window"][1])
                 for r in rows if r.get("trackable")])
    from backend.journal.panel import review_after_scan
    panel = review_after_scan(now)    # AI review + Telegram alerts for the new shortlist
    return _finish_run(run_id, "ok", {
        "ai_panel": panel,
        "signal_date": signal_date.isoformat(), "entry_window": first.isoformat(), "time_stop": exit_by.isoformat(),
        "regime": regime, "stocks": len(universe), "valid_setups": sum(1 for r in rows if r.get("valid")),
        "above_60": len(ranked), "shortlisted": [r["symbol"] for r in shortlist],
        "skipped_open_trade": [r["symbol"] for r in ranked if r.get("selection_skip")],
        "journal_records": sum(1 for r in rows if r.get("trackable")), "tracking": tracked,
        "rule_version": get_config().rule_version,
    })


def _last_ok_run(signal_date: str, segment: str = SEGMENT) -> dict | None:
    ensure_scan_tables()
    with get_db() as conn:
        row = conn.execute("SELECT id, started_at FROM scan_runs WHERE segment = ? AND signal_date = ? "
                           "AND status = 'ok' ORDER BY id DESC LIMIT 1", (segment, signal_date)).fetchone()
    return dict(row) if row else None


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
