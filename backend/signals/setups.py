"""Setups A (trend pullback), B (volume breakout) and C (range reversal),
symmetrical for long and short, plus stop/target geometry and the 100-point score.

``evaluate()`` returns one candidate per (setup, direction) that was checked,
each with ``valid`` and, if invalid, a ``reject_reason`` — so rejected ideas
are logged too. Factor scores are kept separately for the learning review.
"""

from __future__ import annotations

from typing import Sequence

from backend.signals.indicators import atr, ema, rsi, swing_points
from backend.signals.regime import PERMISSIONS

SETUPS = ("pullback", "breakout", "range_reversal")
MIN_ENTRY_BARS = 60
MIN_HTF_BARS = 50
SHORTLIST_SCORE = 60

DEFAULTS = {
    "stop_buffer_atr": 0.25,
    "min_stop_atr": 1.0,
    "max_stop_atr": 3.0,
    "min_rr": 2.0,
    "max_rr": 4.0,
    "no_level_rr": 3.0,
    "breakout_lookback": 20,
    "breakout_volume_ratio": 1.5,
    "range_lookback": 60,
}


def _context(bars: Sequence[dict]) -> dict:
    closes = [b["close"] for b in bars]
    vols = [b.get("volume") or 0 for b in bars]
    prev = vols[-21:-1]
    return {
        "closes": closes,
        "e20": ema(closes, 20),
        "e50": ema(closes, 50),
        "rsi": rsi(closes, 14),
        "atr": atr(bars, 14),
        "vol_ratio": (vols[-1] / (sum(prev) / len(prev))) if prev and sum(prev) > 0 else 0.0,
        "swings": swing_points(bars, 2),
    }


def htf_trend(htf_bars: Sequence[dict]) -> float | None:
    """+1 full uptrend, +0.5 above EMA50 only, 0 mixed, mirrored for down. None = not enough data."""
    if len(htf_bars) < MIN_HTF_BARS:
        return None
    closes = [b["close"] for b in htf_bars]
    c, e20, e50 = closes[-1], ema(closes, 20)[-1], ema(closes, 50)[-1]
    if c > e20 > e50:
        return 1.0
    if c < e20 < e50:
        return -1.0
    if c > e50:
        return 0.5
    if c < e50:
        return -0.5
    return 0.0


def _close_position(bar: dict, s: int) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return (bar["close"] - bar["low"]) / rng if s > 0 else (bar["high"] - bar["close"]) / rng


def _rejection(bar: dict, s: int) -> bool:
    return s * (bar["close"] - bar["open"]) > 0 and _close_position(bar, s) >= 0.6


# --- setup rules ---------------------------------------------------------------

def _pullback(bars, ctx, s) -> tuple[bool, str, int, dict]:
    """Structural checks are hard; the RSI turn is a soft check (logged, shadow-tracked)."""
    c, e20, e50, a = ctx["closes"][-1], ctx["e20"][-1], ctx["e50"][-1], ctx["atr"][-1]
    if not (s * (c - e20) > 0 and s * (c - e50) > 0 and s * (e20 - e50) > 0):
        return False, "not in trend (price/EMA20/EMA50 not aligned)", 0, {}
    recent = bars[-3:]
    touched = (min(b["low"] for b in recent) <= e20 + 0.25 * a) if s > 0 else \
              (max(b["high"] for b in recent) >= e20 - 0.25 * a)
    if not touched:
        return False, "no pullback to EMA20", 0, {}
    if not _rejection(bars[-1], s):
        return False, "no rejection candle", 0, {}
    r = [v for v in ctx["rsi"][-3:] if v is not None]
    if len(r) < 3:
        return False, "RSI not ready", 0, {}
    band_ok = (40 <= min(r) <= 50 and r[-1] > r[-2]) if s > 0 else (50 <= max(r) <= 60 and r[-1] < r[-2])
    soft = {"rsi_turn": band_ok}
    # Trend structure: the higher low (lower high for shorts) that preceded the
    # latest swing high (low) must hold on a closing basis during the pullback.
    highs, lows = ctx["swings"]
    peaks, troughs = (highs, lows) if s > 0 else (lows, highs)
    if not peaks:
        return False, "no confirmed swing to define trend structure", 0, {}
    last_peak_i = peaks[-1][0]
    before = [p for i, p in troughs if i < last_peak_i]
    if not before:
        return False, "no confirmed swing to define trend structure", 0, {}
    if any(s * (b["close"] - before[-1]) <= 0 for b in bars[last_peak_i:]):
        return False, "pullback broke trend structure", 0, {}
    return True, "", 5 if band_ok else 0, soft


def _breakout(bars, ctx, s, min_volume_ratio) -> tuple[bool, str, int, dict]:
    """Close beyond the 20-bar level is hard; the volume ratio is a soft check."""
    n = DEFAULTS["breakout_lookback"]
    prior = bars[-n - 1:-1]
    level = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
    c, a = bars[-1]["close"], ctx["atr"][-1]
    if s * (c - level) <= 0:
        return False, f"no close beyond {n}-bar {'high' if s > 0 else 'low'}", 0, {}
    bonus = 5 if s * (c - level) >= 0.25 * a else 0
    return True, "", bonus, {"volume": ctx["vol_ratio"] >= min_volume_ratio}


def _clusters(points, tolerance):
    """Groups of swing prices within ``tolerance``: [(level, count)]."""
    out = []
    for _, p in points:
        members = [q for _, q in points if abs(q - p) <= tolerance]
        out.append((sum(members) / len(members), len(members)))
    return out


def _range_reversal(bars, ctx, s) -> tuple[bool, str, int, dict]:
    n = DEFAULTS["range_lookback"]
    a = ctx["atr"][-1]
    window = bars[-n - 1:-1]
    rng_hi, rng_lo = max(b["high"] for b in window), min(b["low"] for b in window)
    if rng_hi - rng_lo <= 2 * a:
        return False, "range too narrow", 0, {}
    last = bars[-1]
    pos = (last["close"] - rng_lo) / (rng_hi - rng_lo)
    if (s > 0 and pos > 0.25) or (s < 0 and pos < 0.75):
        return False, "not at the edge of the range (middle 50% excluded)", 0, {}
    highs, lows = ctx["swings"]
    start = len(bars) - n - 1
    pts = [(i, p) for i, p in (lows if s > 0 else highs) if start <= i < len(bars) - 1]
    clusters = [c for c in _clusters(pts, 0.5 * a) if c[1] >= 2]
    if not clusters:
        return False, "no level with 2+ prior reactions", 0, {}
    level = min(clusters, key=lambda c: abs(c[0] - last["close"]))[0]
    touched = (last["low"] <= level + 0.5 * a) if s > 0 else (last["high"] >= level - 0.5 * a)
    if not touched:
        return False, "price not at the support/resistance level", 0, {}
    if not _rejection(last, s):
        return False, "no rejection candle", 0, {}
    # RSI divergence: new extreme in price but not in RSI vs the previous swing.
    bonus = 0
    if pts:
        i_prev, p_prev = pts[-1]
        r_prev, r_now = ctx["rsi"][i_prev], ctx["rsi"][-1]
        extreme = last["low"] if s > 0 else last["high"]
        if r_prev is not None and r_now is not None and s * (p_prev - extreme) > 0 and s * (r_now - r_prev) > 0:
            bonus = 5
    return True, "", bonus, {}


# --- stop / target -------------------------------------------------------------

def geometry(bars, ctx, s, max_stop_pct: float | None = None) -> dict:
    entry, a = bars[-1]["close"], ctx["atr"][-1]
    highs, lows = ctx["swings"]
    pivots = [(i, p) for i, p in (lows if s > 0 else highs) if s * (entry - p) > 0 and i >= len(bars) - 30]
    swing = pivots[-1][1] if pivots else (min(b["low"] for b in bars[-5:]) if s > 0 else max(b["high"] for b in bars[-5:]))
    stop = swing - s * DEFAULTS["stop_buffer_atr"] * a
    if s * (entry - stop) < DEFAULTS["min_stop_atr"] * a:
        stop = entry - s * DEFAULTS["min_stop_atr"] * a
    risk = s * (entry - stop)
    result = {"entry": round(entry, 2), "stop": round(stop, 2), "risk_per_unit": round(risk, 2),
              "stop_atr": round(risk / a, 2) if a else None}
    if risk > DEFAULTS["max_stop_atr"] * a:
        return {**result, "ok": False, "reason": f"stop too wide ({risk / a:.1f} ATR > {DEFAULTS['max_stop_atr']})"}
    if max_stop_pct is not None and risk / entry > max_stop_pct:
        return {**result, "ok": False, "reason": f"stop too wide ({100 * risk / entry:.1f}% of price)"}
    # Next significant level in the trade direction: confirmed swings of the opposite side.
    levels = [p for _, p in (highs if s > 0 else lows) if s * (p - entry) > 0.1 * a]
    history = bars[-251:-1]  # exclude the signal bar itself
    extreme = max(b["high"] for b in history) if s > 0 else min(b["low"] for b in history)
    if s * (extreme - entry) > 0.1 * a:
        levels.append(extreme)
    level = min(levels, key=lambda p: abs(p - entry)) if levels else None
    min_rr, max_rr = DEFAULTS["min_rr"], DEFAULTS["max_rr"]
    if level is not None:
        room = s * (level - 0.1 * s * a - entry)
        if room < min_rr * risk:
            return {**result, "ok": False, "reason": f"no room for {min_rr:.0f}R before the next level ({level:.2f})",
                    "next_level": round(level, 2)}
        target = entry + s * min(room, max_rr * risk)
    else:
        target = entry + s * DEFAULTS["no_level_rr"] * risk
    rr = s * (target - entry) / risk
    return {**result, "ok": True, "reason": "", "target": round(target, 2), "rr": round(rr, 2),
            "next_level": round(level, 2) if level is not None else None}


# --- scoring -------------------------------------------------------------------

def _relative_strength_points(bars, benchmark, s) -> tuple[int, float | None]:
    if not benchmark:
        return 0, None
    bench = {b["ts"][:10]: b["close"] for b in benchmark}
    start, end = bars[-21], bars[-1]
    b0, b1 = bench.get(start["ts"][:10]), bench.get(end["ts"][:10])
    if not b0 or not b1:
        return 0, None
    diff = 100 * (end["close"] / start["close"] - b1 / b0) * s
    pts = 10 if diff >= 5 else 7 if diff >= 2 else 4 if diff >= 0 else 0
    return pts, round(diff, 2)


def score(*, setup, s, htf, quality_bonus, close_pos, vol_ratio, geo, regime_fit, is_stock, rs_points) -> dict:
    htf_aligned = htf * s
    factors = {
        "htf_trend": 25 if htf_aligned >= 1 else 12 if (htf_aligned > 0 or (setup == "range_reversal" and htf_aligned == 0)) else 0,
        "setup_quality": 15 + (5 if close_pos >= 0.75 else 2 if close_pos >= 0.6 else 0) + quality_bonus,
        "volume_oi": 15 if vol_ratio >= 2 else 10 if vol_ratio >= 1.5 else 5 if vol_ratio >= 1.0 else 0,
        "rr_stop": (10 if geo["rr"] >= 3 else 7 if geo["rr"] >= 2.5 else 5)
                   + (5 if geo["stop_atr"] <= 2 else 2 if geo["stop_atr"] <= 3 else 0),
        "regime_fit": regime_fit,
        "relative_strength": rs_points if is_stock else None,
    }
    raw = sum(v for v in factors.values() if v is not None)
    total = raw if is_stock else raw * 100 / 90
    return {"factors": factors, "score": round(min(total, 100), 1)}


# --- entry point ---------------------------------------------------------------

def evaluate(bars: Sequence[dict], htf_bars: Sequence[dict], regime: str, *,
             directions=("long", "short"), is_stock=True, benchmark: Sequence[dict] | None = None,
             max_stop_pct: float | None = None) -> list[dict]:
    """Check every setup allowed in ``regime`` for each direction."""
    if len(bars) < MIN_ENTRY_BARS:
        return [{"setup": None, "direction": None, "valid": False,
                 "reject_reason": f"only {len(bars)} bars (need {MIN_ENTRY_BARS})"}]
    htf = htf_trend(htf_bars)
    if htf is None:
        return [{"setup": None, "direction": None, "valid": False,
                 "reject_reason": f"only {len(htf_bars)} higher-timeframe bars (need {MIN_HTF_BARS})"}]
    ctx = _context(bars)
    if ctx["atr"][-1] is None or ctx["e50"][-1] is None:
        return [{"setup": None, "direction": None, "valid": False, "reject_reason": "indicators not ready"}]
    allowed = PERMISSIONS.get(regime, {})
    out = []
    for setup in SETUPS:
        rule = allowed.get(setup)
        for direction in directions:
            s = 1 if direction == "long" else -1
            cand = {"setup": setup, "direction": direction, "valid": False, "trackable": False, "regime": regime,
                    "htf_trend": htf, "vol_ratio": round(ctx["vol_ratio"], 2)}
            out.append(cand)
            if setup in ("pullback", "breakout") and htf * s <= 0:
                cand["reject_reason"] = "higher-timeframe trend does not agree"
                continue
            # Regime permission is a soft check: a setup the regime doesn't allow is
            # still evaluated and shadow-tracked, so the learning engine can measure it.
            permitted = bool(rule and direction in rule["directions"]
                             and not (rule.get("with_trend_only") and htf * s < 1))
            volume_needed = (rule or {}).get("min_volume_ratio") or DEFAULTS["breakout_volume_ratio"]
            if setup == "pullback":
                ok, why, bonus, soft = _pullback(bars, ctx, s)
            elif setup == "breakout":
                ok, why, bonus, soft = _breakout(bars, ctx, s, volume_needed)
            else:
                ok, why, bonus, soft = _range_reversal(bars, ctx, s)
            if not ok:
                cand["reject_reason"] = why
                continue
            geo = geometry(bars, ctx, s, max_stop_pct)
            cand.update({k: v for k, v in geo.items() if k not in ("ok", "reason")})
            if not geo["ok"]:
                cand["reject_reason"] = geo["reason"]
                continue
            soft_checks = {"regime": permitted, **soft}
            rs_points, rs_diff = _relative_strength_points(bars, benchmark, s) if is_stock else (0, None)
            regime_fit = (10 if rule.get("preferred") else 5) if permitted else 0
            scored = score(setup=setup, s=s, htf=htf, quality_bonus=bonus,
                           close_pos=_close_position(bars[-1], s), vol_ratio=ctx["vol_ratio"], geo=geo,
                           regime_fit=regime_fit, is_stock=is_stock, rs_points=rs_points)
            failed = []
            if not permitted:
                failed.append(f"{setup} {direction} not allowed in {regime}")
            if soft.get("volume") is False:
                failed.append(f"volume {ctx['vol_ratio']:.1f}x < {volume_needed}x")
            if soft.get("rsi_turn") is False:
                failed.append("RSI did not turn from the 40-50 zone" if s > 0 else "RSI did not turn from the 50-60 zone")
            cand.update(scored)
            cand.update({"valid": not failed, "trackable": True, "soft_checks": soft_checks,
                         "reject_reason": "; ".join(failed) or None,
                         "risk_mult": (rule or {}).get("risk_mult", 1.0), "rs_vs_nifty_pct": rs_diff,
                         "atr": round(ctx["atr"][-1], 4)})
    return out
