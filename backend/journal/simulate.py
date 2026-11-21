"""Bar-by-bar trade outcome simulation (pure functions).

Used for every journal record — paper trades and shadow trades alike — and
later by the backtester. Conservative conventions:
* A limit entry fills if price trades through it in the entry window; if the
  bar opens beyond the entry in our favour, the fill is at the open.
* No fill if the market opens beyond the stop before we are filled.
* If one bar touches both stop and target, the stop is assumed first.
* A gap through the stop exits at the open (no free stop); a gap through the
  target exits at the open too.
* MFE/MAE are measured from the fill, in R and in price.
* Trailing shadow exit: after 1.5R in favour, the stop moves to breakeven, then
  trails 2 ATR behind the best close. Same target and time stop.
"""

from __future__ import annotations

from typing import Sequence

TRAIL_START_R = 1.5
TRAIL_ATR = 2.0


def _touch(bar: dict, level: float, s: int, side: str) -> bool:
    """side 'adverse': price moved against us to ``level``; 'favour': in our favour."""
    if side == "adverse":
        return bar["low"] <= level if s > 0 else bar["high"] >= level
    return bar["high"] >= level if s > 0 else bar["low"] <= level


def _run_exit(bars: Sequence[dict], start: int, fill: float, stop: float, target: float, s: int,
              time_stop_date: str, atr: float | None, trailing: bool, fill_bar_done: bool) -> dict:
    risk = s * (fill - stop)
    best_fav = worst_adv = 0.0                     # price distance from fill
    best_close = fill
    trail = stop
    for i in range(start, len(bars)):
        b = bars[i]
        first = i == start and fill_bar_done       # the fill bar: no gap logic, open already used
        if not first:
            if s * (b["open"] - trail) <= 0:
                return _exit(b, b["open"], "gap_stop" if trail == stop else "trail_stop", fill, risk, s,
                             best_fav, max(worst_adv, s * (fill - b["open"])), i)
            if s * (b["open"] - target) >= 0:
                return _exit(b, b["open"], "target", fill, risk, s, max(best_fav, s * (b["open"] - fill)), worst_adv, i)
        adverse = s * (fill - (b["low"] if s > 0 else b["high"]))
        favour = s * ((b["high"] if s > 0 else b["low"]) - fill)
        if _touch(b, trail, s, "adverse"):
            reason = "stop" if trail == stop else "trail_stop"
            return _exit(b, trail, reason, fill, risk, s, best_fav, max(worst_adv, s * (fill - trail)), i)
        if _touch(b, target, s, "favour"):
            return _exit(b, target, "target", fill, risk, s, max(best_fav, s * (target - fill)),
                         max(worst_adv, adverse), i)
        best_fav, worst_adv = max(best_fav, favour), max(worst_adv, adverse)
        if s * (b["close"] - best_close) > 0:
            best_close = b["close"]
        if trailing and atr and best_fav >= TRAIL_START_R * risk:
            candidate = max(fill, best_close - TRAIL_ATR * atr) if s > 0 else min(fill, best_close + TRAIL_ATR * atr)
            trail = max(trail, candidate) if s > 0 else min(trail, candidate)
        if b["ts"][:10] >= time_stop_date and b.get("eod", True):
            return _exit(b, b["close"], "time_stop", fill, risk, s, best_fav, worst_adv, i)
    return {"status": "open", "mfe_r": round(best_fav / risk, 3), "mae_r": round(worst_adv / risk, 3),
            "trail_stop": round(trail, 4)}


def _exit(bar, price, reason, fill, risk, s, fav, adv, i) -> dict:
    return {"status": "closed", "exit_ts": bar["ts"], "exit_price": round(price, 4), "exit_reason": reason,
            "pnl_r": round(s * (price - fill) / risk, 3), "mfe_r": round(fav / risk, 3),
            "mae_r": round(adv / risk, 3), "exit_index": i}


def simulate(*, direction: str, entry: float, stop: float, target: float, atr: float | None,
             bars: Sequence[dict], fill_until: str, time_stop_date: str) -> dict:
    """``bars``: chronological bars from the start of the entry window onward.
    ``fill_until``: last bar ts (exclusive) in which the limit entry may fill.
    ``time_stop_date``: exit at the close of the last bar of this date.
    Daily bars are end-of-day bars; intraday bars mark their last bar of the day
    with ``eod=True`` (the caller sets it)."""
    s = 1 if direction == "long" else -1
    fill = fill_i = None
    for i, b in enumerate(bars):
        if b["ts"] >= fill_until:
            break
        if s * (b["open"] - stop) <= 0:
            return {"status": "no_fill", "reason": "opened beyond the stop"}
        if _touch(b, entry, s, "adverse"):
            fill = min(b["open"], entry) if s > 0 else max(b["open"], entry)
            fill_i = i
            break
    if fill is None:
        if bars and bars[-1]["ts"] >= fill_until:
            return {"status": "no_fill", "reason": "limit not reached in the entry window"}
        return {"status": "pending"}
    base = {"fill_ts": bars[fill_i]["ts"], "fill_price": round(fill, 4), "risk_per_unit": round(s * (fill - stop), 4)}
    if base["risk_per_unit"] <= 0:
        return {"status": "no_fill", "reason": "fill at or beyond the stop"}
    fixed = _run_exit(bars, fill_i, fill, stop, target, s, time_stop_date, atr, trailing=False, fill_bar_done=True)
    trail = _run_exit(bars, fill_i, fill, stop, target, s, time_stop_date, atr, trailing=True, fill_bar_done=True)
    out = {**base, **{k: v for k, v in fixed.items() if k != "exit_index"}}
    out["trail"] = {k: v for k, v in trail.items() if k in ("status", "exit_ts", "exit_price", "exit_reason", "pnl_r")}
    return out


# --- charges (Zerodha equity delivery) --------------------------------------------

def delivery_charges(buy_value: float, sell_value: float) -> float:
    """Approximate all-in delivery charges: STT 0.1% both sides, stamp duty 0.015%
    on buy, NSE transaction 0.00297% + SEBI 0.0001% both sides, 18% GST on
    exchange + SEBI fees, DP charge ₹15.34 per sell. Brokerage ₹0."""
    stt = 0.001 * (buy_value + sell_value)
    stamp = 0.00015 * buy_value
    exch = 0.0000297 * (buy_value + sell_value)
    sebi = 0.000001 * (buy_value + sell_value)
    gst = 0.18 * (exch + sebi)
    dp = 15.34 if sell_value else 0.0
    return round(stt + stamp + exch + sebi + gst + dp, 2)
