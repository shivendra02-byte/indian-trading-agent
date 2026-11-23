"""Momentum book — pure rules (no database, no network).

Ranking (weekly): every F&O stock's return over each ``lookbacks_days`` window
ending ``skip_days`` ago, minus NIFTY's over the same window; the score is the
average. Long group = the ``top_n`` strongest, short group = the ``top_n``
weakest. A held position stays while its rank is inside ``buffer_n`` from its
end of the table (hysteresis, so the book doesn't churn).

Entry: daily trend agrees (fast EMA beyond slow EMA, close beyond the slow EMA,
positive (negative for shorts) returns over each ``return_days``), then on hourly
bars a pullback to the hourly fast EMA within ``pullback_bars`` followed by a
break of structure: the first close beyond the extreme of the previous
``structure_bars`` bars.

Exit: initial stop ``stop_atr`` × daily ATR; ATR trail ``trail_atr`` × daily ATR
behind the best close (moves only in favour, on closed bars); daily fast EMA
crossing back through the slow EMA; leaving the buffer group at a re-rank; time
stop after ``max_hold_days`` trading days.
"""

from __future__ import annotations

from typing import Sequence

from backend.signals.indicators import atr, ema


def defaults() -> dict:
    return {
        "long_enabled": True, "short_enabled": False, "lookbacks_days": [63, 126], "skip_days": 21,
        "min_turnover_cr": 100.0, "min_oi_value_cr": 0.0, "top_n": 10, "buffer_n": 20,
        "max_positions_per_side": 3, "ema_fast": 20, "ema_slow": 50, "return_days": [20, 60],
        "pullback_bars": 8, "pullback_touch_atr": 0.25, "structure_bars": 5, "stop_atr": 2.0,
        "trail_atr": 3.0, "max_hold_days": 60, "results_block_days": 3, "rerank_weekday": 0,
    }


def _ret(closes: Sequence[float], end: int, n: int) -> float | None:
    if end - n < 0 or end >= len(closes) or not closes[end - n]:
        return None
    return closes[end] / closes[end - n] - 1


def momentum_score(closes: Sequence[float], bench: Sequence[float], cfg: dict) -> dict | None:
    """Relative strength vs the benchmark, both aligned on the same last bar."""
    end_s, end_b = len(closes) - 1 - cfg["skip_days"], len(bench) - 1 - cfg["skip_days"]
    parts = {}
    for n in cfg["lookbacks_days"]:
        r, rb = _ret(closes, end_s, n), _ret(bench, end_b, n)
        if r is None or rb is None:
            return None
        parts[n] = r - rb
    return {"score": round(100 * sum(parts.values()) / len(parts), 3),
            "parts": {f"rs_{n}d_pct": round(100 * v, 2) for n, v in parts.items()}}


def rank(rows: list[dict], cfg: dict) -> list[dict]:
    """``rows``: {symbol, score, ...} for eligible stocks. Adds rank (1 = strongest),
    rank_from_bottom and side ('long' / 'short' / None)."""
    ordered = sorted(rows, key=lambda r: r["score"], reverse=True)
    n = len(ordered)
    top = min(cfg["top_n"], n // 2)
    for i, r in enumerate(ordered):
        r["rank"], r["rank_from_bottom"] = i + 1, n - i
        r["side"] = "long" if i < top else "short" if i >= n - top else None
    return ordered


def still_in_buffer(row: dict | None, side: str, cfg: dict) -> bool:
    if row is None:
        return False
    place = row["rank"] if side == "long" else row["rank_from_bottom"]
    return place <= cfg["buffer_n"]


def trend_ok(daily: Sequence[dict], s: int, cfg: dict) -> tuple[bool, str]:
    closes = [b["close"] for b in daily]
    need = max(cfg["ema_slow"], *cfg["return_days"]) + 1
    if len(closes) < need:
        return False, "not enough daily bars"
    fast, slow = ema(closes, cfg["ema_fast"])[-1], ema(closes, cfg["ema_slow"])[-1]
    if fast is None or slow is None:
        return False, "EMAs not ready"
    if s * (fast - slow) <= 0:
        return False, "fast EMA not beyond slow EMA"
    if s * (closes[-1] - slow) <= 0:
        return False, "close not beyond slow EMA"
    for n in cfg["return_days"]:
        if s * (closes[-1] / closes[-1 - n] - 1) <= 0:
            return False, f"{n}-day return against the trade"
    return True, ""


def ema_reversed(daily: Sequence[dict], s: int, cfg: dict) -> bool:
    closes = [b["close"] for b in daily]
    fast, slow = ema(closes, cfg["ema_fast"])[-1], ema(closes, cfg["ema_slow"])[-1]
    return fast is not None and slow is not None and s * (fast - slow) < 0


def entry_trigger(hourly: Sequence[dict], s: int, cfg: dict) -> dict | None:
    """Pullback to the hourly fast EMA, then the first close beyond the last
    ``structure_bars`` extreme. The last bar is the signal bar."""
    k, p = cfg["structure_bars"], cfg["pullback_bars"]
    if len(hourly) < max(cfg["ema_slow"], k + p) + 2:
        return None
    closes = [b["close"] for b in hourly]
    e = ema(closes, cfg["ema_fast"])
    a = atr(hourly, 14)
    last = len(hourly) - 1
    if a[last] is None or e[last] is None:
        return None
    prior = hourly[last - k:last]
    level = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
    if s * (closes[last] - level) <= 0:
        return None
    before = hourly[last - k - 1:last - 1]           # the same test one bar earlier: must not have broken
    prev_level = max(b["high"] for b in before) if s > 0 else min(b["low"] for b in before)
    if s * (closes[last - 1] - prev_level) > 0:
        return None
    window = range(last - p, last)
    tol = cfg["pullback_touch_atr"] * a[last]
    touched = [i for i in window if e[i] is not None and
               (hourly[i]["low"] <= e[i] + tol if s > 0 else hourly[i]["high"] >= e[i] - tol)]
    if not touched:
        return None
    span = hourly[touched[0]:last]
    extreme = min(b["low"] for b in span) if s > 0 else max(b["high"] for b in span)
    return {"entry": closes[last], "structure_level": level, "pullback_extreme": extreme, "ts": hourly[last]["ts"]}


def follow(pos: dict, bars: Sequence[dict], cfg: dict) -> dict:
    """Walk closed hourly bars after ``pos['last_ts']``: stop first (a gap through it
    exits at the open), then the ATR trail on each close. Returns updated fields;
    ``exit`` is set when the stop is hit."""
    s = 1 if pos["side"] == "long" else -1
    stop, best = pos["stop"], pos["best"]
    last_ts = pos.get("last_ts")
    for b in bars:
        if last_ts and b["ts"] <= last_ts:
            continue
        last_ts = b["ts"]
        hit = b["low"] <= stop if s > 0 else b["high"] >= stop
        if hit:
            gap = (b["open"] < stop) if s > 0 else (b["open"] > stop)
            price = b["open"] if gap else stop
            reason = "trail_stop" if s * (stop - pos["initial_stop"]) > 0 else "stop"
            return {"stop": stop, "best": best, "last_ts": last_ts,
                    "exit": {"ts": b["ts"], "price": price, "reason": reason}}
        if s * (b["close"] - best) > 0:
            best = b["close"]
        trail = best - s * cfg["trail_atr"] * pos["atr"]
        if s * (trail - stop) > 0:
            stop = trail
    return {"stop": stop, "best": best, "last_ts": last_ts, "exit": None}


def pnl_r(pos: dict, price: float) -> float:
    s = 1 if pos["side"] == "long" else -1
    risk = abs(pos["entry"] - pos["initial_stop"])
    return round(s * (price - pos["entry"]) / risk, 3) if risk else 0.0


def size_lots(capital: float, risk_pct: float, entry: float, stop: float, lot_size: int | None) -> tuple[int, str]:
    """Lots so one trade risks at most ``risk_pct`` of capital; 0 with the reason when
    even one lot risks more (the lot-size problem for expensive stocks)."""
    if not lot_size:
        return 0, "lot size unknown"
    per_lot = abs(entry - stop) * lot_size
    budget = capital * risk_pct / 100
    lots = int(budget // per_lot) if per_lot else 0
    if lots < 1:
        return 0, f"1 lot risks ₹{per_lot:,.0f} > ₹{budget:,.0f} ({risk_pct:g}% of capital)"
    return lots, ""
