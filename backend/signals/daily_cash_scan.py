"""Shared daily scan for the cash pattern books (reversal, VCP), after the close.

Same flow as the Wolfe Wave scan it generalises: NIFTY data and RBI/Budget checks for the first
``event_block_days`` of the hold, per stock data quality, turnover and results checks, then the
book's ``candidate_fn``; rows are saved as a scan run, every trackable row is journaled, the top
``shortlist_per_scan`` go to the AI panel (unless the book is a shadow strategy, then they are
only tracked). A stock already held in any cash book is not shortlisted.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from backend.decision.config import get_config
from backend.signals.selection import choose_shortlist, open_positions
from backend.signals.setups import stage

# one stock is never held in two cash books at once
CASH_BOOKS = ("cash_swing", "cup_handle", "wolfe", "cash_reversal", "vcp")


def run_daily_scan(segment: str, setup: str, cfg: dict, candidate_fn: Callable, title: str,
                   now: datetime | None = None, force: bool = False,
                   results_fetch: Callable[[str], date | None] | None = None) -> dict:
    from backend.journal.store import add_records as journal_add
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.signals import events
    from backend.signals.cash_swing import (IST, _finish_run, _save, _start_run, avg_turnover, data_quality,
                                            ensure_scan_tables, expected_last_daily_bar, min_score)
    from backend.signals.regime import current_regime
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import is_trading_day

    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    today = now.date()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": f"{title} disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if not is_trading_day(today) and not force:
        return {"status": "skipped", "reason": "not an NSE trading day"}

    signal_date = expected_last_daily_bar(now)
    run_id = _start_run(signal_date.isoformat(), now, segment=segment)
    hold = events.trading_days_after(signal_date, cfg["hold_days"])
    first, last = hold[0], hold[-1]
    block_last = hold[min(cfg["event_block_days"], len(hold)) - 1]
    if not cfg["inst"].time_stop:   # target / stop / trailing stop; safety exit after max_hold_trading_days
        last = events.trading_days_after(signal_date, cfg["inst"].max_hold_trading_days)[-1]
    nifty = spot_instrument("NIFTY")
    nifty_bars = get_candles(nifty["instrument_token"], "day", limit=800) if nifty else []
    problem = data_quality(nifty_bars, signal_date, need_volume=False) if nifty else "NIFTY instrument missing"
    if problem:
        return _finish_run(run_id, "blocked", {"reason": f"NIFTY data: {problem}"})
    market = events.market_events_between(first, block_last, kinds=("RBI", "BUDGET"))
    if not market["verified"]:
        return _finish_run(run_id, "blocked", {"reason": "event calendar not verified for the entry window"})
    if market["events"]:
        names = ", ".join(f"{e['event']} {e['date']}" for e in market["events"])
        return _finish_run(run_id, "blocked", {"reason": f"event in the first {cfg['event_block_days']} days: {names}"})
    regime = current_regime(nifty_bars[-300:])["regime"]
    from backend.marketdata.cash_universe import cash_universe, min_turnover_for
    universe = cash_universe(today)          # F&O stocks + the NIFTY 500 (26 Nov 2026)
    if not universe:
        return _finish_run(run_id, "blocked", {"reason": "F&O universe is empty (instrument master missing)"})
    events.refresh_results([u["symbol"] for u in universe], fetch=results_fetch, today=today)

    rows = []
    for stock in universe:
        symbol = stock["symbol"]
        if not stock["spot_token"]:
            continue

        def reject(why: str) -> None:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": why,
                         "stages": [stage("eligibility", "fail", why)]})

        if stock.get("surveillance"):
            reject(f"on NSE surveillance (ASM/GSM): {stock['surveillance']}")
            continue
        bars = get_candles(stock["spot_token"], "day", limit=400)
        problem = data_quality(bars, signal_date, min_bars=cfg["min_bars"])
        if problem:
            reject(problem)
            continue
        turnover = avg_turnover(bars)
        floor = min_turnover_for(stock, cfg["min_turnover"])
        if turnover < floor:
            reject(f"turnover ₹{turnover / 1e7:.0f} cr < ₹{floor / 1e7:.0f} cr")
            continue
        ok, why = events.results_clear(symbol, first, block_last, today)
        if not ok:
            reject(why)
            continue
        c = candidate_fn(bars, cfg, regime, nifty_bars)
        c["stages"] = [stage("eligibility", "pass", turnover_cr=round(turnover / 1e7, 1),
                             universe="F&O" if stock.get("fno", True) else "NIFTY 500"), *c.get("stages", [])]
        rows.append({**c, "symbol": symbol, "token": stock["spot_token"], "turnover_cr": round(turnover / 1e7, 1)})

    ranked = sorted((r for r in rows if r.get("valid") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    held = {(segment, sym) for _, sym in open_positions(CASH_BOOKS)}
    shortlist = choose_shortlist(ranked, cfg["shortlist"], lambda r: segment, busy=held)
    for r in rows:
        if r.get("trackable"):
            sel = bool(r.get("shortlisted"))
            r["stages"] = [*r["stages"], stage("ai_panel", "pending" if sel else "not_sent"),
                           stage("final_approval", "pending" if sel else "not_applicable")]
    _save(run_id, signal_date.isoformat(), rows, segment=segment)
    journal_add([journal_row(r, segment, setup, signal_date, first, last, cfg["entry_window"][1])
                 for r in rows if r.get("trackable")])
    rejected: dict[str, int] = {}
    for r in rows:
        if not r.get("trackable"):
            key = (r.get("reject_reason") or "rejected").split(" (")[0]
            rejected[key] = rejected.get(key, 0) + 1
    from backend.journal.panel import review_after_scan
    panel = review_after_scan(now)
    return _finish_run(run_id, "ok", {
        "ai_panel": panel, "signal_date": signal_date.isoformat(), "entry_day": first.isoformat(),
        "time_stop": last.isoformat(), "regime": regime, "stocks": len(universe),
        "patterns": len([r for r in rows if r.get("trackable")]),
        "shortlisted": [r["symbol"] for r in shortlist],
        "skipped_open_trade": [r["symbol"] for r in ranked if r.get("selection_skip")],
        "top_reject_reasons": dict(sorted(rejected.items(), key=lambda kv: -kv[1])[:5]),
        "shadow": get_config().global_.is_shadow(segment, setup),
        "rule_version": get_config().rule_version,
    })


def journal_row(r: dict, segment: str, setup: str, signal_date: date, first: date, last: date,
                fill_until: str) -> dict:
    from backend.signals.cash_swing import min_score
    decision = "shortlisted" if r.get("shortlisted") else "not_selected" if r["score"] >= min_score() \
        else "below_score"
    return {
        "segment": segment, "symbol": r["symbol"], "instrument_token": r["token"], "exchange": "NSE",
        "signal_date": signal_date.isoformat(), "setup": r.get("setup") or setup,
        "direction": r.get("direction") or "long",
        "regime": r.get("regime"), "score": r.get("score"), "factors": r.get("factors"),
        "soft_checks": r.get("soft_checks"), "decision": decision, "reject_reason": r.get("selection_skip"),
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": r.get("atr"),
        "entry_date": first.isoformat(), "fill_until": f"{first.isoformat()}T{fill_until}",
        "time_stop_date": last.isoformat(),
        "extra": {"pattern": r.get("pattern"), "turnover_cr": r.get("turnover_cr"), "token": r.get("token"),
                  "daily_stage": r.get("daily_stage"), "next_level": r.get("next_level")},
        "stages": r.get("stages"), "rule_version": r.get("rule_version"),
    }
