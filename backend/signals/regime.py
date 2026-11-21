"""Market regime classifier (daily bars).

Labels, checked in this order:
  HIGH_VOL      20-day realised vol > 1.5x its 120-day average
  STRONG_UP     close > EMA20 > EMA50, EMA50 rising over 10 days, ADX(14) >= 25
  STRONG_DOWN   close < EMA20 < EMA50, EMA50 falling over 10 days, ADX(14) >= 25
  SIDEWAYS      ADX(14) < 20
  UNSTABLE      anything else
The current label becomes UNSTABLE if the label changed 3+ times in the last 10 days.
"""

from __future__ import annotations

from typing import Sequence

from backend.signals.indicators import adx, ema, realized_vol

HIGH_VOL, STRONG_UP, STRONG_DOWN, SIDEWAYS, UNSTABLE = (
    "HIGH_VOL", "STRONG_UP", "STRONG_DOWN", "SIDEWAYS", "UNSTABLE",
)
MIN_BARS = 200

# Which setups may fire in which regime, and in which direction.
# Value: dict setup -> {"directions": (...), "risk_mult": float, "min_volume_ratio": float|None, "preferred": bool}
PERMISSIONS: dict[str, dict[str, dict]] = {
    STRONG_UP: {
        "pullback": {"directions": ("long",), "risk_mult": 1.0, "preferred": True},
        "breakout": {"directions": ("long",), "risk_mult": 1.0, "preferred": True},
    },
    STRONG_DOWN: {
        "pullback": {"directions": ("short",), "risk_mult": 1.0, "preferred": True},
        "breakout": {"directions": ("short",), "risk_mult": 1.0, "preferred": True},
    },
    SIDEWAYS: {
        "range_reversal": {"directions": ("long", "short"), "risk_mult": 1.0, "preferred": True},
        "breakout": {"directions": ("long", "short"), "risk_mult": 1.0, "preferred": False, "min_volume_ratio": 2.0},
    },
    HIGH_VOL: {
        "pullback": {"directions": ("long", "short"), "risk_mult": 0.5, "preferred": False, "with_trend_only": True},
    },
    UNSTABLE: {},
}


def label_series(bars: Sequence[dict]) -> list[str | None]:
    closes = [b["close"] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    adx14 = adx(bars, 14)
    rv = realized_vol(closes, 20)
    labels: list[str | None] = [None] * len(bars)
    for i in range(len(bars)):
        window = [v for v in rv[max(0, i - 119):i + 1] if v is not None]
        if None in (e20[i], e50[i], adx14[i], rv[i]) or len(window) < 60 or i < 10 or e50[i - 10] is None:
            continue
        if rv[i] > 1.5 * (sum(window) / len(window)):
            labels[i] = HIGH_VOL
        elif closes[i] > e20[i] > e50[i] and e50[i] > e50[i - 10] and adx14[i] >= 25:
            labels[i] = STRONG_UP
        elif closes[i] < e20[i] < e50[i] and e50[i] < e50[i - 10] and adx14[i] >= 25:
            labels[i] = STRONG_DOWN
        elif adx14[i] < 20:
            labels[i] = SIDEWAYS
        else:
            labels[i] = UNSTABLE
    return labels


def current_regime(bars: Sequence[dict]) -> dict:
    """Regime for the last bar, with the instability check. Fails closed (UNSTABLE)
    when there isn't enough history."""
    if len(bars) < MIN_BARS:
        return {"regime": UNSTABLE, "reason": f"only {len(bars)} daily bars (need {MIN_BARS})"}
    labels = label_series(bars)
    recent = [l for l in labels[-10:] if l is not None]
    if not recent:
        return {"regime": UNSTABLE, "reason": "indicators not ready"}
    changes = sum(1 for a, b in zip(recent, recent[1:]) if a != b)
    if changes >= 3:
        return {"regime": UNSTABLE, "reason": f"regime changed {changes} times in 10 days", "raw": recent[-1]}
    return {"regime": recent[-1], "reason": "", "raw": recent[-1]}
