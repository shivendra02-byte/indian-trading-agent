"""Weinstein / Minervini stage analysis on daily bars (agreed 25 Nov 2026). Pure functions.

Stages (from the 200-day SMA and the price around it; numbers from ``global.stage_analysis``):

* **Stage 2 — advancing**: SMA200 rising (its change over ``slope_days`` above
  ``flat_slope_pct``) and the close above it.
* **Stage 4 — declining**: SMA200 falling and the close below it.
* **Stage 3 — topping**: SMA200 still rising but the close below it, or SMA200 flat after an
  advance (the earlier ``trend_days`` slope was rising, or no clear earlier trend and the close
  in the upper half of the 52-week range).
* **Stage 1 — basing**: SMA200 falling but the close above it (a recovery attempt), or SMA200
  flat after a decline (or in the lower half of the range).

Minervini's **trend template** (the Stage 1 → 2 proof he waits for instead of buying lows):
close above SMA150 and SMA200, SMA150 above SMA200, SMA200 rising for at least
``template_rising_days``, SMA50 above both, close above SMA50, at least
``template_above_low_pct`` % above the 52-week low and within ``template_below_high_pct`` %
of the 52-week high.

``top_warnings()`` checks a held long for Minervini's top signals (climax run, churning,
heavy-volume break of the 50-day SMA, SMA200 rolling over, distribution days).
"""

from __future__ import annotations

from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import sma

NAMES = {1: "Stage 1 (basing)", 2: "Stage 2 (advancing)", 3: "Stage 3 (topping)", 4: "Stage 4 (declining)"}


def _cfg(cfg=None):
    return cfg or get_config().global_.stage_analysis


def _slope_pct(series: Sequence[float | None], i: int, n: int) -> float | None:
    if i - n < 0 or series[i] is None or series[i - n] is None or not series[i - n]:
        return None
    return 100 * (series[i] - series[i - n]) / series[i - n]


def rising_days(series: Sequence[float | None], i: int) -> int:
    """Consecutive bars up to ``i`` on which the series rose."""
    n = 0
    while i - n - 1 >= 0 and series[i - n] is not None and series[i - n - 1] is not None \
            and series[i - n] > series[i - n - 1]:
        n += 1
    return n


def classify(bars: Sequence[dict], cfg=None, i: int | None = None, _smas: tuple | None = None) -> dict:
    """Stage of bar ``i`` (default the last), point in time. ``stage`` is None when there is
    not enough history (SMA200 plus the slope window)."""
    c = _cfg(cfg)
    i = len(bars) - 1 if i is None else i
    closes = [b["close"] for b in bars]
    s50, s150, s200 = _smas or (sma(closes, c.sma_fast), sma(closes, c.sma_mid), sma(closes, c.sma_slow))
    slope = _slope_pct(s200, i, c.slope_days)
    if i < 0 or slope is None:
        return {"stage": None, "label": "unknown", "reason": f"needs {c.sma_slow + c.slope_days} daily bars "
                                                             f"(have {i + 1})"}
    close = closes[i]
    year = bars[max(0, i - c.year_days + 1):i + 1]
    hi52, lo52 = max(b["high"] for b in year), min(b["low"] for b in year)
    above = close > s200[i]
    flat = abs(slope) <= c.flat_slope_pct
    if not flat and slope > 0:
        stage, why = (2, "SMA200 rising, close above it") if above else (3, "SMA200 rising but close below it")
    elif not flat:
        stage, why = (4, "SMA200 falling, close below it") if not above else (1, "SMA200 falling but close above it")
    else:
        earlier = _slope_pct(s200, i - c.slope_days, c.trend_days)
        if earlier is not None and abs(earlier) > c.flat_slope_pct:
            stage = 3 if earlier > 0 else 1
            why = f"SMA200 flat after {'an advance' if earlier > 0 else 'a decline'}"
        else:
            mid = (hi52 + lo52) / 2
            stage = 3 if close >= mid else 1
            why = f"SMA200 flat, close in the {'upper' if close >= mid else 'lower'} half of the 52-week range"
    info = {
        "stage": stage, "label": NAMES[stage], "reason": why, "close": round(close, 2),
        "sma50": _r(s50[i]), "sma150": _r(s150[i]), "sma200": _r(s200[i]),
        "sma200_slope_pct": round(slope, 2), "sma200_rising_days": rising_days(s200, i),
        "high_52w": round(hi52, 2), "low_52w": round(lo52, 2),
        "pct_above_low": round(100 * (close / lo52 - 1), 1) if lo52 else None,
        "pct_below_high": round(100 * (1 - close / hi52), 1) if hi52 else None,
    }
    info["trend_template"] = trend_template(info, c)
    return info


def _r(x):
    return round(x, 2) if x is not None else None


def trend_template(info: dict, cfg=None) -> dict:
    """Minervini's trend template from a ``classify()`` result: per-check booleans + ``passed``."""
    c = _cfg(cfg)
    close, s50, s150, s200 = info["close"], info["sma50"], info["sma150"], info["sma200"]
    if None in (s50, s150, s200):
        return {"passed": False, "checks": {}}
    checks = {
        "close_above_150_200": close > s150 and close > s200,
        "sma150_above_200": s150 > s200,
        "sma200_rising": info["sma200_rising_days"] >= c.template_rising_days,
        "sma50_above_150_200": s50 > s150 and s50 > s200,
        "close_above_50": close > s50,
        "off_the_low": info["pct_above_low"] is not None and info["pct_above_low"] >= c.template_above_low_pct,
        "near_the_high": info["pct_below_high"] is not None and info["pct_below_high"] <= c.template_below_high_pct,
    }
    return {"passed": all(checks.values()), "checks": checks}


def stages_series(bars: Sequence[dict], cfg=None) -> list[int | None]:
    """Stage of every bar (point in time), for backtests."""
    c = _cfg(cfg)
    closes = [b["close"] for b in bars]
    smas = (sma(closes, c.sma_fast), sma(closes, c.sma_mid), sma(closes, c.sma_slow))
    return [classify(bars, c, i, smas)["stage"] for i in range(len(bars))]


def allows(info: dict | None, direction: str, long_stages: Sequence[int], short_stages: Sequence[int]) -> tuple[bool, str]:
    """Is a trade in ``direction`` allowed by the daily stage?"""
    if not info or info.get("stage") is None:
        return False, f"daily stage unknown ({(info or {}).get('reason', 'no daily bars')})"
    allowed = long_stages if direction == "long" else short_stages
    if info["stage"] not in allowed:
        return False, f"{direction} not taken in {info['label']}"
    return True, info["label"]


# --- Minervini top signals (exit warnings for longs) ----------------------------------------

def _avg(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def top_warnings(bars: Sequence[dict], cfg=None) -> list[dict]:
    """Top signals on the last daily bar: ``[{kind, detail}]`` (empty = none)."""
    c = _cfg(cfg)
    w = c.top_warnings
    t = len(bars) - 1
    if t < max(c.sma_slow + c.slope_days, w.climax_lookback, w.churn_volume_lookback) + 1:
        return []
    closes = [b["close"] for b in bars]
    vols = [b.get("volume") or 0 for b in bars]
    s50, s200 = sma(closes, c.sma_fast), sma(closes, c.sma_slow)
    has_volume = any(vols[-w.volume_avg_days:])
    avg_vol = _avg(vols[t - w.volume_avg_days:t])
    out: list[dict] = []

    # Climax run: the largest one-day gain of the advance, recently, far above the 50-day SMA.
    gains = [(closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0 for i in range(t - w.climax_lookback + 1, t + 1)]
    best = max(range(len(gains)), key=lambda j: gains[j])
    best_i = t - w.climax_lookback + 1 + best
    ext = 100 * (closes[t] / s50[t] - 1) if s50[t] else 0.0
    if t - best_i < w.climax_window and gains[best] > 0 and ext >= w.climax_extension_pct:
        out.append({"kind": "climax_run", "detail": f"largest daily gain of {w.climax_lookback} days "
                    f"({100 * gains[best]:.1f}%) {t - best_i} day(s) ago, {ext:.0f}% above the 50-day SMA"})

    if has_volume:
        b = bars[t]
        rng = b["high"] - b["low"]
        pos = (b["close"] - b["low"]) / rng if rng > 0 else 0.5
        top_vol = vols[t] >= max(vols[t - w.churn_volume_lookback:t])
        at_high = b["high"] >= max(x["high"] for x in bars[t - 20:t])
        if top_vol and at_high and pos <= w.churn_close_max:
            out.append({"kind": "churning", "detail": f"highest volume of {w.churn_volume_lookback} days at a new "
                        f"high, closed in the bottom {100 * pos:.0f}% of the day's range"})
        if s50[t] and s50[t - 1] and closes[t] < s50[t] and closes[t - 1] >= s50[t - 1] \
                and avg_vol and vols[t] >= w.break_volume_mult * avg_vol:
            out.append({"kind": "break_50dma", "detail": f"closed below the 50-day SMA {s50[t]:.2f} on "
                        f"{vols[t] / avg_vol:.1f}× average volume"})
        heavy_down = heavy_up = 0
        for i in range(t - w.distribution_window + 1, t + 1):
            if avg_vol and vols[i] > avg_vol:
                if closes[i] < closes[i - 1]:
                    heavy_down += 1
                elif closes[i] > closes[i - 1]:
                    heavy_up += 1
        if heavy_down - heavy_up >= w.distribution_excess:
            out.append({"kind": "distribution", "detail": f"{heavy_down} heavy-volume down days vs {heavy_up} up "
                        f"days in {w.distribution_window} sessions"})

    now_slope = _slope_pct(s200, t, c.slope_days)
    before = _slope_pct(s200, t - c.slope_days, c.slope_days)
    if now_slope is not None and before is not None and before > c.flat_slope_pct and now_slope <= 0:
        out.append({"kind": "sma200_rollover", "detail": f"200-day SMA turned down ({now_slope:+.2f}% over "
                    f"{c.slope_days} days, was {before:+.2f}%)"})
    return out
