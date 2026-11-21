"""Follow every open journal record against stored candles (paper and shadow alike).

Stateless: each run re-simulates a record from its signal using all candles
stored so far, so re-runs and late data give the same answer. Records stay
'pending'/'open' while data is missing; a gap in the daily bars never closes a
trade on wrong data.

Cash swing: entry window is 09:15–11:00 on the next session (hourly bars),
then hourly bars for the rest of that day, then daily bars to the time stop.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from backend.decision.config import get_config
from backend.journal.simulate import delivery_charges, futures_charges, simulate
from backend.journal.store import list_records, update_record
from backend.marketdata.candles import get_candles
from tradingagents.utils.market_calendar import is_trading_day, session_windows

def _risk_per_trade() -> float:
    g = get_config().global_
    return g.capital * g.risk.risk_per_trade_pct / 100


def _cash_swing_max_value() -> float:
    return float((get_config().instrument("cash_swing").model_extra or {}).get("max_position_value", 250_000))


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
    daily = [b for b in get_candles(token, "day", limit=40)
             if entry_date < b["ts"][:10] <= time_stop_date]
    expected = _trading_days(date.fromisoformat(entry_date) + timedelta(days=1), end)
    have = [b["ts"][:10] for b in daily]
    missing = [d for d in expected[:-1] if d not in have]  # today's bar may not be in yet
    if missing:
        return hourly + daily, f"missing daily bars: {', '.join(missing[:3])}"
    return hourly + daily, None


def hourly_bars(rec: dict) -> tuple[list[dict], str | None]:
    """60-minute bars of the traded contract after the signal bar, with ``eod`` on
    each day's last bar. Days fully in the past must be complete."""
    exchange = "MCX" if rec["exchange"] == "MCX" else "NSE"
    bars = [b for b in get_candles(rec["instrument_token"], "60minute",
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
        # The last hourly bar of a session starts within the final hour before the close.
        day_bars[-1]["eod"] = day < today or _minutes(last_end) + 60 >= _minutes(close)
        if day < today and not day_bars[-1]["eod"]:
            problem = f"incomplete hourly bars on {day}"
    return bars, problem


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _size(rec: dict, fill: float, risk: float) -> tuple[int, float]:
    """(quantity, value per point). Cash: shares. Futures/MCX: lots × multiplier."""
    extra = rec.get("extra") or {}
    if rec["segment"] == "cash_swing":
        return max(0, math.floor(min(_risk_per_trade() / risk, _cash_swing_max_value() / fill))), 1.0
    per_lot = float(extra.get("value_per_point") or 1)
    lots = max(0, math.floor(_risk_per_trade() / (risk * per_lot)))
    return lots, per_lot


def track_record(rec: dict) -> dict:
    if rec["segment"] == "cash_swing":
        bars, problem = cash_swing_bars(rec["instrument_token"], rec["entry_date"], rec["time_stop_date"])
    else:
        bars, problem = hourly_bars(rec)
    if not bars:
        return {}
    result = simulate(direction=rec["direction"], entry=rec["entry"], stop=rec["stop"], target=rec["target"],
                      atr=rec.get("atr"), bars=bars, fill_until=rec["fill_until"],
                      time_stop_date=rec["time_stop_date"],
                      trail_start_r=get_config().global_.trailing_shadow.start_r,
                      trail_atr=get_config().global_.trailing_shadow.trail_atr)
    status = result["status"]
    if status == "pending":
        return {}
    if status == "no_fill":
        return {"track_status": "no_fill", "no_fill_reason": result["reason"]}
    fields = {"fill_ts": result["fill_ts"], "fill_price": result["fill_price"],
              "mfe_r": result["mfe_r"], "mae_r": result["mae_r"], "trail": result["trail"]}
    if status == "open" or problem:
        fields["track_status"] = "open"
        if problem:
            fields["extra"] = {**(rec.get("extra") or {}), "data_problem": problem}
        return fields
    s = 1 if rec["direction"] == "long" else -1
    qty, per_point = _size(rec, result["fill_price"], result["risk_per_unit"])
    entry_value = qty * per_point * result["fill_price"]
    exit_value = qty * per_point * result["exit_price"]
    buy, sell = (entry_value, exit_value) if s > 0 else (exit_value, entry_value)
    if not qty:
        charges = 0.0
    elif rec["segment"] == "cash_swing":
        charges = delivery_charges(buy, sell)
    else:
        charges = futures_charges(buy, sell, rec["exchange"])
    gross = s * (exit_value - entry_value)
    fields.update({"track_status": "closed", "exit_ts": result["exit_ts"], "exit_price": result["exit_price"],
                   "exit_reason": result["exit_reason"], "pnl_r": result["pnl_r"], "qty": qty,
                   "charges": charges, "pnl_rupees": round(gross - charges, 2)})
    return fields


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
