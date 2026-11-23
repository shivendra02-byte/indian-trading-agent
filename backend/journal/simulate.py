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
* Exit management (``global.exit_management`` in the YAML) decides the real
  exit: ``fixed`` (stop/target/time stop only), ``breakeven`` (after
  ``breakeven_at_r`` in favour the stop moves to entry + ``breakeven_lock_r``)
  or ``breakeven_trail`` (breakeven as above, then from ``trail_start_r`` the
  stop trails ``trail_atr`` ATR behind the best close, never below breakeven).
  The stop only moves after a bar has closed, and only to a level that bar
  closed beyond: a bar that reaches 1R and closes back below the entry leaves
  the original stop in place until a later bar closes above breakeven.
* Trailing shadow exit: after 1.5R in favour, the stop moves to breakeven, then
  trails 2 ATR behind the best close. Same target and time stop. Recorded next
  to the real exit for comparison, as is the plain fixed exit when the real
  exit is managed.
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


EXIT_MODES = ("fixed", "breakeven", "breakeven_trail")


def _run_exit(bars: Sequence[dict], start: int, fill: float, stop: float, target: float, s: int,
              time_stop_date: str, atr: float | None, trailing: bool, fill_bar_done: bool,
              trail_start_r: float = TRAIL_START_R, trail_atr: float = TRAIL_ATR,
              breakeven_at_r: float | None = None, breakeven_lock_r: float = 0.0) -> dict:
    """``trailing``: trail ``trail_atr`` ATR behind the best close once the trade
    is ``trail_start_r`` in favour (floor: breakeven). ``breakeven_at_r``: move
    the stop to fill + ``breakeven_lock_r`` R once that far in favour."""
    risk = s * (fill - stop)
    best_fav = worst_adv = 0.0                     # price distance from fill
    best_close = fill
    trail = stop
    breakeven = fill + s * breakeven_lock_r * risk

    def reason_for(level: float, gap: bool) -> str:
        if level == stop:
            return "gap_stop" if gap else "stop"
        return "breakeven_stop" if abs(level - breakeven) < 1e-9 else "trail_stop"
    for i in range(start, len(bars)):
        b = bars[i]
        first = i == start and fill_bar_done       # the fill bar: no gap logic, open already used
        if not first:
            if s * (b["open"] - trail) <= 0:
                return _exit(b, b["open"], reason_for(trail, True), fill, risk, s,
                             best_fav, max(worst_adv, s * (fill - b["open"])), i)
            if s * (b["open"] - target) >= 0:
                return _exit(b, b["open"], "target", fill, risk, s, max(best_fav, s * (b["open"] - fill)), worst_adv, i)
        adverse = s * (fill - (b["low"] if s > 0 else b["high"]))
        favour = s * ((b["high"] if s > 0 else b["low"]) - fill)
        if _touch(b, trail, s, "adverse"):
            return _exit(b, trail, reason_for(trail, False), fill, risk, s, best_fav, max(worst_adv, s * (fill - trail)), i)
        if _touch(b, target, s, "favour"):
            return _exit(b, target, "target", fill, risk, s, max(best_fav, s * (target - fill)),
                         max(worst_adv, adverse), i)
        best_fav, worst_adv = max(best_fav, favour), max(worst_adv, adverse)
        if s * (b["close"] - best_close) > 0:
            best_close = b["close"]
        managed = breakeven_at_r is not None
        # Managed exits only move the stop to a level the bar closed beyond: a stop
        # can't be placed on the wrong side of the market, so it waits for a better close.
        if managed and best_fav >= breakeven_at_r * risk and s * (breakeven - trail) > 0 \
                and s * (b["close"] - breakeven) > 0:
            trail = breakeven
        if trailing and atr and best_fav >= trail_start_r * risk and (not managed or s * (trail - breakeven) >= 0):
            floor = breakeven if managed else fill
            candidate = max(floor, best_close - trail_atr * atr) if s > 0 else min(floor, best_close + trail_atr * atr)
            if not managed or s * (b["close"] - candidate) > 0:
                trail = max(trail, candidate) if s > 0 else min(trail, candidate)
        if b["ts"][:10] >= time_stop_date and b.get("eod", True):
            return _exit(b, b["close"], "time_stop", fill, risk, s, best_fav, worst_adv, i)
    state = "initial" if trail == stop else "breakeven" if abs(trail - breakeven) < 1e-9 else "trailing"
    return {"status": "open", "mfe_r": round(best_fav / risk, 3), "mae_r": round(worst_adv / risk, 3),
            "trail_stop": round(trail, 4), "stop_state": state,
            "last_close": round(bars[-1]["close"], 4) if len(bars) > start else None}


def _exit(bar, price, reason, fill, risk, s, fav, adv, i) -> dict:
    return {"status": "closed", "exit_ts": bar["ts"], "exit_price": round(price, 4), "exit_reason": reason,
            "pnl_r": round(s * (price - fill) / risk, 3), "mfe_r": round(fav / risk, 3),
            "mae_r": round(adv / risk, 3), "exit_index": i}


def _managed_kwargs(exit_rule: dict | None) -> dict | None:
    """``_run_exit`` arguments for the real exit; None = fixed exit."""
    rule = exit_rule or {}
    mode = rule.get("mode", "fixed")
    if mode not in EXIT_MODES:
        raise ValueError(f"exit mode must be one of {EXIT_MODES}")
    if mode == "fixed":
        return None
    kw = {"breakeven_at_r": float(rule.get("breakeven_at_r", 1.0)),
          "breakeven_lock_r": float(rule.get("breakeven_lock_r", 0.0)), "trailing": False}
    if mode == "breakeven_trail":
        kw.update(trailing=True, trail_start_r=float(rule.get("trail_start_r", kw["breakeven_at_r"])),
                  trail_atr=float(rule.get("trail_atr", TRAIL_ATR)))
    return kw


def simulate(*, direction: str, entry: float, stop: float, target: float, atr: float | None,
             bars: Sequence[dict], fill_until: str, time_stop_date: str,
             trail_start_r: float = TRAIL_START_R, trail_atr: float = TRAIL_ATR,
             exit_rule: dict | None = None) -> dict:
    """``bars``: chronological bars from the start of the entry window onward.
    ``fill_until``: last bar ts (exclusive) in which the limit entry may fill.
    ``time_stop_date``: exit at the close of the last bar of this date.
    Daily bars are end-of-day bars; intraday bars mark their last bar of the day
    with ``eod=True`` (the caller sets it).
    ``exit_rule``: ``global.exit_management`` as a dict (None = fixed exit).
    ``trail_start_r`` / ``trail_atr``: the trailing *shadow* exit."""
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
    keep = ("status", "exit_ts", "exit_price", "exit_reason", "pnl_r")
    fixed = _run_exit(bars, fill_i, fill, stop, target, s, time_stop_date, atr, trailing=False, fill_bar_done=True)
    trail = _run_exit(bars, fill_i, fill, stop, target, s, time_stop_date, atr, trailing=True, fill_bar_done=True,
                      trail_start_r=trail_start_r, trail_atr=trail_atr)
    managed = _managed_kwargs(exit_rule)
    real = fixed
    if managed is not None:
        real = _run_exit(bars, fill_i, fill, stop, target, s, time_stop_date, atr, fill_bar_done=True, **managed)
    out = {**base, **{k: v for k, v in real.items() if k != "exit_index"}}
    out["trail"] = {k: v for k, v in trail.items() if k in keep}
    if managed is not None:
        out["exit_mode"] = (exit_rule or {}).get("mode")
        out["fixed"] = {k: v for k, v in fixed.items() if k in keep}
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


def futures_charges(buy_value: float, sell_value: float, exchange: str = "NFO") -> float:
    """Approximate Zerodha futures charges for one round trip.

    Brokerage: lower of ₹20 or 0.03% per executed order. NSE futures: STT 0.05%
    on the sell side (from 1 Apr 2026), exchange transaction ~0.00173%. MCX
    (non-agri): CTT 0.01% on the sell side, exchange transaction ~0.0021%.
    Both: SEBI 0.0001%, stamp duty 0.002% on buy, 18% GST on brokerage +
    exchange + SEBI fees."""
    brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    if exchange == "MCX":
        tax, exch_rate = 0.0001 * sell_value, 0.000021
    else:
        tax, exch_rate = 0.0005 * sell_value, 0.0000173
    exch = exch_rate * (buy_value + sell_value)
    sebi = 0.000001 * (buy_value + sell_value)
    stamp = 0.00002 * buy_value
    gst = 0.18 * (brokerage + exch + sebi)
    return round(brokerage + tax + exch + sebi + stamp + gst, 2)
