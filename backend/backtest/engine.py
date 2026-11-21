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
import threading
from datetime import date, datetime, time, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import available_rule_sets, get_config, use_config
from backend.journal.learning import stats
from backend.journal.simulate import simulate
from backend.marketdata.candles import get_candles, resample_hours, resample_weekly
from backend.marketdata.instruments import fno_universe, spot_instrument
from backend.signals import events
from backend.signals.cash_swing import avg_turnover
from backend.signals.regime import current_regime
from backend.signals.setups import _relative_strength_points, evaluate
from tradingagents.utils.market_calendar import NSE, is_trading_day

SEGMENTS = ("cash_swing", "cup_handle", "stock_futures_proxy", "index_futures_proxy")
_COMMON = [
    "Portfolio limits are not applied: every candidate is counted on its own.",
    "Only RBI and Budget dates are checked for market-wide events.",
]
_SURVIVORSHIP = "Universe is today's F&O list: stocks removed from F&O in the past are missing (survivorship bias)."
LIMITATIONS = {
    "cash_swing": [
        _SURVIVORSHIP,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts (about 12 months back) the entry uses the daily bar and the 4H "
        "confirmation is not tested (shown as 'daily only').",
        *_COMMON],
    "cup_handle": [
        _SURVIVORSHIP,
        "No historical company results dates, so the results filter is not applied.",
        "Before hourly history starts the entry uses the daily bar (shown as 'daily only').",
        *_COMMON],
    "stock_futures_proxy": [
        _SURVIVORSHIP,
        "Futures are approximated by the spot stock, because expired contracts cannot be downloaded.",
        "No historical F&O ban list, bid-ask spreads, open interest or company results dates, so those "
        "filters are not applied.",
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
                trades TEXT
            )""")


def _update(run_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [json.dumps(v) if k in ("summary", "trades") else v for k, v in fields.items()]
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
    out["trades"] = json.loads(out["trades"]) if (with_trades and out["trades"]) else None
    if not with_trades:
        out.pop("trades")
    return out


def list_runs(limit: int = 30) -> list[dict]:
    ensure_tables()
    with get_db() as conn:
        rows = conn.execute("""SELECT id, created_at, segment, rule_set, rule_version, start_date, end_date,
                                      status, progress, summary FROM rule_backtests ORDER BY id DESC LIMIT ?""",
                            (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        summary = json.loads(item.pop("summary") or "null") or {}
        item["headline"] = (summary.get("groups") or {}).get("shortlisted")
        item["trades"] = summary.get("trades")
        out.append(item)
    return out


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
            "exit_ts": outcome.get("exit_ts"), "trail_pnl_r": (outcome.get("trail") or {}).get("pnl_r")}


def _simulate(c: dict, path: list[dict], fill_until: str, time_stop: date) -> dict:
    trail = get_config().global_.trailing_shadow
    return simulate(direction=c["direction"], entry=c["entry"], stop=c["stop"], target=c["target"],
                    atr=c.get("atr"), bars=path, fill_until=fill_until, time_stop_date=time_stop.isoformat(),
                    trail_start_r=trail.start_r, trail_atr=trail.trail_atr)


# --- segment runners ---------------------------------------------------------------------

def run_cash_swing(start: date, end: date, symbols: list[dict], nifty: list[dict],
                   progress: Callable[[str], None] = lambda m: None) -> list[dict]:
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
    plan = []          # (day, window, regime, nifty slice)
    for d in _trading_days(start, end):
        iso = d.isoformat()
        k = bisect.bisect_right(nifty_index, iso)
        if k < 250 or nifty_index[k - 1] != iso:
            continue
        window = _next_days(d, inst.hold_trading_days)
        if _market_blocked(window[0], window[-1], blocked_cache):
            continue
        bench = nifty[max(0, k - 300):k]
        plan.append((d, window, current_regime(bench)["regime"], bench))

    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = get_candles(stock["spot_token"], "60minute", limit=20000)
        d_index = [b["ts"][:10] for b in daily]
        h_index = [b["ts"] for b in hourly]
        hourly_from = h_index[0][:10] if h_index else "9999"
        views = {"week": weekly_view(daily)} if daily else {}
        views.update({tf: hours_view(hourly, int(tf[0])) for tf in ("2h", "4h")} if hourly else {})
        for d, window, regime, bench in plan:
            iso = d.isoformat()
            i = bisect.bisect_right(d_index, iso)
            if i == 0 or d_index[i - 1] != iso:
                continue
            bars = daily[max(0, i - 300):i]
            if len(bars) < inst.min_primary_bars or avg_turnover(bars) < min_turnover:
                continue
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
            for c in evaluate(bars, view(inst.scored_htf or "week"), regime, directions=tuple(inst.directions),
                              is_stock=True, benchmark=bench, max_stop_pct=max_stop,
                              confirmations=confirmations, min_primary_bars=inst.min_primary_bars):
                if not c.get("trackable"):
                    continue
                entry_day = window[0].isoformat()
                lo = bisect.bisect_left(h_index, entry_day)
                hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
                fwd_hourly = hourly[lo:hi] if has_hourly else []
                fwd_daily = [{**b, "eod": True} for b in daily[i:]
                             if entry_day < b["ts"][:10] <= window[-1].isoformat()]
                if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                    path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
                else:
                    entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                    if not entry_bar:
                        continue
                    # Daily-only: the fill can happen at any time on the entry day.
                    path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
                outcome = _simulate(c, path, fill_until, window[-1])
                rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
                rec.update(data=data, untested=untested or None)
                trades.append(rec)
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, inst.shortlist_per_scan)
    return trades


def run_hourly_proxy(start: date, end: date, symbols: list[dict], nifty: list[dict],
                     progress: Callable[[str], None] = lambda m: None, *, instrument: str = "stock_futures",
                     is_stock: bool = True) -> list[dict]:
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
    days = {}
    for d in _trading_days(start, end):
        window = [d] + _next_days(d, inst.hold_trading_days - 1)
        if _market_blocked(window[0], window[-1], blocked_cache):
            continue
        k = bisect.bisect_left(nifty_index, d.isoformat())       # yesterday's close and before
        if k < 250:
            continue
        bench = nifty[max(0, k - 300):k]
        days[d.isoformat()] = (window, current_regime(bench)["regime"], bench)

    trades = []
    for n, stock in enumerate(symbols, 1):
        daily = get_candles(stock["spot_token"], "day", limit=5000)
        hourly = get_candles(stock["spot_token"], "60minute", limit=20000)
        if not hourly or not daily:
            progress(f"{n}/{len(symbols)} instruments")
            continue
        volume_available = any(b.get("volume") for b in hourly[-50:])
        d_index = [b["ts"][:10] for b in daily]
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
                continue
            if day not in daily_cache:
                k = bisect.bisect_left(d_index, day)
                ctx_daily = daily[max(0, k - 300):k]
                rs = {dn: _relative_strength_points(ctx_daily, bench, 1 if dn == "long" else -1)
                      for dn in ("long", "short")} if is_stock else None
                daily_cache[day] = (ctx_daily, rs)
            ctx_daily, rs = daily_cache[day]

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
            for c in evaluate(primary, view(scored), regime, directions=tuple(inst.directions), is_stock=is_stock,
                              rs_override=rs, confirmations=confirmations, context=context,
                              min_primary_bars=inst.min_primary_bars, volume_available=volume_available):
                if not c.get("trackable"):
                    continue
                fill_until = min(bar_end + timedelta(hours=entry_bars), datetime.combine(bar_start.date(), cutoff))
                last = window[-1].isoformat()
                path = []
                for b in hourly[j + 1:]:
                    if b["ts"][:10] > last:
                        break
                    path.append(b)
                if not path:
                    continue
                outcome = _simulate(c, mark_eod(path), fill_until.isoformat(timespec="seconds"), window[-1])
                rec = _record(c, stock["symbol"], bar["ts"], _decision(c, min_score), outcome)
                rec.update(data="hourly", untested=None if volume_available else ["volume", "oi"])
                trades.append(rec)
        progress(f"{n}/{len(symbols)} instruments")
    mark_shortlist(trades, inst.shortlist_per_scan)
    return trades


def run_futures_proxy(start, end, symbols, nifty, progress=lambda m: None) -> list[dict]:
    return run_hourly_proxy(start, end, symbols, nifty, progress, instrument="stock_futures", is_stock=True)


def run_index_futures(start, end, symbols, nifty, progress=lambda m: None) -> list[dict]:
    return run_hourly_proxy(start, end, symbols, nifty, progress, instrument="index_futures", is_stock=False)


def run_cup_handle(start: date, end: date, symbols: list[dict], nifty: list[dict],
                   progress: Callable[[str], None] = lambda m: None) -> list[dict]:
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
        hourly = get_candles(stock["spot_token"], "60minute", limit=20000)
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
            if avg_turnover(bars) < ch["min_turnover"]:
                continue
            # Swings confirmed by today: fractal index ≤ i-3 (two bars after it must exist).
            upto = bisect.bisect_right(sh_index, i - 3)
            highs = [(k - lo_bar, p) for k, p in swing_highs[:upto] if k >= lo_bar]
            c = cup_handle.candidate(bars, bench, ch, swing_highs=highs, regime=regime)
            if not c.get("trackable"):
                continue
            entry_day = window[0].isoformat()
            lo = bisect.bisect_left(h_index, entry_day)
            hi = bisect.bisect_left(h_index, window[1].isoformat()) if len(window) > 1 else len(hourly)
            fwd_hourly = hourly[lo:hi]
            fwd_daily = [{**b, "eod": True} for b in daily[i:] if entry_day < b["ts"][:10] <= window[-1].isoformat()]
            if fwd_hourly and fwd_hourly[0]["ts"][:10] == entry_day:
                path, data, fill_until = mark_eod(fwd_hourly) + fwd_daily, "hourly", f"{entry_day}T{entry_end}"
            else:
                entry_bar = [{**b, "eod": True} for b in daily[i:i + 1] if b["ts"][:10] == entry_day]
                if not entry_bar:
                    continue
                path, data, fill_until = entry_bar + fwd_daily, "daily_only", f"{entry_day}T23:59:59"
            outcome = _simulate(c, path, fill_until, window[-1])
            rec = _record(c, stock["symbol"], iso, _decision(c, min_score), outcome)
            rec.update(data=data, untested=None, pattern=c.get("pattern"))
            trades.append(rec)
        progress(f"{n}/{len(symbols)} stocks")
    mark_shortlist(trades, ch["shortlist"])
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
    return st


def summarize(trades: list[dict], segment: str = "cash_swing") -> dict:
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
            "passed_all_rules": block([t for t in closed if t["decision"] == "passed"]),
            "all_trackable": block(closed),
        },
        "by_setup": by(lambda t: f"{t['setup']} {t['direction']}"),
        "by_regime": by(lambda t: t["regime"] or "?"),
        "by_setup_regime": by(lambda t: f"{t['setup']} {t['direction']} | {t['regime'] or '?'}"),
        "by_data": by(lambda t: {"hourly": "hourly data", "daily_only": "daily only (older history)"}
                      .get(t.get("data") or "hourly", t.get("data"))),
        "untested": sorted({u for t in closed for u in (t.get("untested") or [])}),
        "by_exit": by(lambda t: t["exit_reason"] or "?"),
        "by_month": by(lambda t: (t["exit_ts"] or "")[:7]),
        "soft_checks": {
            name: {"passed": block([t for t in closed if (t.get("soft_checks") or {}).get(name) is True]),
                   "failed": block([t for t in closed if (t.get("soft_checks") or {}).get(name) is False])}
            for name in soft_names
        },
        "limitations": LIMITATIONS.get(segment, _COMMON),
    }


# --- entry points ------------------------------------------------------------------------

def start_backtest(segment: str, rule_set: str, start: date, end: date, max_symbols: int | None = None) -> int:
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {', '.join(SEGMENTS)}")
    sets = available_rule_sets()
    if rule_set not in sets:
        raise ValueError(f"rule set must be one of {', '.join(sorted(sets))}")
    if end < start:
        raise ValueError("end date is before start date")
    if _run_lock.locked():
        raise RuntimeError("a backtest is already running")
    ensure_tables()
    with use_config(sets[rule_set]):
        version = get_config().rule_version
    with get_db() as conn:
        run_id = conn.execute(
            """INSERT INTO rule_backtests (segment, rule_set, rule_version, start_date, end_date, status, progress)
               VALUES (?, ?, ?, ?, ?, 'running', 'starting')""",
            (segment, rule_set, version, start.isoformat(), end.isoformat())).lastrowid
    threading.Thread(target=_run_safely, args=(run_id, segment, sets[rule_set], start, end, max_symbols),
                     daemon=True, name=f"backtest-{run_id}").start()
    return run_id


def _run_safely(*args) -> None:
    try:
        run_backtest(*args)
    except Exception:
        pass  # already recorded on the run row


def run_backtest(run_id: int, segment: str, config_file, start: date, end: date,
                 max_symbols: int | None = None) -> dict:
    """Run synchronously (the API runs it in a thread). Returns the summary."""
    with _run_lock, use_config(config_file):
        try:
            nifty_inst = spot_instrument("NIFTY")
            nifty = get_candles(nifty_inst["instrument_token"], "day", limit=5000) if nifty_inst else []
            if len(nifty) < 300:
                raise RuntimeError("not enough NIFTY daily history; run the history download first")
            if segment == "index_futures_proxy":
                symbols = [{"symbol": name, "spot_token": inst["instrument_token"]}
                           for name in ("NIFTY", "BANKNIFTY") if (inst := spot_instrument(name))]
            else:
                symbols = [s for s in fno_universe() if s.get("spot_token")]
            symbols = symbols[:max_symbols] if max_symbols else symbols
            if not symbols:
                raise RuntimeError("no instruments to test; run the history download first")
            runner = {"cash_swing": run_cash_swing, "cup_handle": run_cup_handle,
                      "stock_futures_proxy": run_futures_proxy, "index_futures_proxy": run_index_futures}[segment]
            trades = runner(start, end, symbols, nifty, lambda msg: _update(run_id, progress=msg))
            summary = summarize(trades, segment)
            summary.update({"symbols": len(symbols), "rule_version": get_config().rule_version})
            _update(run_id, status="done", progress="done", summary=summary, trades=trades)
            return summary
        except Exception as exc:
            _update(run_id, status="error", progress=f"{type(exc).__name__}: {exc}"[:300])
            raise


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
