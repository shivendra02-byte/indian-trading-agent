"""Multi-timeframe entry for the index option spreads (agreed 24 Nov 2026). Pure functions.

The daily regime gives permission and direction; the entry is timed intraday on the trend
index's completed bars:

* **Daily** — ``signals.regime``: STRONG/MODERATE_UP → bullish spreads only, STRONG/MODERATE_DOWN →
  bearish only, SIDEWAYS → iron condor, HIGH_VOL / UNSTABLE → nothing.
* **4H confirms** (completed session-anchored 4H bars): close beyond EMA20 beyond EMA50 in the
  trade's direction, and (``h4_structure``) the last two confirmed 4H swing lows rising (highs
  falling for bearish). Fewer than two swings → not checked.
* **1H setup** (completed 1H bars): *pullback* — within the last ``pullback_bars`` bars one bar
  reached EMA20 (within ``pullback_touch_atr`` ATR) while every close held beyond EMA50; or
  *breakout* — a close through the last confirmed 1H swing high (low) within those bars. Gives the
  invalidation level: the pullback's extreme, or the swing on the other side before the break.
* **15m trigger** (completed 15-min bars): the first close beyond the last ``trigger_lookback``
  bars' extreme, in the trade's direction (a bullish / bearish candle) and beyond the 15m EMA20.
* **Sideways** — no 4H/15m check: an iron condor on a 1H *range rejection*: over the previous
  ``range_bars`` 1H bars, the last bar reached the outer ``range_edge_pct`` % of the range and
  closed back inside, against the touched edge. Invalid once a 1H bar closes outside the range.

Exits are checked on each 1H close (``invalidated()``): beyond the invalidation level → close.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Sequence

from backend.signals.indicators import atr, ema, swing_points
from backend.signals.regime import direction as regime_direction

NSE_CLOSE = time(15, 30)
DEFAULTS = {
    "first_signal": "09:30", "last_signal": "14:45", "ema_fast": 20, "ema_slow": 50, "h4_structure": True,
    "pullback_bars": 6, "pullback_touch_atr": 0.25, "trigger_lookback": 3, "range_bars": 30,
    "range_edge_pct": 20, "strike_range_pct": 8, "max_leg_spread_pct": 5.0, "min_leg_spread_points": 1.0,
}


def bar_end(bar: dict, minutes: int) -> datetime:
    start = datetime.fromisoformat(bar["ts"])
    end = start + timedelta(minutes=minutes)
    close = datetime.combine(start.date(), NSE_CLOSE)
    return min(end, close)


def completed(bars: Sequence[dict], minutes: int, now: datetime) -> list[dict]:
    return [b for b in bars if bar_end(b, minutes) <= now]


def completed_4h(h4: Sequence[dict], now: datetime) -> list[dict]:
    return completed(h4, 240, now)


def _sign(d: str) -> int:
    return 1 if d == "up" else -1


def h4_confirm(h4: Sequence[dict], d: str, cfg: dict) -> tuple[bool, str]:
    if len(h4) < cfg["ema_slow"] + 1:
        return False, f"only {len(h4)} completed 4H bars"
    closes = [b["close"] for b in h4]
    e20, e50 = ema(closes, cfg["ema_fast"])[-1], ema(closes, cfg["ema_slow"])[-1]
    s = _sign(d)
    if not (s * (closes[-1] - e20) > 0 and s * (e20 - e50) > 0):
        side = "above" if s > 0 else "below"
        return False, f"4H close {closes[-1]:.0f} not {side} EMA{cfg['ema_fast']} {e20:.0f} {side} EMA{cfg['ema_slow']} {e50:.0f}"
    if cfg["h4_structure"]:
        highs, lows = swing_points(h4, 2)
        pts = lows if s > 0 else highs
        if len(pts) >= 2 and s * (pts[-1][1] - pts[-2][1]) <= 0:
            what = "higher lows" if s > 0 else "lower highs"
            return False, f"4H structure: no {what} ({pts[-2][1]:.0f} → {pts[-1][1]:.0f})"
    return True, "4H agrees"


def h1_setup(h1: Sequence[dict], d: str, cfg: dict) -> tuple[dict | None, str]:
    """``{"kind": "pullback" | "breakout", "invalid": level}`` or why not."""
    n = cfg["pullback_bars"]
    if len(h1) < cfg["ema_slow"] + n + 5:
        return None, f"only {len(h1)} completed 1H bars"
    closes = [b["close"] for b in h1]
    e20, e50, a = ema(closes, cfg["ema_fast"]), ema(closes, cfg["ema_slow"]), atr(h1, 14)
    s = _sign(d)
    recent = range(len(h1) - n, len(h1))
    held = all(s * (closes[i] - e50[i]) > 0 for i in recent)
    if held:
        touched = [i for i in recent if a[i] is not None and
                   (h1[i]["low"] <= e20[i] + cfg["pullback_touch_atr"] * a[i] if s > 0
                    else h1[i]["high"] >= e20[i] - cfg["pullback_touch_atr"] * a[i])]
        if touched:
            extreme = (min(h1[i]["low"] for i in recent) if s > 0 else max(h1[i]["high"] for i in recent))
            return {"kind": "pullback", "invalid": round(extreme, 2)}, f"1H pullback to EMA{cfg['ema_fast']}"
    highs, lows = swing_points(h1, 2)
    level_pts, other_pts = (highs, lows) if s > 0 else (lows, highs)
    for i in recent:
        before = [p for p in level_pts if p[0] < i - 2]          # confirmed by bar i
        if not before:
            continue
        level = before[-1][1]
        if s * (closes[i] - level) > 0 and s * (closes[i - 1] - level) <= 0:
            opp = [p for p in other_pts if p[0] < i]
            invalid = opp[-1][1] if opp else (min(b["low"] for b in h1[i - n:i]) if s > 0 else max(b["high"] for b in h1[i - n:i]))
            return {"kind": "breakout", "invalid": round(invalid, 2), "level": round(level, 2)}, \
                f"1H breakout of {level:.0f}"
    if not held:
        return None, f"1H closed {'below' if s > 0 else 'above'} EMA{cfg['ema_slow']} in the last {n} bars, no breakout"
    return None, f"no 1H pullback to EMA{cfg['ema_fast']} or breakout in the last {n} bars"


def m15_trigger(m15: Sequence[dict], d: str, cfg: dict) -> tuple[bool, str]:
    k = cfg["trigger_lookback"]
    if len(m15) < max(cfg["ema_fast"], k) + 2:
        return False, f"only {len(m15)} completed 15m bars"
    s = _sign(d)
    closes = [b["close"] for b in m15]
    e20 = ema(closes, cfg["ema_fast"])

    def fires(i: int) -> bool:
        prior = m15[i - k:i]
        edge = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
        bar = m15[i]
        return (s * (bar["close"] - edge) > 0 and s * (bar["close"] - bar["open"]) > 0
                and e20[i] is not None and s * (bar["close"] - e20[i]) > 0)

    last = len(m15) - 1
    if not fires(last):
        return False, "waiting for the 15m trigger"
    if fires(last - 1):
        return False, "15m trigger already fired on the previous bar"
    return True, f"15m close {closes[-1]:.0f} beyond the last {k} bars"


def range_rejection(h1: Sequence[dict], cfg: dict) -> tuple[dict | None, str]:
    n = cfg["range_bars"]
    if len(h1) < n + 1:
        return None, f"only {len(h1)} completed 1H bars"
    window, bar = h1[-n - 1:-1], h1[-1]
    hi, lo = max(b["high"] for b in window), min(b["low"] for b in window)
    width = hi - lo
    if width <= 0:
        return None, "no range"
    edge = cfg["range_edge_pct"] / 100 * width
    if not (lo < bar["close"] < hi):
        return None, "1H closed outside the range"
    if bar["high"] >= hi - edge and bar["close"] < hi - edge and bar["close"] < bar["open"]:
        side = "top"
    elif bar["low"] <= lo + edge and bar["close"] > lo + edge and bar["close"] > bar["open"]:
        side = "bottom"
    else:
        return None, f"no 1H rejection at the range edges ({lo:.0f}–{hi:.0f})"
    return {"kind": "range_rejection", "side": side, "invalid": {"above": round(hi, 2), "below": round(lo, 2)}}, \
        f"1H rejection at the range {side} ({lo:.0f}–{hi:.0f})"


def evaluate(regime: str, h1: Sequence[dict], h4: Sequence[dict], m15: Sequence[dict], now: datetime,
             cfg: dict) -> dict:
    """Alignment of every timeframe and, when all agree, the signal. Bars must be completed bars."""
    cfg = {**DEFAULTS, **(cfg or {})}
    d = regime_direction(regime)
    out = {"regime": regime, "bias": d or ("range" if regime == "SIDEWAYS" else None), "h4": None, "h1": None,
           "m15": None, "signal": None, "at": now.isoformat(timespec="minutes")}
    first, last = time.fromisoformat(cfg["first_signal"]), time.fromisoformat(cfg["last_signal"])

    def in_window(bars: Sequence[dict], minutes: int) -> bool:
        """The window applies to the end of the bar that triggers (fixed 26 Nov 2026: it was compared
        with the clock, so the 14:30–14:45 bar, scanned ~14:47, could never trigger). The bar must be
        from today's session."""
        if not bars:
            return False
        end = bar_end(bars[-1], minutes)
        return end.date() == now.date() and first <= end.time() <= last

    if regime == "SIDEWAYS":
        setup, why = range_rejection(h1, cfg)
        out["h1"] = {"ok": bool(setup), "note": why}
        out["h4"] = {"ok": None, "note": "not used in a sideways market"}
        out["m15"] = {"ok": None, "note": "condor enters on the 1H close"}
        if setup and in_window(h1, 60):
            out["signal"] = {"direction": "range", **setup, "signal_ts": h1[-1]["ts"]}
        elif setup:
            out["reason"] = f"outside the entry window ({cfg['first_signal']}–{cfg['last_signal']} bar end)"
        else:
            out["reason"] = why
        return out
    if not d:
        out["reason"] = f"no spread in a {regime} regime"
        return out
    ok4, why4 = h4_confirm(h4, d, cfg)
    out["h4"] = {"ok": ok4, "note": why4}
    setup, why1 = h1_setup(h1, d, cfg)
    out["h1"] = {"ok": bool(setup), "note": why1}
    ok15, why15 = m15_trigger(m15, d, cfg)
    out["m15"] = {"ok": ok15, "note": why15}
    if not ok4:
        out["reason"] = why4
    elif not setup:
        out["reason"] = why1
    elif not ok15:
        out["reason"] = why15
    elif not in_window(m15, 15):
        out["reason"] = f"outside the entry window ({cfg['first_signal']}–{cfg['last_signal']} bar end)"
    else:
        out["signal"] = {"direction": d, **setup, "signal_ts": m15[-1]["ts"], "trigger_close": m15[-1]["close"]}
    return out


def invalidated(invalid, direction: str, close: float) -> bool:
    """Has a 1H close passed the stored invalidation level?"""
    if invalid is None or close is None:
        return False
    if isinstance(invalid, dict):          # iron condor: outside the range either way
        return close > invalid["above"] or close < invalid["below"]
    return close < invalid if direction == "up" else close > invalid
