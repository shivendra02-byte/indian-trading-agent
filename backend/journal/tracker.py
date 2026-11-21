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

from backend.journal.simulate import delivery_charges, simulate
from backend.journal.store import list_records, update_record
from backend.marketdata.candles import get_candles
from tradingagents.utils.market_calendar import is_trading_day

RISK_PER_TRADE = 10_000          # ₹ — 1% of ₹10L; notional sizing until the risk engine exists
CASH_SWING_MAX_VALUE = 250_000   # ₹2.5L per stock
ENTRY_WINDOW_END = "11:00"


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


def track_record(rec: dict) -> dict:
    bars, problem = cash_swing_bars(rec["instrument_token"], rec["entry_date"], rec["time_stop_date"])
    if not bars:
        return {}
    result = simulate(direction=rec["direction"], entry=rec["entry"], stop=rec["stop"], target=rec["target"],
                      atr=rec.get("atr"), bars=bars, fill_until=f"{rec['entry_date']}T{ENTRY_WINDOW_END}",
                      time_stop_date=rec["time_stop_date"])
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
    risk = result["risk_per_unit"]
    qty = max(0, math.floor(min(RISK_PER_TRADE / risk, CASH_SWING_MAX_VALUE / result["fill_price"])))
    buy, sell = qty * result["fill_price"], qty * result["exit_price"]
    charges = delivery_charges(buy, sell) if qty else 0.0
    gross = qty * (result["exit_price"] - result["fill_price"]) * (1 if rec["direction"] == "long" else -1)
    fields.update({"track_status": "closed", "exit_ts": result["exit_ts"], "exit_price": result["exit_price"],
                   "exit_reason": result["exit_reason"], "pnl_r": result["pnl_r"], "qty": qty,
                   "charges": charges, "pnl_rupees": round(gross - charges, 2)})
    return fields


def track_open_records() -> dict:
    """Update every pending/open cash swing record. Returns counts."""
    counts = {"checked": 0, "filled": 0, "closed": 0, "no_fill": 0}
    for rec in list_records("segment = 'cash_swing' AND track_status IN ('pending', 'open')", limit=5000):
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
