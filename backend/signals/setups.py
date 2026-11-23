"""Stages 2–5 of the decision engine for one instrument on one bar:
regime gate → setup detection → trade construction (stop/target) → scoring.

All thresholds come from config/decision_engine.yaml (``get_config()``):
strategy parameters per setup, geometry, scoring weights and the regime
permission matrix. ``evaluate()`` returns one candidate per (setup, direction)
checked, each with ``valid``, ``trackable``, ``reject_reason``, the factor
scores and a ``stages`` list — the structured, timestamped audit trail.

Hard vs soft (``global.selection.mode``):
* ``strict``: structure, higher-timeframe agreement, confirmation/context
  timeframes, geometry and a setup's required regime are hard. Regime
  permission, the RSI turn and the volume multiplier are soft: the candidate is
  still constructed, scored and shadow-tracked, but marked invalid.
* ``scored`` (two-stage): only setup structure and geometry (an identifiable
  stop and at least the minimum R:R) reject here; the safety filters live in the
  scans. Higher timeframe, confirmation/context timeframes and the required
  regime subtract penalty points; regime permission, volume and RSI only change
  the factor scores. Candidates stay valid and are ranked by score.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import atr, ema, rsi, sma, swing_points

SETUPS = ("pullback", "breakout", "range_reversal")
OPTIONAL_SETUPS = ("mean_reversion", "fib_pullback", "choch_flip")      # checked only when the rule set configures them


def active_setups() -> tuple[str, ...]:
    configured = get_config().strategies
    return SETUPS + tuple(name for name in OPTIONAL_SETUPS if name in configured)
MIN_HTF_BARS = 50
_FACTOR_MAX = {"htf_trend": 25, "setup_quality": 25, "volume_oi": 15, "rr_stop": 15,
               "regime_fit": 10, "relative_strength": 10}

# Default shortlist threshold; scans read the live value with ``min_score()``.
SHORTLIST_SCORE = 60


def min_score() -> float:
    return get_config().global_.scoring.min_shortlist_score


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stage(name: str, decision: str, reason: str = "", **data) -> dict:
    """One audit entry: stage name, pass / fail / soft_fail / pending, why, when."""
    entry = {"stage": name, "decision": decision, "reason": reason, "at": _stamp()}
    if data:
        entry["data"] = data
    return entry


def volume_ratio(bars: Sequence[dict], lookback: int, intraday: bool) -> float:
    """Volume of the signal bar vs comparable completed bars. Intraday: the same
    bar slot (time of day) on previous days, so the busy opening hour isn't
    compared with quiet midday hours. Daily/weekly: the previous ``lookback`` bars."""
    vol = bars[-1].get("volume") or 0
    if intraday:
        slot = bars[-1]["ts"][11:16]
        same = [b.get("volume") or 0 for b in bars[:-1] if b["ts"][11:16] == slot][-lookback:]
        ref = same if len(same) >= 5 else [b.get("volume") or 0 for b in bars[-lookback - 1:-1]]
    else:
        ref = [b.get("volume") or 0 for b in bars[-lookback - 1:-1]]
    avg = sum(ref) / len(ref) if ref else 0
    return vol / avg if avg > 0 else 0.0


def _is_intraday(bars: Sequence[dict]) -> bool:
    """True when the bars are intraday (two consecutive bars share a date)."""
    recent = bars[-10:]
    return any(a["ts"][:10] == b["ts"][:10] for a, b in zip(recent, recent[1:]))


def _context(bars: Sequence[dict], strategy: str = "pullback", intraday: bool | None = None) -> dict:
    cfg = get_config()
    st = cfg.strategy(strategy)
    closes = [b["close"] for b in bars]
    if intraday is None:
        intraday = _is_intraday(bars)
    return {
        "closes": closes,
        "e20": ema(closes, st.ema_fast),
        "e50": ema(closes, st.ema_slow),
        "rsi": rsi(closes, st.rsi_period),
        "atr": atr(bars, st.atr_period),
        "vol_ratio": volume_ratio(bars, st.lookback,
                                  intraday and _extra(st, "volume_reference", "same_slot") == "same_slot"),
        "swings": swing_points(bars, cfg.global_.geometry.swing_fractal_bars),
        "strategy": st,
    }


def htf_trend(htf_bars: Sequence[dict]) -> float | None:
    """+1 full uptrend, +0.5 above slow EMA only, 0 mixed, mirrored for down. None = not enough data."""
    if len(htf_bars) < MIN_HTF_BARS:
        return None
    st = get_config().strategy("pullback")
    closes = [b["close"] for b in htf_bars]
    c, e_fast, e_slow = closes[-1], ema(closes, st.ema_fast)[-1], ema(closes, st.ema_slow)[-1]
    if c > e_fast > e_slow:
        return 1.0
    if c < e_fast < e_slow:
        return -1.0
    if c > e_slow:
        return 0.5
    if c < e_slow:
        return -0.5
    return 0.0


def _close_position(bar: dict, s: int) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return (bar["close"] - bar["low"]) / rng if s > 0 else (bar["high"] - bar["close"]) / rng


def _rejection(bar: dict, s: int) -> bool:
    return s * (bar["close"] - bar["open"]) > 0 and _close_position(bar, s) >= 0.6


def _extra(st, key, default):
    return (st.model_extra or {}).get(key, default)


# --- Stage 3: setup detection ----------------------------------------------------

def _pullback(bars, ctx, s) -> tuple[bool, str, int, dict]:
    st = ctx["strategy"]
    c, e20, e50, a = ctx["closes"][-1], ctx["e20"][-1], ctx["e50"][-1], ctx["atr"][-1]
    if not (s * (c - e20) > 0 and s * (c - e50) > 0 and s * (e20 - e50) > 0):
        return False, "not in trend (price/fast EMA/slow EMA not aligned)", 0, {}
    tol = _extra(st, "touch_tolerance_atr", 0.25)
    recent = bars[-3:]
    touched = (min(b["low"] for b in recent) <= e20 + tol * a) if s > 0 else \
              (max(b["high"] for b in recent) >= e20 - tol * a)
    if not touched:
        return False, "no pullback to the fast EMA", 0, {}
    if not _rejection(bars[-1], s):
        return False, "no rejection candle", 0, {}
    r = [v for v in ctx["rsi"][-3:] if v is not None]
    if len(r) < 3:
        return False, "RSI not ready", 0, {}
    lo_l, hi_l = _extra(st, "rsi_band_long", [40, 50])
    lo_s, hi_s = _extra(st, "rsi_band_short", [50, 60])
    band_ok = (lo_l <= min(r) <= hi_l and r[-1] > r[-2]) if s > 0 else (lo_s <= max(r) <= hi_s and r[-1] < r[-2])
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
    return True, "", 5 if band_ok else 0, {"rsi_turn": band_ok,
                                           "volume": ctx["vol_ratio"] >= st.volume_multiplier}


def _breakout(bars, ctx, s, volume_needed) -> tuple[bool, str, int, dict]:
    st = ctx["strategy"]
    n = st.lookback
    prior = bars[-n - 1:-1]
    level = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
    c, a = bars[-1]["close"], ctx["atr"][-1]
    if s * (c - level) <= 0:
        return False, f"no close beyond the {n}-bar {'high' if s > 0 else 'low'}", 0, {}
    bonus = 5 if s * (c - level) >= _extra(st, "strong_close_atr", 0.25) * a else 0
    return True, "", bonus, {"volume": ctx["vol_ratio"] >= volume_needed}


def _bollinger(closes: Sequence[float], n: int, k: float) -> tuple[list, list, list]:
    mid = sma(closes, n)
    upper, lower = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        sd = (sum((c - mid[i]) ** 2 for c in window) / n) ** 0.5
        upper[i], lower[i] = mid[i] + k * sd, mid[i] - k * sd
    return mid, upper, lower


def _mean_reversion(bars, ctx, s) -> tuple[bool, str, int, dict]:
    """Sideways mean reversion: within the last ``probe_bars`` bars price traded
    outside the Bollinger band (``lookback`` bars, ``band_std`` deviations); the
    signal bar closes back inside it with a rejection candle. Stop beyond the
    probe extreme; target the band middle (``target: mean``) or the far band."""
    st = ctx["strategy"]
    n, k = st.lookback, float(_extra(st, "band_std", 2.0))
    probe = int(_extra(st, "probe_bars", 2))
    mid, upper, lower = _bollinger(ctx["closes"], n, k)
    if mid[-1] is None or len(bars) < n + probe + 1:
        return False, "Bollinger band not ready", 0, {}
    band = lower if s > 0 else upper
    last = bars[-1]
    if s * (last["close"] - band[-1]) <= 0:
        return False, "close not back inside the band", 0, {}
    recent = range(len(bars) - probe - 1, len(bars))
    poked = [i for i in recent if band[i] is not None
             and (bars[i]["low"] < band[i] if s > 0 else bars[i]["high"] > band[i])]
    if not poked:
        return False, "no move outside the band", 0, {}
    if s * (mid[-1] - last["close"]) <= 0:
        return False, "already beyond the band middle", 0, {}
    if not _rejection(last, s):
        return False, "no rejection candle", 0, {}
    span = [bars[i] for i in recent]
    ctx["stop_anchor"] = min(b["low"] for b in span) if s > 0 else max(b["high"] for b in span)
    ctx["band"] = (lower[-1], mid[-1], upper[-1])
    r = [v for v in ctx["rsi"][-probe - 1:] if v is not None]
    lo, hi = _extra(st, "rsi_extreme", [30, 70])
    stretched = bool(r) and (min(r) <= lo if s > 0 else max(r) >= hi)
    return True, "", 5 if stretched else 0, {"rsi_extreme": stretched,
                                             "volume": ctx["vol_ratio"] >= st.volume_multiplier}


def _impulse(bars, ctx, s, lookback: int) -> tuple[dict | None, str]:
    """The latest break of structure in direction ``s`` and the impulse leg that made it.

    For longs: the highest high of the last ``lookback`` bars before the signal bar
    (the impulse extreme); the most recent confirmed swing high before it and below
    it (the structure level); the first close above that level (the break); the
    lowest low between the structure swing and the extreme (the impulse origin).
    Shorts mirrored. Only bars before the signal bar define the leg."""
    last = len(bars) - 1
    window = range(max(0, last - lookback), last)
    if not window:
        return None, "not enough bars"
    ext = max(window, key=lambda i: bars[i]["high"]) if s > 0 else min(window, key=lambda i: bars[i]["low"])
    ext_p = bars[ext]["high"] if s > 0 else bars[ext]["low"]
    highs, lows = ctx["swings"]
    peaks = [(i, p) for i, p in (highs if s > 0 else lows) if i < ext and s * (ext_p - p) > 0]
    if not peaks:
        return None, "no break of structure"
    pk_i, pk_p = peaks[-1]
    bos = next((i for i in range(pk_i + 1, ext + 1) if s * (bars[i]["close"] - pk_p) > 0), None)
    if bos is None:
        return None, "no break of structure"
    leg = range(pk_i, ext + 1)
    org = min(leg, key=lambda i: bars[i]["low"]) if s > 0 else max(leg, key=lambda i: bars[i]["high"])
    org_p = bars[org]["low"] if s > 0 else bars[org]["high"]
    if org >= ext:
        return None, "no impulse leg"
    return {"ext": ext, "ext_p": ext_p, "org": org, "org_p": org_p, "level": pk_p, "bos": bos,
            "range": abs(ext_p - org_p)}, ""


def _fib_levels(imp: dict, s: int, frac: float) -> float:
    """Price at a retracement fraction of the impulse (0 = extreme, 1 = origin)."""
    return imp["ext_p"] - s * frac * imp["range"]


def _fib_pullback(bars, ctx, s) -> tuple[bool, str, int, dict]:
    """Trend continuation after a break of structure: price pulls back into the
    ``zone`` retracement of the impulse (default 60–70%) and the signal bar is a
    rejection back out of it. One entry per break: an earlier rejection in the zone
    after the same extreme uses it up. Invalid once a close goes beyond the impulse
    origin. Stop beyond the pullback extreme (``stop: retrace``) or the impulse
    origin (``stop: origin``); target ``target: fixed_r`` at ``target_r`` R."""
    st = ctx["strategy"]
    a = ctx["atr"][-1]
    imp, why = _impulse(bars, ctx, s, st.lookback)
    if imp is None:
        return False, why, 0, {}
    if imp["range"] < float(_extra(st, "min_impulse_atr", 2.0)) * a:
        return False, "impulse too small", 0, {}
    z1, z2 = _extra(st, "zone", [0.6, 0.7])
    near, far = _fib_levels(imp, s, z1), _fib_levels(imp, s, z2)
    last = len(bars) - 1
    after = range(imp["ext"] + 1, last + 1)
    if not after:
        return False, "no pullback yet", 0, {}
    if any(s * (bars[i]["close"] - imp["org_p"]) <= 0 for i in after):
        return False, "pullback went beyond the impulse origin", 0, {}

    def in_zone(i):
        b = bars[i]
        return (b["low"] <= near and b["close"] >= far) if s > 0 else (b["high"] >= near and b["close"] <= far)

    if not in_zone(last):
        return False, "not in the retracement zone", 0, {}
    if not _rejection(bars[last], s):
        return False, "no rejection candle", 0, {}
    if any(in_zone(i) and _rejection(bars[i], s) for i in range(imp["ext"] + 1, last)):
        return False, "zone already used for this break", 0, {}
    pull = [bars[i] for i in after]
    extreme = min(b["low"] for b in pull) if s > 0 else max(b["high"] for b in pull)
    depth = abs(imp["ext_p"] - extreme) / imp["range"]
    ctx["stop_anchor"] = imp["org_p"] if _extra(st, "stop", "retrace") == "origin" else extreme
    ctx["fib"] = {"extreme": round(imp["ext_p"], 2), "origin": round(imp["org_p"], 2),
                  "structure_level": round(imp["level"], 2), "zone": [round(near, 2), round(far, 2)],
                  "depth": round(depth, 3)}
    golden = 0.6 <= depth <= 0.66
    return True, "", 5 if golden else 0, {"golden_zone": golden,
                                          "volume": ctx["vol_ratio"] >= st.volume_multiplier}


def _choch_flip(bars, ctx, s) -> tuple[bool, str, int, dict]:
    """Change of character: a break of structure in the *other* direction pulled
    back into its zone and then failed — the signal bar is the first close beyond
    that impulse's origin. Trade in direction ``s`` (a short after a failed bullish
    structure). Stop beyond the bounce extreme since the zone touch; fixed-R target."""
    st = ctx["strategy"]
    a = ctx["atr"][-1]
    imp, why = _impulse(bars, ctx, -s, st.lookback)
    if imp is None:
        return False, why, 0, {}
    if imp["range"] < float(_extra(st, "min_impulse_atr", 2.0)) * a:
        return False, "impulse too small", 0, {}
    z1, _ = _extra(st, "zone", [0.6, 0.7])
    near = _fib_levels(imp, -s, z1)
    last = len(bars) - 1
    after = list(range(imp["ext"] + 1, last + 1))
    if len(after) < 2:
        return False, "no pullback yet", 0, {}
    beyond = lambda i: s * (bars[i]["close"] - imp["org_p"]) > 0  # noqa: E731  (s = trade direction)
    if not beyond(last):
        return False, "no close beyond the impulse origin", 0, {}
    if any(beyond(i) for i in after[:-1]):
        return False, "origin already broken earlier", 0, {}
    touched = [i for i in after if (bars[i]["low"] <= near if -s > 0 else bars[i]["high"] >= near)]
    if not touched:
        return False, "the failed structure never reached its zone", 0, {}
    since = bars[touched[0]:last + 1]
    ctx["stop_anchor"] = max(b["high"] for b in since) if s < 0 else min(b["low"] for b in since)
    ctx["fib"] = {"extreme": round(imp["ext_p"], 2), "origin": round(imp["org_p"], 2),
                  "structure_level": round(imp["level"], 2), "failed_direction": "long" if s < 0 else "short"}
    return True, "", 0, {"volume": ctx["vol_ratio"] >= st.volume_multiplier}


def _clusters(points, tolerance):
    out = []
    for _, p in points:
        members = [q for _, q in points if abs(q - p) <= tolerance]
        out.append((sum(members) / len(members), len(members)))
    return out


def _range_reversal(bars, ctx, s) -> tuple[bool, str, int, dict]:
    st = ctx["strategy"]
    n, a = st.lookback, ctx["atr"][-1]
    window = bars[-n - 1:-1]
    rng_hi, rng_lo = max(b["high"] for b in window), min(b["low"] for b in window)
    if rng_hi - rng_lo <= _extra(st, "min_range_atr", 2.0) * a:
        return False, "range too narrow", 0, {}
    last = bars[-1]
    pos = (last["close"] - rng_lo) / (rng_hi - rng_lo)
    edge = _extra(st, "edge_fraction", 0.25)
    if (s > 0 and pos > edge) or (s < 0 and pos < 1 - edge):
        return False, "not at the edge of the range (middle excluded)", 0, {}
    highs, lows = ctx["swings"]
    start = len(bars) - n - 1
    pts = [(i, p) for i, p in (lows if s > 0 else highs) if start <= i < len(bars) - 1]
    tol = _extra(st, "level_tolerance_atr", 0.5) * a
    clusters = [c for c in _clusters(pts, tol) if c[1] >= _extra(st, "min_reactions", 2)]
    if not clusters:
        return False, "no level with enough prior reactions", 0, {}
    ctx["range"] = (rng_lo, rng_hi)
    level = min(clusters, key=lambda c: abs(c[0] - last["close"]))[0]
    touched = (last["low"] <= level + tol) if s > 0 else (last["high"] >= level - tol)
    if not touched:
        return False, "price not at the support/resistance level", 0, {}
    if not _rejection(last, s):
        return False, "no rejection candle", 0, {}
    bonus = 0
    if pts:
        i_prev, p_prev = pts[-1]
        r_prev, r_now = ctx["rsi"][i_prev], ctx["rsi"][-1]
        extreme = last["low"] if s > 0 else last["high"]
        if r_prev is not None and r_now is not None and s * (p_prev - extreme) > 0 and s * (r_now - r_prev) > 0:
            bonus = 5
    return True, "", bonus, {"volume": ctx["vol_ratio"] >= st.volume_multiplier}


# --- Stage 5: trade construction (stop / target) ----------------------------------

def geometry(bars, ctx, s, max_stop_pct: float | None = None, min_rr: float | None = None) -> dict:
    g = get_config().global_.geometry
    st = ctx.get("strategy")
    min_rr = min_rr or (st.min_rr if st else get_config().global_.scoring.min_rr)
    swing_window = max(st.lookback if st else 20, 20) + 10
    entry, a = bars[-1]["close"], ctx["atr"][-1]
    highs, lows = ctx["swings"]
    pivots = [(i, p) for i, p in (lows if s > 0 else highs) if s * (entry - p) > 0 and i >= len(bars) - swing_window]
    swing = pivots[-1][1] if pivots else (min(b["low"] for b in bars[-5:]) if s > 0 else max(b["high"] for b in bars[-5:]))
    if ctx.get("stop_anchor") is not None:   # the setup names its own invalidation point
        swing = ctx["stop_anchor"]
    stop = swing - s * g.stop_buffer_atr * a
    if s * (entry - stop) < g.min_stop_atr * a:
        stop = entry - s * g.min_stop_atr * a
    risk = s * (entry - stop)
    result = {"entry": round(entry, 2), "stop": round(stop, 2), "risk_per_unit": round(risk, 2),
              "stop_atr": round(risk / a, 2) if a else None}
    if risk > g.max_stop_atr * a:
        return {**result, "ok": False, "reason": f"stop too wide ({risk / a:.1f} ATR > {g.max_stop_atr})"}
    if max_stop_pct is not None and risk / entry > max_stop_pct:
        return {**result, "ok": False, "reason": f"stop too wide ({100 * risk / entry:.1f}% of price)"}
    target_mode = _extra(st, "target", "levels") if st else "levels"
    edge = name = None
    if target_mode in ("range_opposite", "range_middle") and ctx.get("range"):
        lo, hi = ctx["range"]
        edge = (hi if s > 0 else lo) if target_mode == "range_opposite" else (lo + hi) / 2
        name = "the opposite side of the range" if target_mode == "range_opposite" else "the middle of the range"
    elif target_mode == "fixed_r":
        rr = min(float(_extra(st, "target_r", 2.5)), g.max_rr)
        if rr < min_rr:
            return {**result, "ok": False, "reason": f"target_r {rr:g} below min_rr {min_rr:g}"}
        return {**result, "ok": True, "reason": "", "target": round(entry + s * rr * risk, 2), "rr": round(rr, 2),
                "next_level": None}
    elif target_mode in ("mean", "band_opposite") and ctx.get("band"):
        lo, mid, hi = ctx["band"]
        edge = mid if target_mode == "mean" else (hi if s > 0 else lo)
        name = "the band middle" if target_mode == "mean" else "the opposite band"
    if edge is not None:
        room = s * (edge - 0.1 * s * a - entry)
        if room < min_rr * risk:
            return {**result, "ok": False, "reason": f"no room for {min_rr:g}R before {name} ({edge:.2f})",
                    "next_level": round(edge, 2)}
        target = entry + s * min(room, g.max_rr * risk)
        return {**result, "ok": True, "reason": "", "target": round(target, 2),
                "rr": round(s * (target - entry) / risk, 2), "next_level": round(edge, 2)}
    levels = [p for _, p in (highs if s > 0 else lows) if s * (p - entry) > 0.1 * a]
    history = bars[-251:-1]  # exclude the signal bar itself
    extreme = max(b["high"] for b in history) if s > 0 else min(b["low"] for b in history)
    if s * (extreme - entry) > 0.1 * a:
        levels.append(extreme)
    level = min(levels, key=lambda p: abs(p - entry)) if levels else None
    if level is not None:
        room = s * (level - 0.1 * s * a - entry)
        if room < min_rr * risk:
            return {**result, "ok": False, "reason": f"no room for {min_rr:g}R before the next level ({level:.2f})",
                    "next_level": round(level, 2)}
        target = entry + s * min(room, g.max_rr * risk)
    else:
        target = entry + s * g.no_level_rr * risk
    rr = s * (target - entry) / risk
    return {**result, "ok": True, "reason": "", "target": round(target, 2), "rr": round(rr, 2),
            "next_level": round(level, 2) if level is not None else None}


# --- Stage 4: scoring --------------------------------------------------------------

def _relative_strength_points(bars, benchmark, s) -> tuple[int, float | None]:
    if not benchmark or len(bars) < 21:
        return 0, None
    bench = {b["ts"][:10]: b["close"] for b in benchmark}
    start, end = bars[-21], bars[-1]
    b0, b1 = bench.get(start["ts"][:10]), bench.get(end["ts"][:10])
    if not b0 or not b1:
        return 0, None
    diff = 100 * (end["close"] / start["close"] - b1 / b0) * s
    pts = 10 if diff >= 5 else 7 if diff >= 2 else 4 if diff >= 0 else 0
    return pts, round(diff, 2)


def _volume_oi_points(vol_ratio: float, oi_change_pct: float | None, price_change: float | None, s: int) -> int:
    """Without OI: volume only, up to 15. With futures OI: volume up to 10, plus 5
    when OI rises with price in the trade direction (long/short build-up)."""
    if oi_change_pct is None:
        return 15 if vol_ratio >= 2 else 10 if vol_ratio >= 1.5 else 5 if vol_ratio >= 1.0 else 0
    vol = 10 if vol_ratio >= 2 else 7 if vol_ratio >= 1.5 else 3 if vol_ratio >= 1.0 else 0
    buildup = oi_change_pct >= 1.0 and price_change is not None and s * price_change > 0
    return vol + (5 if buildup else 0)


def score(*, setup, s, htf, quality_bonus, close_pos, vol_ratio, geo, regime_fit, is_stock, rs_points,
          oi_change_pct=None, price_change=None, volume_available=True) -> dict:
    """Factor points on the reference scale, re-weighted by the configured weights.
    Factors that don't apply (relative strength for non-stocks, volume for
    indices) are left out and the score is scaled back to 100."""
    htf_aligned = htf * s
    raw = {
        "htf_trend": 25 if htf_aligned >= 1 else 12 if (htf_aligned > 0 or (setup in ("range_reversal", "mean_reversion", "choch_flip")
                                                                             and htf_aligned == 0)) else 0,
        "setup_quality": min(25, 15 + (5 if close_pos >= 0.75 else 2 if close_pos >= 0.6 else 0) + quality_bonus),
        "volume_oi": _volume_oi_points(vol_ratio, oi_change_pct, price_change, s) if volume_available else None,
        "rr_stop": (10 if geo["rr"] >= 3 else 7 if geo["rr"] >= 2.5 else 5)
                   + (5 if geo["stop_atr"] <= 2 else 2 if geo["stop_atr"] <= 3 else 0),
        "regime_fit": regime_fit,
        "relative_strength": rs_points if is_stock else None,
    }
    weights = get_config().global_.scoring.weights
    factors = {k: (None if v is None else round(v * weights[k] / _FACTOR_MAX[k], 1)) for k, v in raw.items()}
    total = sum(v for v in factors.values() if v is not None)
    possible = sum(weights[k] for k, v in factors.items() if v is not None)
    return {"factors": factors, "score": round(min(100.0, 100 * total / possible), 1) if possible else 0.0}


# --- entry point -------------------------------------------------------------------

def _reject_all(reason: str) -> list[dict]:
    return [{"setup": None, "direction": None, "valid": False, "trackable": False, "reject_reason": reason,
             "stages": [stage("setup", "fail", reason)]}]


def evaluate(bars: Sequence[dict], htf_bars: Sequence[dict], regime: str, *,
             directions=("long", "short"), is_stock=True, benchmark: Sequence[dict] | None = None,
             max_stop_pct: float | None = None, rs_override: dict | None = None,
             oi: dict | None = None, confirmations: dict[str, Sequence[dict]] | None = None,
             context: dict[str, Sequence[dict]] | None = None, min_primary_bars: int = 60,
             volume_available: bool = True, stock_regime: str | None = None) -> list[dict]:
    """Check every setup for each direction.

    ``htf_bars``: the scored higher timeframe (must agree for pullback/breakout).
    ``confirmations``: {timeframe: bars} that must not be against the trade
    (too little data rejects everything).
    ``context``: {timeframe: bars} that must not be against the trade either
    (too little data counts as neutral).
    ``volume_available=False`` (index spot data has no volume): volume is
    recorded as not tested, never fails a check and is left out of the score.
    ``stock_regime``: the instrument's own daily regime. A strategy with
    ``regime_source: stock`` judges its regime requirement, permission and fit
    on it instead of the market (NIFTY) regime.
    ``rs_override``: {"long": (points, diff), "short": ...} computed elsewhere.
    ``oi``: {"change_pct", "price_change"} for futures."""
    cfg = get_config()
    if len(bars) < min_primary_bars:
        return _reject_all(f"only {len(bars)} bars (need {min_primary_bars})")
    htf = htf_trend(htf_bars)
    if htf is None:
        return _reject_all(f"only {len(htf_bars)} higher-timeframe bars (need {MIN_HTF_BARS})")
    confirm_trends = {tf: htf_trend(b) for tf, b in (confirmations or {}).items()}
    missing = [tf for tf, t in confirm_trends.items() if t is None]
    if missing:
        return _reject_all(f"not enough {', '.join(missing)} bars for confirmation")
    context_trends = {tf: htf_trend(b) or 0.0 for tf, b in (context or {}).items()}
    sel = cfg.global_.selection
    scored_mode = sel.mode == "scored"
    setups = active_setups()
    contexts = {name: _context(bars, name) for name in setups}
    if any(c["atr"][-1] is None or c["e50"][-1] is None for c in contexts.values()):
        return _reject_all("indicators not ready")
    def permissions_for(r):
        return {k: v.model_dump() for k, v in cfg.global_.permissions.get(r, {}).items()}

    min_shortlist = cfg.global_.scoring.min_shortlist_score
    out = []
    for setup in setups:
        ctx = dict(contexts[setup])    # per-direction copies below: setups write stop/target hints into it
        st = ctx["strategy"]
        use_stock = _extra(st, "regime_source", "market") == "stock" and stock_regime is not None
        setup_regime = stock_regime if use_stock else regime
        rule = permissions_for(setup_regime).get(setup)
        base_ctx = ctx
        for direction in directions:
            s = 1 if direction == "long" else -1
            ctx = dict(base_ctx)
            stages: list[dict] = []
            cand = {"setup": setup, "direction": direction, "valid": False, "trackable": False, "regime": regime,
                    "setup_regime": setup_regime, "regime_source": "stock" if use_stock else "market",
                    "htf_trend": htf, "confirmations": confirm_trends, "vol_ratio": round(ctx["vol_ratio"], 2),
                    "stages": stages, "rule_version": cfg.rule_version}
            out.append(cand)
            # Stage 2: regime. A required regime (range reversal → SIDEWAYS) is hard;
            # otherwise the permission matrix is a soft check.
            penalties: dict[str, float] = {}
            required = _extra(st, "requires_regime", None)
            if required and setup_regime != required:
                why = f"{setup} requires a {required} regime (now {setup_regime}{' for the stock' if use_stock else ''})"
                if not scored_mode or _extra(st, "requires_regime_hard", False):
                    cand["reject_reason"] = why
                    stages.append(stage("regime", "fail", why))
                    continue
                penalties["required_regime"] = sel.required_regime_penalty
            permitted = bool(rule and direction in rule["directions"]
                             and not (rule.get("with_trend_only") and htf * s < 1))
            hard_block = not permitted and setup_regime in sel.hard_permission_regimes
            stages.append(stage("regime", "pass" if permitted else "fail" if hard_block else "soft_fail",
                                "" if permitted else f"{setup} {direction} not allowed in {setup_regime}"))
            if setup in ("pullback", "breakout", "fib_pullback") and htf * s <= 0:
                if not scored_mode:
                    cand["reject_reason"] = "higher-timeframe trend does not agree"
                    stages.append(stage("setup", "fail", cand["reject_reason"]))
                    continue
                penalties["htf_against"] = sel.htf_against_penalty if htf * s < 0 else sel.htf_against_penalty / 2
            against = [tf for tf, t in {**confirm_trends, **context_trends}.items() if t * s < 0]
            if against:
                if not scored_mode:
                    cand["reject_reason"] = f"{', '.join(against)} trend against the trade"
                    stages.append(stage("setup", "fail", cand["reject_reason"]))
                    continue
                for tf in against:
                    penalties[f"{tf}_against"] = sel.confirmation_against_penalty
            # Stage 3: setup detection.
            volume_needed = st.volume_multiplier
            if setup == "pullback":
                ok, why, bonus, soft = _pullback(bars, ctx, s)
            elif setup == "breakout":
                volume_needed = (rule or {}).get("min_volume_ratio") or st.volume_multiplier
                ok, why, bonus, soft = _breakout(bars, ctx, s, volume_needed)
            elif setup == "mean_reversion":
                ok, why, bonus, soft = _mean_reversion(bars, ctx, s)
            elif setup == "fib_pullback":
                ok, why, bonus, soft = _fib_pullback(bars, ctx, s)
            elif setup == "choch_flip":
                ok, why, bonus, soft = _choch_flip(bars, ctx, s)
            else:
                ok, why, bonus, soft = _range_reversal(bars, ctx, s)
            if not ok:
                cand["reject_reason"] = why
                stages.append(stage("setup", "fail", why))
                continue
            stages.append(stage("setup", "pass", vol_ratio=round(ctx["vol_ratio"], 2)))
            # Stage 5 (trade construction) runs before scoring: the score uses R:R and stop quality.
            geo = geometry(bars, ctx, s, max_stop_pct)
            cand.update({k: v for k, v in geo.items() if k not in ("ok", "reason")})
            if not geo["ok"]:
                cand["reject_reason"] = geo["reason"]
                stages.append(stage("construction", "fail", geo["reason"]))
                continue
            stages.append(stage("construction", "pass", entry=geo["entry"], stop=geo["stop"],
                                target=geo["target"], rr=geo["rr"]))
            if not volume_available and "volume" in soft:
                soft["volume"] = None   # not tested
            soft_checks = {"regime": permitted, **soft}
            if oi is not None:  # recorded for learning; adds score points, never blocks
                soft_checks["oi_buildup"] = bool((oi.get("change_pct") or 0) >= 1.0
                                                 and s * (oi.get("price_change") or 0) > 0)
            if rs_override is not None:
                rs_points, rs_diff = rs_override.get(direction, (0, None))
            else:
                rs_points, rs_diff = _relative_strength_points(bars, benchmark, s) if is_stock else (0, None)
            regime_fit = (10 if rule.get("preferred") else 5) if permitted else 0
            if setup == "mean_reversion" and "band" in ctx:
                cand["band"] = [round(v, 2) for v in ctx["band"]]
            if "fib" in ctx:
                cand["fib"] = ctx["fib"]
            scored = score(setup=setup, s=s, htf=htf, quality_bonus=bonus,
                           close_pos=_close_position(bars[-1], s), vol_ratio=ctx["vol_ratio"], geo=geo,
                           regime_fit=regime_fit, is_stock=is_stock, rs_points=rs_points,
                           oi_change_pct=(oi or {}).get("change_pct"), price_change=(oi or {}).get("price_change"),
                           volume_available=volume_available)
            if penalties:
                scored["score_before_penalties"] = scored["score"]
                scored["score"] = round(max(0.0, scored["score"] - sum(penalties.values())), 1)
                scored["penalties"] = penalties
            failed = []   # checks that make the candidate invalid (strict mode; hard regimes in both)
            if not permitted and (hard_block or not scored_mode):
                failed.append(f"{setup} {direction} not allowed in {setup_regime}")
            if not scored_mode:
                if soft.get("volume") is False:
                    failed.append(f"volume {ctx['vol_ratio']:.1f}x < {volume_needed}x")
                if soft.get("rsi_turn") is False:
                    failed.append("RSI did not turn from its band")
                if soft.get("rsi_extreme") is False and _extra(st, "rsi_extreme_required", False):
                    failed.append("RSI not stretched")
            stages.append(stage("scoring", "pass" if scored["score"] >= min_shortlist else "below_threshold",
                                score=scored["score"], **({"penalties": penalties} if penalties else {})))
            cand.update(scored)
            cand.update({"valid": not failed, "trackable": True, "soft_checks": soft_checks, "context": context_trends,
                         "reject_reason": "; ".join(failed) or None,
                         "risk_mult": (rule or {}).get("risk_mult", 1.0), "rs_vs_nifty_pct": rs_diff,
                         "atr": round(ctx["atr"][-1], 4)})
    return out
