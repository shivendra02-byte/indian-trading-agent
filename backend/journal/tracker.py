"""Follow every open journal record against stored candles (paper and shadow alike).

Stateless: each run re-simulates a record from its signal using all candles
stored so far, so re-runs and late data give the same answer. Records stay
'pending'/'open' while data is missing; a gap in the daily bars never closes a
trade on wrong data.

Cash swing: entry window is 09:15–11:00 on the next session (hourly bars),
then hourly bars for the rest of that day, then daily bars to the time stop.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

from backend.decision.config import exit_rule_for, get_config
from backend.journal.simulate import delivery_charges, futures_charges, simulate
from backend.journal.store import list_records, update_record
from backend.marketdata.candles import get_candles
from tradingagents.utils.market_calendar import is_trading_day, session_windows

log = logging.getLogger(__name__)

def _risk_per_trade() -> float:
    g = get_config().global_
    return g.capital * g.risk.risk_per_trade_pct / 100


CASH_SEGMENTS = ("cash_swing", "cup_handle", "wolfe")   # delivery trades: next-day hourly entry, then daily bars


def _cash_max_value(segment: str = "cash_swing") -> float:
    default = 250_000 if segment == "cash_swing" else 200_000
    return float((get_config().instrument(segment).model_extra or {}).get("max_position_value", default))


def _trading_days(first: date, last: date) -> list[str]:
    out, d = [], first
    while d <= last:
        if is_trading_day(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def cash_swing_bars(token: int, entry_date: str, time_stop_date: str) -> tuple[list[dict], str | None]:
    """Hourly bars of the entry day, then daily bars after it. Returns (bars, problem)."""
    hourly = [b for b in get_candles(token, "60minute", since=datetime.fromisoformat(entry_date))
              if b["ts"][:10] == entry_date]
    if not hourly:
        return [], None  # entry day not traded/synced yet
    for b in hourly:
        b["eod"] = False
    hourly[-1]["eod"] = hourly[-1]["ts"][11:16] >= "15:15"
    end = min(date.fromisoformat(time_stop_date), date.today())
    daily = [b for b in get_candles(token, "day", limit=400)   # trades may now run for months (no time stop)
             if entry_date < b["ts"][:10] <= time_stop_date]
    expected = _trading_days(date.fromisoformat(entry_date) + timedelta(days=1), end)
    have = [b["ts"][:10] for b in daily]
    missing = [d for d in expected[:-1] if d not in have]  # today's bar may not be in yet
    if missing:
        return hourly + daily, f"missing daily bars: {', '.join(missing[:3])}"
    return hourly + daily, None


def hourly_bars(rec: dict) -> tuple[list[dict], str | None]:
    """Intraday bars of the traded contract after the signal bar (60-minute, or the
    record's ``extra.bar_interval`` — 10-minute for ORB), with ``eod`` on each
    day's last bar. Days fully in the past must be complete. Records with an
    ``extra.exit_time`` (ORB) exit with the bar that ends at/after that time."""
    exchange = "MCX" if rec["exchange"] == "MCX" else "NSE"
    extra = rec.get("extra") or {}
    interval = extra.get("bar_interval", "60minute")
    bar_minutes = int(extra.get("bar_minutes", 60))
    bars = [b for b in get_candles(rec["instrument_token"], interval,
                                   since=datetime.fromisoformat(rec["signal_ts"]))
            if b["ts"] > rec["signal_ts"]]
    if not bars:
        return [], None
    by_day: dict[str, list[dict]] = {}
    for b in bars:
        by_day.setdefault(b["ts"][:10], []).append(b)
    problem = None
    today = date.today().isoformat()
    for day, day_bars in by_day.items():
        windows = session_windows(exchange, date.fromisoformat(day))
        close = windows[-1][1].strftime("%H:%M") if windows else "23:59"
        last_end = day_bars[-1]["ts"][11:16]
        for b in day_bars:
            b["eod"] = False
        # The last bar of a session starts within the final bar length before the close.
        day_bars[-1]["eod"] = day < today or _minutes(last_end) + bar_minutes >= _minutes(close)
        if day < today and not day_bars[-1]["eod"]:
            problem = f"incomplete {interval} bars on {day}"
    if extra.get("exit_time"):
        from backend.signals.orb import mark_exit
        bars = mark_exit(bars, extra["exit_time"], bar_minutes)   # the time stop is the same day
    return bars, problem


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _size(rec: dict, fill: float, risk: float) -> tuple[int, float]:
    """(quantity, value per point). Cash: shares. Futures/MCX: lots × multiplier."""
    extra = rec.get("extra") or {}
    if rec["segment"] in CASH_SEGMENTS:
        return max(0, math.floor(min(_risk_per_trade() / risk, _cash_max_value(rec["segment"]) / fill))), 1.0
    per_lot = float(extra.get("value_per_point") or 1)
    from backend.decision.config import lots_within_risk
    lots, _ = lots_within_risk(rec["segment"], risk * per_lot)   # MCX: 1 lot up to one_lot_max_risk_pct
    return max(0, lots), per_lot


def track_record(rec: dict) -> dict:
    if rec["segment"] in CASH_SEGMENTS:
        bars, problem = cash_swing_bars(rec["instrument_token"], rec["entry_date"], rec["time_stop_date"])
    else:
        bars, problem = hourly_bars(rec)
    if not bars:
        return {}
    result = simulate(direction=rec["direction"], entry=rec["entry"], stop=rec["stop"], target=rec["target"],
                      atr=rec.get("atr"), bars=bars, fill_until=rec["fill_until"],
                      time_stop_date=rec["time_stop_date"],
                      trail_start_r=get_config().global_.trailing_shadow.start_r,
                      trail_atr=get_config().global_.trailing_shadow.trail_atr,
                      exit_rule=exit_rule_for(rec["segment"]))
    status = result["status"]
    if status == "pending":
        return {}
    if status == "no_fill":
        return {"track_status": "no_fill", "no_fill_reason": result["reason"]}
    fields = {"fill_ts": result["fill_ts"], "fill_price": result["fill_price"],
              "mfe_r": result["mfe_r"], "mae_r": result["mae_r"], "trail": result["trail"]}
    if result.get("fixed"):   # managed exit: keep the plain fixed exit for comparison
        fields["extra"] = {**(rec.get("extra") or {}), "exit_mode": result["exit_mode"],
                           "fixed_exit": result["fixed"]}
    if status == "open" or problem:
        fields["track_status"] = "open"
        if problem:
            fields["extra"] = {**(fields.get("extra") or rec.get("extra") or {}), "data_problem": problem}
        if status == "open" and result.get("trail_stop") is not None:
            fields["extra"] = {**(fields.get("extra") or rec.get("extra") or {}),
                               "stop_now": _stop_now(rec, result)}
        return fields
    s = 1 if rec["direction"] == "long" else -1
    qty, per_point = _size(rec, result["fill_price"], result["risk_per_unit"])
    entry_value = qty * per_point * result["fill_price"]
    exit_value = qty * per_point * result["exit_price"]
    buy, sell = (entry_value, exit_value) if s > 0 else (exit_value, entry_value)
    if not qty:
        charges = 0.0
    elif rec["segment"] in CASH_SEGMENTS:
        charges = delivery_charges(buy, sell)
    else:
        charges = futures_charges(buy, sell, rec["exchange"])
    gross = s * (exit_value - entry_value)
    fields.update({"track_status": "closed", "exit_ts": result["exit_ts"], "exit_price": result["exit_price"],
                   "exit_reason": result["exit_reason"], "pnl_r": result["pnl_r"], "qty": qty,
                   "charges": charges, "pnl_rupees": round(gross - charges, 2)})
    return fields


def _stop_now(rec: dict, result: dict) -> dict:
    """Where the managed stop is now (initial / breakeven / trailing), shown on the Trade Journal
    and sent as a "move your stop" message for trades you were alerted on (asked 23 Nov 2026)."""
    from backend.journal.panel import stop_move_alert
    s = 1 if rec["direction"] == "long" else -1
    risk = result["risk_per_unit"]
    level = result["trail_stop"]
    now = {"level": level, "state": result.get("stop_state") or "initial",
           "locked_r": round(s * (level - result["fill_price"]) / risk, 2) if risk else 0.0,
           "mfe_r": result["mfe_r"], "last_close": result.get("last_close"),
           "at": datetime.now().isoformat(timespec="seconds")}
    try:
        return stop_move_alert(rec, now, risk)
    except Exception as exc:   # a message must never stop the tracker
        log.warning("stop alert failed for %s: %s", rec.get("symbol"), exc)
        return {**now, **{k: v for k, v in ((rec.get("extra") or {}).get("stop_now") or {}).items()
                          if k in ("alerted_level", "alert")}}


def track_open_records() -> dict:
    """Update every pending/open record (all segments). Returns counts."""
    counts = {"checked": 0, "filled": 0, "closed": 0, "no_fill": 0}
    for rec in list_records("track_status IN ('pending', 'open')", limit=5000):
        counts["checked"] += 1
        fields = track_record(rec)
        if not fields:
            continue
        if fields.get("track_status") == "closed":
            counts["closed"] += 1
        elif fields.get("track_status") == "no_fill":
            counts["no_fill"] += 1
        elif rec["track_status"] == "pending":
            counts["filled"] += 1
        update_record(rec["id"], fields)
    return counts
