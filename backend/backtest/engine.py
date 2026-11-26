"""Point-in-time backtester for the rule engine.

Replays a rule set (``current`` or a file in config/experiments/) over stored
history, using only data available at each signal time, and runs every
candidate through the same outcome simulator as the live journal.

Segments:
* ``cash_swing`` — daily signals at the close, long only; entry window on the
  next session's hourly bars, then daily bars; scored HTF and confirmations as configured.
* ``stock_futures_proxy`` — hourly signals on the **spot** stock (expired futures
  contracts can't be downloaded from Kite), long and short, confirmations and
  daily context as configured.

Higher-timeframe bars (2H, 4H, weekly) are rebuilt as they were at the signal
time: completed bars plus the current partial bar, never later data.
"""

from __future__ import annotations

import bisect
import json
import re
import threading
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import available_rule_sets, exit_rule_for, get_config, instrument_or_live, use_config
from backend.journal.learning import stats
from backend.journal.simulate import simulate
from backend.marketdata.candles import get_candles, resample_hours, resample_weekly
from backend.marketdata.instruments import fno_universe, spot_instrument
from backend.signals import events
from backend.signals.cash_swing import avg_turnover
from backend.signals.regime import current_regime, label_series, regime_at
from backend.signals.setups import _relative_strength_points, evaluate
from tradingagents.utils.market_calendar import NSE, is_trading_day

SEGMENTS = ("cash_swing", "cup_handle", "wolfe", "cash_reversal", "vcp", "stock_futures_proxy", "orb_proxy",
            "index_futures_proxy", "nifty_options_proxy")
_COMMON = [
    "Portfolio limits are not applied: every candidate is counted on its own.",
    "Only RBI and Budget dates are checked for market-wide events.",
]
_SURVIVORSHIP = "Universe is today's F&O list: stocks removed from F&O in the past are missing (survivorship bias)."
_SURVIVORSHIP_CASH = ("Universe is today's F&O list plus today's NIFTY 500: stocks that left either list in the past "
                      "are missing, so results look better than they really were (survivorship bias).")
CASH_BACKTESTS = ("cash_swing", "cup_handle", "wolfe", "cash_reversal", "vcp")


def _floor(stock: dict, default: float) -> float:
    """₹ turnover floor: the strategy's own for F&O stocks, the NIFTY 500 floor for the others."""
    from backend.marketdata.cash_universe import min_turnover_for
    return min_turnover_for(stock, default)
LIMITATIONS = {
    "cash_swing": [
        _SURVIVORSHIP_CASH,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts (about 12 months back) the entry uses the daily bar and the 4H "
        "confirmation is not tested (shown as 'daily only').",
        *_COMMON],
    "cup_handle": [
        _SURVIVORSHIP_CASH,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts the entry uses the daily bar (shown as 'daily only').",
        *_COMMON],
    "wolfe": [
        _SURVIVORSHIP_CASH,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts the entry uses the daily bar (shown as 'daily only').",
        "Exit: this book's own exit_management (breakeven, then ATR trail), not the global one.",
        *_COMMON],
    "cash_reversal": [
        _SURVIVORSHIP_CASH,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts the entry uses the daily bar (shown as 'daily only').",
        "Daily stage from the stock's own stored daily bars, point in time.",
        *_COMMON],
    "vcp": [
        _SURVIVORSHIP_CASH,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts the entry uses the daily bar (shown as 'daily only').",
        *_COMMON],
    "stock_futures_proxy": [
        _SURVIVORSHIP,
        "Futures are approximated by the spot stock, because expired contracts cannot be downloaded.",
        "No historical F&O ban list, bid-ask spreads, open interest or company results dates, so those "
        "filters are not applied.",
        *_COMMON],
    "orb_proxy": [
        "Universe is the most liquid F&O stocks by today's turnover (survivorship bias).",
        "Stock futures are approximated by the spot stock (10-minute bars), because expired contracts cannot "
        "be downloaded; no open interest, F&O ban list, bid-ask spreads or results dates.",
        "10-minute history covers about 24 months.",
        *_COMMON],
    "nifty_options_proxy": [
        "Results are in R on the NIFTY index (5-minute bars): the option's premium can't be replayed, because "
        "expired options' intraday history can't be downloaded. Premium decay, IV changes and the bid-ask "
        "spread are not modelled, so option buying will do worse than these R figures.",
        "The index has no volume: the 'VWAP' is a time-weighted session average.",
        "Weekly expiry day taken as Thursday before 1 Sep 2025 and Tuesday from then on.",
        "5-minute history covers about 24 months.",
        *_COMMON],
    "index_futures_proxy": [
        "NIFTY and BANKNIFTY futures are approximated by the index, because expired contracts cannot be "
        "downloaded.",
        "The index has no volume and futures open interest history isn't available: volume and OI are not "
        "tested and are left out of the score.",
        "No bid-ask spreads, so the liquidity filter is not applied.",
        *_COMMON],
}
_run_lock = threading.Lock()


# --- storage ---------------------------------------------------------------------------

def ensure_tables() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rule_backtests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                segment TEXT NOT NULL,
                rule_set TEXT NOT NULL,
                rule_version TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                progress TEXT,
                summary TEXT,
                trades TEXT,
                options TEXT
            )""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_backtests)")}
        if "options" not in cols:
            conn.execute("ALTER TABLE rule_backtests ADD COLUMN options TEXT")


def _update(run_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [json.dumps(v) if k in ("summary", "trades", "options") else v for k, v in fields.items()]
    with get_db() as conn:
        conn.execute(f"UPDATE rule_backtests SET {sets} WHERE id = ?", (*values, run_id))


def get_run(run_id: int, with_trades: bool = False) -> dict | None:
    ensure_tables()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM rule_backtests WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["summary"] = json.loads(out["summary"]) if out["summary"] else None
    out["options"] = json.loads(out["options"]) if out.get("options") else {}
    out["trades"] = json.loads(out["trades"]) if (with_trades and out["trades"]) else None
    if not with_trades:
        out.pop("trades")
    return out


def list_runs(limit: int = 30) -> list[dict]:
    ensure_tables()
    with get_db() as conn:
        rows = conn.execute("""SELECT id, created_at, segment, rule_set, rule_version, start_date, end_date,
                                      status, progress, summary, options FROM rule_backtests
                               ORDER BY id DESC LIMIT ?""",
                            (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        summary = json.loads(item.pop("summary") or "null") or {}
        item["headline"] = (summary.get("groups") or {}).get("shortlisted")
        item["trades"] = summary.get("trades")
        item["options"] = json.loads(item["options"]) if item.get("options") else {}
        out.append(item)
    return out


def delete_run(run_id: int) -> str:
    """'deleted', 'running' (refused: still in progress) or 'not_found'."""
    ensure_tables()
    with get_db() as conn:
        row = conn.execute("SELECT status FROM rule_backtests WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return "not_found"
        if row["status"] == "running":
            return "running"
        conn.execute("DELETE FROM rule_backtests WHERE id = ?", (run_id,))
    return "deleted"


def delete_finished_runs() -> int:
    """Delete every run that is not still running. Returns how many were deleted."""
    ensure_tables()
    with get_db() as conn:
        return conn.execute("DELETE FROM rule_backtests WHERE status != 'running'").rowcount


def recover_interrupted() -> None:
    """A restart kills the worker thread; mark its run so the UI doesn't spin forever."""
    ensure_tables()
    with get_db() as conn:
        conn.execute("UPDATE rule_backtests SET status = 'error', progress = 'interrupted by restart' "
                     "WHERE status = 'running'")


# --- point-in-time higher-timeframe views ------------------------------------------------

class IncrementalBars:
    """Higher-timeframe bars as they looked when source bar ``i`` had just closed.

    ``upto(i)`` = completed higher-timeframe bars before the one containing
    source bar ``i`` + that bar rebuilt from source bars up to ``i`` only."""

    def __init__(self, source: list[dict], resample: Callable[[list[dict]], list[dict]],
                 key: Callable[[dict], str]):
        self.source = source
        self.resample = resample
        self.full = resample(source)
        self.position = {b["ts"]: n for n, b in enumerate(self.full)}
        self.keys = [key(b) for b in source]
        self.first: dict[str, int] = {}
        for i, k in enumerate(self.keys):
            self.first.setdefault(k, i)

    def upto(self, i: int, limit: int = 300) -> list[dict]:
        if i < 0:
            return []
        k = self.keys[i]
        n = self.position.get(k)
        if n is None:
            return []
        partial = self.resample(self.source[self.first[k]:i + 1])
        return (self.full[max(0, n - limit + 1):n] + partial[-1:])[-limit:]


def _week_key(b: dict) -> str:
    d = date.fromisoformat(b["ts"][:10])
    return f"{(d - timedelta(days=d.weekday())).isoformat()}T00:00:00"


def _hours_key(hours: int) -> Callable[[dict], str]:
    def key(b: dict) -> str:
        ts = datetime.fromisoformat(b["ts"])
        anchor = ts.replace(hour=9, minute=15, second=0)
        n = int((ts - anchor).total_seconds() // (hours * 3600))
        return (anchor + timedelta(hours=hours * n)).isoformat(timespec="seconds")
    return key


def weekly_view(daily: list[dict]) -> IncrementalBars:
    return IncrementalBars(daily, resample_weekly, _week_key)


def hours_view(hourly: list[dict], hours: int) -> IncrementalBars:
    return IncrementalBars(hourly, lambda rows: resample_hours(rows, NSE, hours), _hours_key(hours))


# --- calendar helpers --------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _next_days(after: date, n: int) -> list[date]:
    out, d = [], after
    while len(out) < n:
        d += timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


def mark_eod(bars: list[dict]) -> list[dict]:
    """Flag the last hourly bar of each session (the simulator's time stop needs it)."""
    out = []
    for i, b in enumerate(bars):
        if i + 1 < len(bars):
            eod = bars[i + 1]["ts"][:10] != b["ts"][:10]
        else:
            eod = b["ts"][11:16] >= "15:15"
        out.append({**b, "eod": eod})
    return out


def _market_blocked(first: date, last: date, cache: dict) -> bool:
    key = (first, last)
    if key not in cache:
        market = events.market_events_between(first, last, kinds=("RBI", "BUDGET"))
        cache[key] = (not market["verified"]) or bool(market["events"])
    return cache[key]


def _decision(c: dict, min_score: float) -> str:
    if not c.get("valid"):
        return "soft_rejected"
    return "passed" if c["score"] >= min_score else "below_score"


def _record(c: dict, symbol: str, signal_ts: str, decision: str, outcome: dict) -> dict:
    return {"symbol": symbol, "signal_ts": signal_ts, "setup": c["setup"], "direction": c["direction"],
            "regime": c.get("regime"), "score": c.get("score"), "decision": decision,
            "soft_checks": c.get("soft_checks"), "vol_ratio": c.get("vol_ratio"),
            "entry": c["entry"], "stop": c["stop"], "target": c["target"], "rr": c.get("rr"),
            "status": outcome["status"], "exit_reason": outcome.get("exit_reason"),
            "pnl_r": outcome.get("pnl_r"), "mfe_r": outcome.get("mfe_r"), "mae_r": outcome.get("mae_r"),
            "exit_ts": outcome.get("exit_ts"), "trail_pnl_r": (outcome.get("trail") or {}).get("pnl_r"),
            "fixed_pnl_r": (outcome.get("fixed") or {}).get("pnl_r")}


# --- funnel: where candidates drop out ---------------------------------------------------

def _reason_key(reason: str | None) -> str:
    """'no room for 2R before the next level (24123.45)' → 'no room for NR before the next level'."""
    text = re.sub(r"\([^)]*\)", "", reason or "unknown")
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    return re.sub(r"\s+", " ", text).strip()


class Funnel:
    """Counts for the run summary: days and bars checked, why days were skipped,
    the market regime on the days checked, and every rejection reason per setup.
    Answers "why so few trades?" without re-running anything."""

    def __init__(self):
        self.counts: Counter = Counter()
        self.regimes: Counter = Counter()
        self.rejects: dict[str, Counter] = {}

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def candidates(self, cands: list[dict]) -> None:
        for c in cands:
            if c.get("trackable"):
                self.counts["candidates_trackable"] += 1
                if c.get("valid"):
                    self.counts["candidates_valid"] += 1
                else:
                    for part in (c.get("reject_reason") or "").split("; "):
                        if part:
                            self.rejects.setdefault(f"{c['setup']} (tracked, not valid)", Counter())[
                                _reason_key(part)] += 1
                continue
            self.counts["candidates_rejected"] += 1
            setup = c.get("setup") or "all setups"
            self.rejects.setdefault(setup, Counter())[_reason_key(c.get("reject_reason"))] += 1

    def as_dict(self) -> dict:
        return {"counts": dict(self.counts), "regime_days": dict(self.regimes.most_common()),
                "rejections": {k: dict(v.most_common(12)) for k, v in sorted(self.rejects.items())}}


# backtest segment -> the live instrument whose rules (and exit) it replays
SEGMENT_INSTRUMENT = {"stock_futures_proxy": "stock_futures", "index_futures_proxy": "index_futures",
                      "orb_proxy": "stock_futures", "nifty_options_proxy": "nifty_options"}


def _simulate(c: dict, path: list[dict], fill_until: str, time_stop: date, segment: str | None = None) -> dict:
    """``segment``: the instrument / book whose exit rule applies (``DecisionConfig.exit_rule``)."""
    g = get_config().global_
    return simulate(direction=c["direction"], entry=c["entry"], stop=c["stop"], target=c["target"],
                    atr=c.get("atr"), bars=path, fill_until=fill_until, time_stop_date=time_stop.isoformat(),
                    trail_start_r=g.trailing_shadow.start_r, trail_atr=g.trailing_shadow.trail_atr,
                    exit_rule=exit_rule_for(segment))


# --- segment runners ---------------------------------------------------------------------

def run_cash_swing(start: date, end: date, symbols: list[dict], nifty: list[dict],
                   progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
                   funnel: Funnel | None = None) -> list[dict]:
    """Daily signals, long only. Where hourly history exists the entry window uses
    hourly bars and the 4H confirmation is applied; before that (older years) the
    entry is modelled on the next day's daily bar and timeframes that need hourly
    bars are recorded as not tested (``data = daily_only``)."""
    cfg = get_config()
    inst = cfg.instrument("cash_swing")
    extra = inst.model_extra or {}
    min_turnover = float(extra.get("min_turnover_cr", 50)) * 1e7
    max_stop = float(extra.get("max_stop_pct", 8.0)) / 100
    entry_end = extra.get("entry_window", ["09:30", "11:00"])[1]
    min_score = cfg.global_.scoring.min_shortlist_score
    nifty_index = [b["ts"][:10] for b in nifty]
    blocked_cache: dict = {}
    funnel = funnel or Funnel()
    plan = []          # (day, window, regime, nifty slice)
    for d in _trading_days(start, end):
        iso = d.isoformat()
        funnel.add("trading_days")
        k = bisect.bisect_right(nifty_index, iso)
        if k < 250 or nifty_index[k - 1] != iso:
            funnel.add("days_skipped_no_nifty_history")
            continue
        window = _next_days(d, inst.hold_trading_days)
        if _market_blocked(window[0], window[-1], blocked_cache):
            funnel.add("days_skipped_rbi_budget")
            continue
        bench = nifty[max(0, k - 300):k]
        regime = current_regime(bench)["regime"]
        funnel.regimes[regime] += 1
        plan.append((d, window, regime, bench))

    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = [] if daily_only else get_candles(stock["spot_token"], "60minute", limit=20000)
        d_index = [b["ts"][:10] for b in daily]
        h_index = [b["ts"] for b in hourly]
        hourly_from = h_index[0][:10] if h_index else "9999"
        stock_labels = label_series(daily) if daily else []
        views = {"week": weekly_view(daily)} if daily else {}
        views.update({tf: hours_view(hourly, int(tf[0])) for tf in ("2h", "4h")} if hourly else {})
        for d, window, regime, bench in plan:
            iso = d.isoformat()
            i = bisect.bisect_right(d_index, iso)
            if i == 0 or d_index[i - 1] != iso:
                continue
            bars = daily[max(0, i - 300):i]
            if len(bars) < inst.min_primary_bars or avg_turnover(bars) < _floor(stock, min_turnover):
                funnel.add("stock_days_skipped_history_or_turnover")
                continue
            funnel.add("stock_days_evaluated")
            has_hourly = iso > hourly_from          # 4H needs ~25 sessions of hourly bars before the day
            j_end = bisect.bisect_right(h_index, f"{iso}T23:59:59") - 1

            def view(tf: str) -> list[dict]:
                if tf == "day":
                    return bars
                if tf == "week":
                    return views["week"].upto(i - 1) if "week" in views else []
                if tf in ("2h", "4h"):
                    ok = tf in views and j_end >= 0 and h_index[j_end][:10] == iso
                    return views[tf].upto(j_end) if ok else []
                raise ValueError(f"timeframe {tf} is not supported in backtests")

            hourly_tfs = [tf for tf in inst.confirmation_tfs if tf in ("2h", "4h")]
            confirmations = {tf: view(tf) for tf in inst.confirmation_tfs}
            untested = []
            if hourly_tfs and any(len(confirmations[tf]) < 50 for tf in hourly_tfs):
                untested = hourly_tfs
                confirmations = {tf: b for tf, b in confirmations.items() if tf not in hourly_tfs}
            cands = evaluate(bars, view(inst.scored_htf or "week"), regime, directions=tuple(inst.directions),
                             is_stock=True, benchmark=bench, max_stop_pct=max_stop,
                             confirmations=confirmations, min_primary_bars=inst.min_primary_bars,
                             stock_regime=regime_at(stock_labels, i - 1))
            funnel.candidates(cands)
            for c in cands:
                if not c.get("trackable"):
                    continue
                entry_day = window[0].isoformat()
                exit_by = window[-1] if inst.time_stop else _next_days(d, inst.max_hold_trading_days)[-1]
                lo = bisect.bisect_left(h_index, entry_day)
                hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
                fwd_hourly = hourly[lo:hi] if has_hourly else []
                fwd_daily = [{**b, "eod": True} for b in daily[i:]
                             if entry_day < b["ts"][:10] <= exit_by.isoformat()]
                if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                    path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
                else:
                    entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                    if not entry_bar:
                        continue
                    # Daily-only: the fill can happen at any time on the entry day.
                    path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
                outcome = _simulate(c, path, fill_until, exit_by, "cash_swing")
                rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
                rec.update(data=data, untested=untested or None)
                trades.append(rec)
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, inst.shortlist_per_scan)
    return trades


def run_hourly_proxy(start: date, end: date, symbols: list[dict], nifty: list[dict],
                     progress: Callable[[str], None] = lambda m: None, *, instrument: str = "stock_futures",
                     is_stock: bool = True, daily_only: bool = False, funnel: Funnel | None = None) -> list[dict]:
    """Hourly signals on the spot price (expired futures can't be downloaded).
    ``instrument`` selects the config block (stock_futures / index_futures).
    Index spot has no volume: volume is recorded as not tested and left out of the score."""
    cfg = get_config()
    inst = cfg.instrument(instrument)
    extra = inst.model_extra or {}
    min_score = cfg.global_.scoring.min_shortlist_score
    entry_bars = int(extra.get("entry_valid_bars", 2))
    cutoff = (datetime.combine(date.today(), time(15, 30))
              - timedelta(minutes=int(extra.get("no_entry_last_minutes", 30)))).time()
    nifty_index = [b["ts"][:10] for b in nifty]
    blocked_cache: dict = {}
    funnel = funnel or Funnel()
    days = {}
    for d in _trading_days(start, end):
        funnel.add("trading_days")
        window = [d] + _next_days(d, inst.hold_trading_days - 1)
        if _market_blocked(window[0], window[-1], blocked_cache):
            funnel.add("days_skipped_rbi_budget")
            continue
        k = bisect.bisect_left(nifty_index, d.isoformat())       # yesterday's close and before
        if k < 250:
            funnel.add("days_skipped_no_nifty_history")
            continue
        bench = nifty[max(0, k - 300):k]
        regime = current_regime(bench)["regime"]
        funnel.regimes[regime] += 1
        days[d.isoformat()] = (window, regime, bench)

    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = get_candles(stock["spot_token"], "60minute", limit=20000)
        if not hourly or not daily:
            progress(f"{n}/{len(symbols)} instruments")
            continue
        volume_available = any(b.get("volume") for b in hourly[-50:])
        d_index = [b["ts"][:10] for b in daily]
        stock_labels = label_series(daily)
        views = {tf: hours_view(hourly, int(tf[0])) for tf in ("2h", "4h")}
        daily_cache: dict[str, tuple] = {}
        for j, bar in enumerate(hourly):
            day = bar["ts"][:10]
            if day not in days:
                continue
            window, regime, bench = days[day]
            bar_start = datetime.fromisoformat(bar["ts"])
            bar_end = bar_start + timedelta(hours=1)
            if bar_end.time() > cutoff:
                funnel.add("bars_skipped_last_minutes")
                continue
            funnel.add("bars_evaluated")
            if day not in daily_cache:
                k = bisect.bisect_left(d_index, day)
                ctx_daily = daily[max(0, k - 300):k]
                rs = {dn: _relative_strength_points(ctx_daily, bench, 1 if dn == "long" else -1)
                      for dn in ("long", "short")} if is_stock else None
                daily_cache[day] = (ctx_daily, rs, regime_at(stock_labels, k - 1) if k else None)
            ctx_daily, rs, own_regime = daily_cache[day]

            def view(tf: str) -> list[dict]:
                if tf == "day":
                    return ctx_daily
                if tf in views:
                    return views[tf].upto(j)
                raise ValueError(f"timeframe {tf} is not supported in backtests")

            scored = inst.scored_htf or "4h"
            confirmations = {tf: view(tf) for tf in inst.confirmation_tfs if tf != scored}
            context = {tf: view(tf) for tf in inst.context_must_not_oppose}
            primary = hourly[max(0, j - 399):j + 1]
            cands = evaluate(primary, view(scored), regime, directions=tuple(inst.directions), is_stock=is_stock,
                             rs_override=rs, confirmations=confirmations, context=context,
                             min_primary_bars=inst.min_primary_bars, volume_available=volume_available,
                             stock_regime=own_regime)
            funnel.candidates(cands)
            for c in cands:
                if not c.get("trackable"):
                    continue
                fill_until = min(bar_end + timedelta(hours=entry_bars), datetime.combine(bar_start.date(), cutoff))
                # No time stop: follow the trade up to backtest_max_hold_days (spot has no expiry).
                exit_by = window[-1] if inst.time_stop else _next_days(date.fromisoformat(day), inst.max_hold_trading_days)[-1]
                last = exit_by.isoformat()
                path = []
                for b in hourly[j + 1:]:
                    if b["ts"][:10] > last:
                        break
                    path.append(b)
                if not path:
                    funnel.add("candidates_without_forward_bars")
                    continue
                outcome = _simulate(c, mark_eod(path), fill_until.isoformat(timespec="seconds"), exit_by, instrument)
                rec = _record(c, stock["symbol"], bar["ts"], _decision(c, min_score), outcome)
                rec.update(data="hourly", untested=None if volume_available else ["volume", "oi"])
                trades.append(rec)
        progress(f"{n}/{len(symbols)} instruments")
    mark_shortlist(trades, inst.shortlist_per_scan)
    return trades


def run_futures_proxy(start, end, symbols, nifty, progress=lambda m: None, daily_only=False, funnel=None) -> list[dict]:
    return run_hourly_proxy(start, end, symbols, nifty, progress, instrument="stock_futures", is_stock=True,
                            funnel=funnel)


def run_index_futures(start, end, symbols, nifty, progress=lambda m: None, daily_only=False, funnel=None) -> list[dict]:
    return run_hourly_proxy(start, end, symbols, nifty, progress, instrument="index_futures", is_stock=False,
                            funnel=funnel)


def run_cup_handle(start: date, end: date, symbols: list[dict], nifty: list[dict],
                   progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
                   funnel: Funnel | None = None) -> list[dict]:
    """Daily cup-and-handle breakouts; entry on the next session (hourly bars where
    stored, otherwise the daily bar), then daily bars to the 8-week time stop."""
    from backend.signals import cup_handle
    from backend.signals.indicators import swing_points

    cfg = get_config()
    ch = cup_handle.settings()
    min_score = cfg.global_.scoring.min_shortlist_score
    entry_end = ch["entry_window"][1]
    nifty_index = [b["ts"][:10] for b in nifty]
    blocked_cache: dict = {}
    plan = []
    for d in _trading_days(start, end):
        iso = d.isoformat()
        k = bisect.bisect_right(nifty_index, iso)
        if k == 0 or nifty_index[k - 1] != iso:
            continue
        window = _next_days(d, ch["hold_days"])
        if _market_blocked(window[0], window[min(ch["event_block_days"], len(window)) - 1], blocked_cache):
            continue
        bench = nifty[max(0, k - 300):k]
        plan.append((d, window, current_regime(bench)["regime"], nifty[:k]))

    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = [] if daily_only else get_candles(stock["spot_token"], "60minute", limit=20000)
        d_index = [b["ts"][:10] for b in daily]
        h_index = [b["ts"] for b in hourly]
        swing_highs, _ = swing_points(daily, 2)
        sh_index = [i for i, _ in swing_highs]
        for d, window, regime, bench in plan:
            iso = d.isoformat()
            i = bisect.bisect_right(d_index, iso)
            if i == 0 or d_index[i - 1] != iso or i < ch["min_bars"]:
                continue
            lo_bar = max(0, i - 800)
            bars = daily[lo_bar:i]
            if avg_turnover(bars) < _floor(stock, ch["min_turnover"]):
                continue
            # Swings confirmed by today: fractal index ≤ i-3 (two bars after it must exist).
            upto = bisect.bisect_right(sh_index, i - 3)
            highs = [(k - lo_bar, p) for k, p in swing_highs[:upto] if k >= lo_bar]
            c = cup_handle.candidate(bars, bench, ch, swing_highs=highs, regime=regime)
            if not c.get("trackable"):
                continue
            entry_day = window[0].isoformat()
            exit_by = window[-1] if ch["inst"].time_stop else _next_days(d, ch["inst"].max_hold_trading_days)[-1]
            lo = bisect.bisect_left(h_index, entry_day)
            hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
            fwd_hourly = hourly[lo:hi]
            fwd_daily = [{**b, "eod": True} for b in daily[i:] if entry_day < b["ts"][:10] <= exit_by.isoformat()]
            if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
            else:
                entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                if not entry_bar:
                    continue
                path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
            outcome = _simulate(c, path, fill_until, exit_by, "cup_handle")
            rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
            rec.update(data=data, untested=None, pattern=c.get("pattern"))
            trades.append(rec)
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, ch["shortlist"])
    return trades


def run_wolfe(start: date, end: date, symbols: list[dict], nifty: list[dict],
              progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
              funnel: Funnel | None = None) -> list[dict]:
    """Bullish Wolfe Waves on daily bars (point in time: swings confirmed by the signal
    day); buy limit at the signal close next session (hourly bars where stored, else
    the daily bar), then daily bars to the time stop, with the book's own exit."""
    from backend.signals import wolfe

    cfg = get_config()
    wc = wolfe.settings()
    min_score = cfg.global_.scoring.min_shortlist_score
    entry_end = wc["entry_window"][1]
    nifty_index = [b["ts"][:10] for b in nifty]
    blocked_cache: dict = {}
    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = [] if daily_only else get_candles(stock["spot_token"], "60minute", limit=20000)
        h_index = [b["ts"] for b in hourly]
        first_i = next((i for i, b in enumerate(daily) if b["ts"][:10] >= start.isoformat()), len(daily))
        for c in wolfe.find_all(daily, wc, start=max(first_i, wc["min_bars"])):
            i = c["s"] + 1
            iso = daily[c["s"]]["ts"][:10]
            if iso > end.isoformat():
                break
            if avg_turnover(daily[max(0, i - 60):i]) < _floor(stock, wc["min_turnover"]):
                continue
            d = date.fromisoformat(iso)
            window = _next_days(d, wc["hold_days"])
            if _market_blocked(window[0], window[min(wc["event_block_days"], len(window)) - 1], blocked_cache):
                continue
            k = bisect.bisect_right(nifty_index, iso)
            c["regime"] = current_regime(nifty[max(0, k - 300):k])["regime"] if k else None
            entry_day = window[0].isoformat()
            lo = bisect.bisect_left(h_index, entry_day)
            hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
            fwd_hourly = hourly[lo:hi]
            wolfe_inst = get_config().instrument("wolfe")
            exit_by = window[-1] if wolfe_inst.time_stop else _next_days(d, wolfe_inst.max_hold_trading_days)[-1]
            fwd_daily = [{**b, "eod": True} for b in daily[i:] if entry_day < b["ts"][:10] <= exit_by.isoformat()]
            if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
            else:
                entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                if not entry_bar:
                    continue
                path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
            outcome = _simulate(c, path, fill_until, exit_by, segment="wolfe")
            rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
            rec.update(data=data, untested=None, pattern={k2: c["pattern"][k2] for k2 in
                                                          ("time_ratio", "overshoot_atr", "rsi_divergence", "span_bars")})
            trades.append(rec)
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, wc["shortlist"])
    return trades


def _run_daily_pattern(segment: str, settings: dict, find: Callable[..., list[dict]],
                       pattern_keys: tuple[str, ...], start: date, end: date, symbols: list[dict],
                       nifty: list[dict], progress: Callable[[str], None], daily_only: bool,
                       funnel: Funnel | None = None) -> list[dict]:
    """Daily cash pattern books (reversal, VCP): signals from ``find(daily, first_index, on_reject)``,
    buy limit at the signal close next session (hourly bars where stored, else the daily bar), then
    daily bars with the book's own exit; safety exit after ``max_hold_trading_days``.

    ``funnel`` (26 Nov 2026): every checked stock-day and why it dropped out — the pattern's own
    rejection reasons, then turnover, the event window, the NIFTY-regime filter (``nifty_regimes``)
    and a missing entry bar — so "why so few trades?" is answered per stage."""
    setup_label = segment.replace("_", " ")
    allowed_regimes = settings.get("nifty_regimes")

    def drop(key: str) -> None:
        if funnel is not None:
            funnel.add(key)

    def on_reject(reason: str) -> None:
        if funnel is not None:
            funnel.add("stock_days_checked")
            funnel.rejects.setdefault(setup_label, Counter())[_reason_key(reason)] += 1
    cfg = get_config()
    min_score = cfg.global_.scoring.min_shortlist_score
    entry_end = settings["entry_window"][1]
    inst = settings["inst"]
    nifty_index = [b["ts"][:10] for b in nifty]
    blocked_cache: dict = {}
    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = [] if daily_only else get_candles(stock["spot_token"], "60minute", limit=20000)
        h_index = [b["ts"] for b in hourly]
        first_i = next((i for i, b in enumerate(daily) if b["ts"][:10] >= start.isoformat()), len(daily))
        for c in find(daily, max(first_i, settings["min_bars"]), on_reject):
            i = c["s"] + 1
            iso = daily[c["s"]]["ts"][:10]
            if iso > end.isoformat():
                break
            drop("stock_days_checked")
            drop("pattern_found")
            if avg_turnover(daily[max(0, i - 60):i]) < _floor(stock, settings["min_turnover"]):
                drop("dropped_turnover")
                continue
            d = date.fromisoformat(iso)
            window = _next_days(d, settings["hold_days"])
            if _market_blocked(window[0], window[min(settings["event_block_days"], len(window)) - 1], blocked_cache):
                drop("dropped_event_window")
                continue
            k = bisect.bisect_right(nifty_index, iso)
            c["regime"] = current_regime(nifty[max(0, k - 300):k])["regime"] if k else None
            if allowed_regimes and c["regime"] not in allowed_regimes:
                drop(f"dropped_nifty_regime_{c['regime']}")
                continue
            entry_day = window[0].isoformat()
            lo = bisect.bisect_left(h_index, entry_day)
            hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
            fwd_hourly = hourly[lo:hi]
            exit_by = window[-1] if inst.time_stop else _next_days(d, inst.max_hold_trading_days)[-1]
            fwd_daily = [{**b, "eod": True} for b in daily[i:] if entry_day < b["ts"][:10] <= exit_by.isoformat()]
            if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
            else:
                entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                if not entry_bar:
                    drop("dropped_no_entry_bar")
                    continue
                path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
            outcome = _simulate(c, path, fill_until, exit_by, segment=segment)
            rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
            pattern = c.get("pattern") or {}
            rec.update(data=data, untested=None, pattern={k2: pattern.get(k2) for k2 in pattern_keys})
            trades.append(rec)
            drop("signals_simulated")
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, settings["shortlist"])
    return trades


def run_cash_reversal(start: date, end: date, symbols: list[dict], nifty: list[dict],
                      progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
                      funnel: Funnel | None = None) -> list[dict]:
    """Cash trend reversals on daily bars (the Wolfe Wave replacement), daily stage point in time."""
    from backend.signals import reversal
    from backend.signals import stage as stage_mod
    from backend.signals.indicators import sma

    rc = reversal.cash_settings()
    sa = get_config().global_.stage_analysis

    def find(daily: list[dict], first: int, on_reject=None) -> list[dict]:
        closes = [b["close"] for b in daily]
        smas = (sma(closes, sa.sma_fast), sma(closes, sa.sma_mid), sma(closes, sa.sma_slow))
        return reversal.find_all(daily, rc["params"], lambda t: stage_mod.classify(daily, sa, t, smas),
                                 directions=rc["directions"], start=first, on_reject=on_reject)

    return _run_daily_pattern("cash_reversal", rc, find, ("z", "divergence", "sfp", "effort", "daily_stage"),
                              start, end, symbols, nifty, progress, daily_only, funnel)


def run_vcp(start: date, end: date, symbols: list[dict], nifty: list[dict],
            progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
            funnel: Funnel | None = None) -> list[dict]:
    """Minervini VCP breakouts on daily bars (trend template + contractions, point in time)."""
    from backend.signals import vcp

    vc = vcp.settings()
    return _run_daily_pattern("vcp", vc,
                              lambda daily, first, on_reject=None: vcp.find_all(daily, vc, start=first, on_reject=on_reject),
                              ("pivot", "base_days", "dryup_ratio", "breakout_volume"),
                              start, end, symbols, nifty, progress, daily_only, funnel)


def run_orb(start: date, end: date, symbols: list[dict], nifty: list[dict],
            progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
            funnel: Funnel | None = None) -> list[dict]:
    """Opening range breakout on 10-minute spot bars of the most liquid stocks
    (top N by 20-day turnover before ``start``). First breakout per stock per day,
    exit at ``exit_time`` the same day."""
    from backend.signals.orb import evaluate_orb, mark_exit
    from backend.signals.orb import settings as orb_settings

    cfg = get_config()
    oc = orb_settings()
    min_score = cfg.global_.scoring.min_shortlist_score
    n_or = oc["range_minutes"] // oc["bar_minutes"]
    last_min = int((cfg.instrument("stock_futures").model_extra or {}).get("no_entry_last_minutes", 30))
    cutoff = (datetime.combine(date.today(), time(15, 30)) - timedelta(minutes=last_min)).time()
    nifty_inst = spot_instrument("NIFTY")
    nifty10 = get_candles(nifty_inst["instrument_token"], "10minute", limit=200000) if nifty_inst else []
    nifty_days: dict[str, list[dict]] = {}
    for b in nifty10:
        nifty_days.setdefault(b["ts"][:10], []).append(b)
    blocked_cache: dict = {}
    days = [d for d in _trading_days(start, end) if not _market_blocked(d, d, blocked_cache)]

    ranked = []
    for stock in symbols:
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        before = [b for b in daily if b["ts"][:10] < start.isoformat()][-20:]
        if len(before) >= 20:
            ranked.append((avg_turnover(before), stock, daily))
    chosen = sorted(ranked, key=lambda x: -x[0])[:oc["universe_size"]]

    trades = []
    for n, (_, stock, daily) in enumerate(chosen, 1):
        bars = get_candles(stock["spot_token"], "10minute", limit=200000)
        by_day: dict[str, list[dict]] = {}
        for b in bars:
            by_day.setdefault(b["ts"][:10], []).append(b)
        d_index = [b["ts"][:10] for b in daily]
        history_days: list[str] = []
        for day_key in sorted(by_day):
            d = date.fromisoformat(day_key)
            day_bars = by_day[day_key]
            prev_bars = [b for k in history_days[-oc["volume_days"]:] for b in by_day[k]]
            history_days.append(day_key)
            if d not in days or len(day_bars) <= n_or:
                continue
            k = bisect.bisect_left(d_index, day_key)
            prev_daily = daily[max(0, k - 60):k]
            if len(prev_daily) < 30:
                continue
            nday = nifty_days.get(day_key, [])
            for j in range(n_or, len(day_bars)):
                sig = day_bars[j]
                signal_end = datetime.fromisoformat(sig["ts"]) + timedelta(minutes=oc["bar_minutes"])
                if signal_end.time() > cutoff:
                    break
                c = evaluate_orb(day_bars[:j + 1], prev_daily, prev_bars=prev_bars,
                                 nifty_day_bars=[b for b in nday if b["ts"] <= sig["ts"]] or None, cfg=oc)
                if c.get("reject_reason") == "no close outside the opening range":
                    continue
                if c.get("trackable"):
                    path = mark_exit(day_bars[j + 1:], oc["exit_time"], oc["bar_minutes"])
                    if path:
                        fill_until = min(signal_end + timedelta(minutes=oc["entry_valid_bars"] * oc["bar_minutes"]),
                                         datetime.combine(d, cutoff))
                        outcome = _simulate(c, path, fill_until.isoformat(timespec="seconds"), d, "stock_futures")
                        c["regime"] = None
                        rec = _record(c, stock["symbol"], sig["ts"], _decision(c, min_score), outcome)
                        rec.update(data="10minute", untested=None, stop_type=c["stop_type"])
                        trades.append(rec)
                break   # first breakout of the day only
        progress(f"{n}/{len(chosen)} stocks")
    by_day_trades: dict[str, list[dict]] = {}
    for t in trades:
        if t["decision"] == "passed":
            by_day_trades.setdefault(t["signal_ts"][:10], []).append(t)
    per_day = cfg.instrument("stock_futures").shortlist_per_scan
    for group in by_day_trades.values():
        for t in sorted(group, key=lambda x: x["score"], reverse=True)[:per_day]:
            t["shortlisted"] = True
    return trades


def run_nifty_options(start: date, end: date, symbols: list[dict], nifty: list[dict],
                      progress: Callable[[str], None] = lambda m: None, daily_only: bool = False,
                      funnel: Funnel | None = None) -> list[dict]:
    """NIFTY option buying on 5-minute index bars: every configured setup on every bar
    from first_signal to last_signal, point in time (a window of the last 400 bars),
    out at exit_time the same day. Results in R on the index."""
    from backend.signals import nifty_options as no
    from backend.signals.indicators import atr as atr_series
    from backend.signals.orb import mark_exit

    oc = no.settings()
    inst = spot_instrument(oc["underlying"])
    bars = get_candles(inst["instrument_token"], "5minute", limit=400000) if inst else []
    if not bars:
        raise RuntimeError("no 5-minute NIFTY history; run the history download first")
    dirs = no.supertrend_dirs(bars, oc["supertrend_period"], oc["supertrend_mult"])
    atrs = atr_series(bars, 14)
    blocked_cache: dict = {}
    first, last = no._hm(oc["first_signal"]), no._hm(oc["last_signal"])
    cutoff = no._hm(oc["expiry_day_cutoff"])
    trades, taken = [], {}
    day_index: dict[str, list[int]] = {}
    for i, b in enumerate(bars):
        day_index.setdefault(b["ts"][:10], []).append(i)
    days = [d for d in sorted(day_index) if start.isoformat() <= d <= end.isoformat()]
    for n, day in enumerate(days, 1):
        d = date.fromisoformat(day)
        if _market_blocked(d, d, blocked_cache):
            if funnel:
                funnel.add("days_skipped_events")
            continue
        expiry_wd = 1 if d >= date(2025, 9, 1) else 3
        idx = day_index[day]
        for i in idx:
            end_t = no._end(bars[i])
            if end_t.time() < first or end_t.time() > last:
                continue
            if funnel:
                funnel.add("bars_checked")
            w0 = max(0, i - 399)
            cands = no.candidates(bars[w0:i + 1], oc, st_dirs=dirs[w0:i + 1], atr_values=atrs[w0:i + 1])
            if funnel:
                funnel.candidates([c for c in cands if c.get("direction")])
            for c in cands:
                if not c.get("trackable"):
                    continue
                if d.weekday() == expiry_wd and end_t.time() >= cutoff:
                    continue
                key = (day, c["setup"])
                if taken.get(key, 0) >= oc["max_signals_per_setup_day"]:
                    continue
                taken[key] = taken.get(key, 0) + 1
                path = mark_exit([dict(x) for x in bars[i + 1:idx[-1] + 1]], oc["exit_time"], 5)
                if not path:
                    continue
                fill_until = (end_t + oc["entry_valid_bars"] * no.BAR).isoformat(timespec="seconds")
                outcome = _simulate(c, path, fill_until, d, "nifty_options")
                # one position per setup at a time: each setup is its own "instrument" for the position book
                rec = _record(c, f"{oc['underlying']} {c['setup']}", bars[i]["ts"], "passed", outcome)
                rec.update(data="5minute", untested=None)
                trades.append(rec)
        if n % 20 == 0:
            progress(f"{n}/{len(days)} days")
    for t in trades:
        t["shortlisted"] = True     # every signal is a trade (one ATM option each)
    return trades


def mark_shortlist(trades: list[dict], per_scan: int) -> None:
    """Top ``per_scan`` by score among candidates that passed every rule, per scan time."""
    by_scan: dict[str, list[dict]] = {}
    for t in trades:
        if t["decision"] == "passed":
            by_scan.setdefault(t["signal_ts"], []).append(t)
    for group in by_scan.values():
        for t in sorted(group, key=lambda x: x["score"], reverse=True)[:per_scan]:
            t["shortlisted"] = True


# --- position books: one open trade per instrument ----------------------------------------

# Which config block sets the shortlist size, and what counts as one scan.
_SHORTLIST = {
    "cash_swing": ("cash_swing", "ts"), "stock_futures_proxy": ("stock_futures", "ts"),
    "index_futures_proxy": ("index_futures", "ts"), "orb_proxy": ("stock_futures", "day"),
    "cup_handle": ("cup_handle", "ts"), "wolfe": ("wolfe", "ts"),
    "cash_reversal": ("cash_reversal", "ts"), "vcp": ("vcp", "ts"),
    "nifty_options_proxy": ("nifty_options", "ts"),
}
OPEN_FOREVER = "9999-12-31"


def _busy_until(t: dict) -> str | None:
    """Until when a counted trade blocks its symbol: its exit, forever while open,
    and not at all when the entry never filled."""
    if t["status"] == "closed":
        return t.get("exit_ts") or OPEN_FOREVER
    if t["status"] == "open":
        return OPEN_FOREVER
    return None


def _is_busy(book: dict, t: dict) -> bool:
    until = book.get(t["symbol"])
    return until is not None and until > t["signal_ts"]


def apply_position_books(trades: list[dict], per_scan: int, scan_key: str = "ts") -> dict:
    """One open trade per instrument, applied separately to each group the summary
    reports, the way the live scan would see it: a signal on a symbol that already has
    a counted trade still running in that group is left out (``stacked_all`` /
    ``stacked_passed`` set, ``shortlisted`` re-chosen from the rest, top ``per_scan``
    per scan, at most one per symbol). Returns how many signals each group dropped."""
    trades.sort(key=lambda t: (t["signal_ts"], -(t.get("score") or 0)))
    books = {"all": {}, "passed": {}}
    for t in trades:
        t["stacked_all"] = _is_busy(books["all"], t)
        if not t["stacked_all"] and (until := _busy_until(t)):
            books["all"][t["symbol"]] = until
        if t["decision"] == "passed":
            t["stacked_passed"] = _is_busy(books["passed"], t)
            if not t["stacked_passed"] and (until := _busy_until(t)):
                books["passed"][t["symbol"]] = until
    shortlist_book: dict = {}
    scans: dict[str, list[dict]] = {}
    for t in trades:
        t["shortlisted"] = False
        if t["decision"] == "passed":
            scans.setdefault(t["signal_ts"][:10] if scan_key == "day" else t["signal_ts"], []).append(t)
    skipped_short = 0
    for key in sorted(scans):
        picked: set[str] = set()
        for t in sorted(scans[key], key=lambda x: x["score"], reverse=True):
            if len(picked) >= per_scan:
                break
            if t["symbol"] in picked or _is_busy(shortlist_book, t):
                skipped_short += 1
                continue
            t["shortlisted"] = True
            picked.add(t["symbol"])
            if until := _busy_until(t):
                shortlist_book[t["symbol"]] = until
    return {"all_trackable": sum(1 for t in trades if t["stacked_all"]),
            "passed_all_rules": sum(1 for t in trades if t.get("stacked_passed")),
            "shortlist_skips": skipped_short}


# --- summary -----------------------------------------------------------------------------

def max_drawdown_r(trades: list[dict]) -> float:
    """Largest peak-to-trough fall of cumulative R, trades counted in exit order."""
    equity = peak = dd = 0.0
    for t in sorted((t for t in trades if t.get("pnl_r") is not None), key=lambda t: t.get("exit_ts") or ""):
        equity += t["pnl_r"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return round(dd, 2)


def block(rows: list[dict]) -> dict:
    st = stats(rows)
    st.pop("pnl_rupees", None)
    if st["n"]:
        st["total_r"] = round(sum(r["pnl_r"] for r in rows if r.get("pnl_r") is not None), 2)
        st["max_drawdown_r"] = max_drawdown_r(rows)
        trail = [r["trail_pnl_r"] for r in rows if r.get("trail_pnl_r") is not None]
        st["trailing_avg_r"] = round(sum(trail) / len(trail), 3) if trail else None
        fixed = [r["fixed_pnl_r"] for r in rows if r.get("fixed_pnl_r") is not None]
        st["fixed_avg_r"] = round(sum(fixed) / len(fixed), 3) if fixed else None
    return st


SCORE_BANDS = ((0, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 101))


def score_band(score: float | None) -> str:
    if score is None:
        return "no score"
    for lo, hi in SCORE_BANDS:
        if lo <= score < hi:
            return f"{lo}-{min(hi, 100)}"
    return "no score"


def summarize(trades: list[dict], segment: str = "cash_swing", split_date: str | None = None) -> dict:
    """``stacked_all`` trades (a second signal on a symbol already in a trade) are left
    out of every table; ``passed_all_rules`` also drops ``stacked_passed``.
    ``split_date``: adds ``by_period`` (train = signals before it, test = on/after)."""
    trades_all = trades
    trades = [t for t in trades if not t.get("stacked_all")]
    closed = [t for t in trades if t["status"] == "closed"]

    def by(key) -> dict:
        groups: dict[str, list[dict]] = {}
        for t in closed:
            groups.setdefault(key(t), []).append(t)
        return {k: block(v) for k, v in sorted(groups.items())}

    soft_names = sorted({k for t in closed for k in (t.get("soft_checks") or {})})
    return {
        "candidates": len(trades),
        "trades": len(closed),
        "no_fill": sum(1 for t in trades if t["status"] == "no_fill"),
        "groups": {
            "shortlisted": block([t for t in closed if t.get("shortlisted")]),
            "passed_all_rules": block([t for t in closed if t["decision"] == "passed"
                                       and not t.get("stacked_passed")]),
            "all_trackable": block(closed),
        },
        "by_score_band": by(lambda t: score_band(t.get("score"))),
        "by_setup": by(lambda t: f"{t['setup']} {t['direction']}"),
        "by_regime": by(lambda t: t["regime"] or "n/a (intraday)" if t.get("setup") == "orb" else t["regime"] or "?"),
        "by_setup_regime": by(lambda t: f"{t['setup']} {t['direction']} | {t['regime'] or '?'}"),
        "by_data": by(lambda t: {"hourly": "hourly data", "daily_only": "daily only (older history)"}
                      .get(t.get("data") or "hourly", t.get("data"))),
        "untested": sorted({u for t in closed for u in (t.get("untested") or [])}),
        "by_exit": by(lambda t: t["exit_reason"] or "?"),
        "by_month": by(lambda t: (t["exit_ts"] or "")[:7]),
        "by_year": by(lambda t: (t.get("signal_ts") or "?")[:4]),
        "by_stop_type": by(lambda t: t.get("stop_type") or "swing"),
        "soft_checks": {
            name: {"passed": block([t for t in closed if (t.get("soft_checks") or {}).get(name) is True]),
                   "failed": block([t for t in closed if (t.get("soft_checks") or {}).get(name) is False])}
            for name in soft_names
        },
        "limitations": LIMITATIONS.get(segment, _COMMON),
        **({"by_period": _by_period(closed, split_date)} if split_date else {}),
        "stacked_signals": sum(1 for t in trades_all if t.get("stacked_all")),
    }


def _by_period(closed: list[dict], split_date: str) -> dict:
    """Train (signals before ``split_date``) vs test (on/after) for the three headline
    groups and every setup, so one run shows whether a result holds out of sample."""
    out = {}
    for name, rows in (("train", [t for t in closed if t["signal_ts"][:10] < split_date]),
                       ("test", [t for t in closed if t["signal_ts"][:10] >= split_date])):
        setups: dict[str, list[dict]] = {}
        bands: dict[str, list[dict]] = {}
        for t in rows:
            setups.setdefault(f"{t['setup']} {t['direction']}", []).append(t)
            bands.setdefault(score_band(t.get("score")), []).append(t)
        out[name] = {
            "groups": {"shortlisted": block([t for t in rows if t.get("shortlisted")]),
                       "passed_all_rules": block([t for t in rows if t["decision"] == "passed"
                                                  and not t.get("stacked_passed")]),
                       "all_trackable": block(rows)},
            "by_setup": {k: block(v) for k, v in sorted(setups.items())},
            "by_score_band": {k: block(v) for k, v in sorted(bands.items())},
        }
    out["split_date"] = split_date
    return out


# --- entry points ------------------------------------------------------------------------

def start_backtest(segment: str, rule_set: str, start: date, end: date, max_symbols: int | None = None,
                   daily_only: bool = False, split_date: date | None = None) -> int:
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {', '.join(SEGMENTS)}")
    sets = available_rule_sets()
    if rule_set not in sets:
        raise ValueError(f"rule set must be one of {', '.join(sorted(sets))}")
    if end < start:
        raise ValueError("end date is before start date")
    if split_date and not (start < split_date <= end):
        raise ValueError("split date must be after the start date and on/before the end date")
    if _run_lock.locked():
        raise RuntimeError("a backtest is already running")
    ensure_tables()
    with use_config(sets[rule_set]):
        version = get_config().rule_version
    with get_db() as conn:
        run_id = conn.execute(
            """INSERT INTO rule_backtests (segment, rule_set, rule_version, start_date, end_date, status, progress,
                                          options)
               VALUES (?, ?, ?, ?, ?, 'running', 'starting', ?)""",
            (segment, rule_set, version, start.isoformat(), end.isoformat(),
             json.dumps({"daily_only": daily_only,
                         "split_date": split_date.isoformat() if split_date else None}))).lastrowid
    threading.Thread(target=_run_safely, args=(run_id, segment, sets[rule_set], start, end, max_symbols, daily_only,
                                               split_date),
                     daemon=True, name=f"backtest-{run_id}").start()
    return run_id


def _run_safely(*args) -> None:
    try:
        run_backtest(*args)
    except Exception:
        pass  # already recorded on the run row


def run_backtest(run_id: int, segment: str, config_file, start: date, end: date,
                 max_symbols: int | None = None, daily_only: bool = False, split_date: date | None = None) -> dict:
    """Run synchronously (the API runs it in a thread). Returns the summary."""
    with _run_lock, use_config(config_file):
        try:
            nifty_inst = spot_instrument("NIFTY")
            nifty = get_candles(nifty_inst["instrument_token"], "day", limit=5000) if nifty_inst else []
            if len(nifty) < 300:
                raise RuntimeError("not enough NIFTY daily history; run the history download first")
            if segment == "nifty_options_proxy":
                symbols = [{"symbol": "NIFTY", "spot_token": inst["instrument_token"]}
                           for inst in [spot_instrument("NIFTY")] if inst]
            elif segment == "index_futures_proxy":
                symbols = [{"symbol": name, "spot_token": inst["instrument_token"]}
                           for name in ("NIFTY", "BANKNIFTY") if (inst := spot_instrument(name))]
            elif segment in CASH_BACKTESTS:     # F&O + the NIFTY 500 (26 Nov 2026), like the live cash scans
                from backend.marketdata.cash_universe import cash_universe
                symbols = [s for s in cash_universe() if s.get("spot_token")]
            else:
                symbols = [s for s in fno_universe() if s.get("spot_token")]
            symbols = symbols[:max_symbols] if max_symbols else symbols
            if not symbols:
                raise RuntimeError("no instruments to test; run the history download first")
            runner = {"cash_swing": run_cash_swing, "cup_handle": run_cup_handle, "wolfe": run_wolfe, "orb_proxy": run_orb,
                      "cash_reversal": run_cash_reversal, "vcp": run_vcp,
                      "stock_futures_proxy": run_futures_proxy, "index_futures_proxy": run_index_futures,
                      "nifty_options_proxy": run_nifty_options}[segment]
            funnel = Funnel()
            trades = runner(start, end, symbols, nifty, lambda msg: _update(run_id, progress=msg),
                            daily_only=daily_only, funnel=funnel)
            cfg = get_config()
            stacking = None
            if cfg.global_.limits.one_open_trade_per_instrument:
                block_name, scan_key = _SHORTLIST[segment]
                per_scan = (cup_handle_settings()["shortlist"] if segment == "cup_handle"
                            else instrument_or_live(block_name).shortlist_per_scan)
                stacking = apply_position_books(trades, per_scan, scan_key)
            summary = summarize(trades, segment, split_date.isoformat() if split_date else None)
            summary.update({"symbols": len(symbols), "rule_version": get_config().rule_version,
                            "one_open_trade_per_instrument": stacking is not None, "stacking": stacking,
                            "daily_only": daily_only, "funnel": funnel.as_dict(),
                            "exit_mode": exit_rule_for(SEGMENT_INSTRUMENT.get(segment, segment))["mode"]})
            _update(run_id, status="done", progress="done", summary=summary, trades=trades)
            return summary
        except Exception as exc:
            _update(run_id, status="error", progress=f"{type(exc).__name__}: {exc}"[:300])
            raise


def cup_handle_settings() -> dict:
    from backend.signals import cup_handle
    return cup_handle.settings()


def compare(run_ids: list[int]) -> dict:
    """Side-by-side headline numbers for runs over the same segment and dates."""
    runs = [get_run(i) for i in run_ids]
    runs = [r for r in runs if r and r["status"] == "done"]
    keys = {(r["segment"], r["start_date"], r["end_date"]) for r in runs}
    rows = []
    for r in runs:
        s = r["summary"] or {}
        rows.append({"id": r["id"], "rule_set": r["rule_set"], "rule_version": r["rule_version"],
                     "groups": s.get("groups"), "by_setup": s.get("by_setup"), "trades": s.get("trades")})
    return {"comparable": len(keys) <= 1, "runs": rows}
