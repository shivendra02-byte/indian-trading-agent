"""Market regime classifier (daily bars).

Labels, checked in this order:
Thresholds come from config/decision_engine.yaml (global.regime); defaults:
  HIGH_VOL      20-day realised vol > 1.5x its 120-day average
  STRONG_UP     close > EMA20 > EMA50, EMA50 rising over 10 days, ADX(14) >= 25
  STRONG_DOWN   close < EMA20 < EMA50, EMA50 falling over 10 days, ADX(14) >= 25
  MODERATE_UP   as STRONG_UP but adx_moderate_min (20) <= ADX < 25 (agreed 24 Nov 2026)
  MODERATE_DOWN as STRONG_DOWN with the same ADX band
  SIDEWAYS      ADX(14) < 20
  UNSTABLE      anything else
The current label becomes UNSTABLE if the label changed 3+ times in the last 10 days.
A move between STRONG_x and MODERATE_x is not a change (same direction, ADX hovering at 25).
``adx_moderate_min: null`` switches the moderate tier off (those days stay UNSTABLE).

Hysteresis (27 Nov 2026, experiment; live config leaves both off): ``adx_trend_exit`` keeps a
STRONG_x label while ADX stays at or above it (entering still needs ``adx_trend_min``), and
``confirm_days`` accepts a new label only after it has held that many days in a row.

``current_regime(bars, high_vol_trend=True)`` (MCX, agreed 25 Nov 2026): a HIGH_VOL day whose
close and EMAs are aligned (close < EMA20 < EMA50, or the mirror) counts as part of that trend
for the instability check, and the result carries ``trend`` = "down" / "up" so a falling market
whose volatility jumped isn't read as a regime flip.
"""

from __future__ import annotations

from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import adx, ema, realized_vol

HIGH_VOL, STRONG_UP, STRONG_DOWN, SIDEWAYS, UNSTABLE = (
    "HIGH_VOL", "STRONG_UP", "STRONG_DOWN", "SIDEWAYS", "UNSTABLE",
)
MODERATE_UP, MODERATE_DOWN = "MODERATE_UP", "MODERATE_DOWN"
UP_REGIMES = (STRONG_UP, MODERATE_UP)
DOWN_REGIMES = (STRONG_DOWN, MODERATE_DOWN)


def direction(regime: str | None) -> str | None:
    """"up" / "down" for a trending regime (strong or moderate), else None."""
    return "up" if regime in UP_REGIMES else "down" if regime in DOWN_REGIMES else None


def _family(label: str) -> str:
    """Strong and moderate of one direction count as the same label for the instability check."""
    return direction(label) or label


def _changes(labels: Sequence[str]) -> int:
    return sum(1 for a, b in zip(labels, labels[1:]) if _family(a) != _family(b))


def _cfg():
    return get_config().global_.regime


def label_series(bars: Sequence[dict], confirm_days: int | None = None,
                 adx_trend_exit: float | None = None) -> list[str | None]:
    """Daily labels. ``confirm_days`` / ``adx_trend_exit`` override the config's hysteresis
    settings (backtests of variants); None = use the config."""
    cfg = _cfg()
    confirm = int(confirm_days if confirm_days is not None else getattr(cfg, "confirm_days", 1) or 1)
    exit_adx = adx_trend_exit if adx_trend_exit is not None else getattr(cfg, "adx_trend_exit", None)
    st = get_config().strategy("pullback")
    closes = [b["close"] for b in bars]
    e20, e50 = ema(closes, st.ema_fast), ema(closes, st.ema_slow)
    adx14 = adx(bars, cfg.adx_period)
    rv = realized_vol(closes, 20)
    k = cfg.ema_slope_bars
    labels: list[str | None] = [None] * len(bars)
    for i in range(len(bars)):
        window = [v for v in rv[max(0, i - 119):i + 1] if v is not None]
        if None in (e20[i], e50[i], adx14[i], rv[i]) or len(window) < 60 or i < k or e50[i - k] is None:
            continue
        prev = next((l for l in reversed(labels[:i]) if l is not None), None)
        up_min = exit_adx if (exit_adx is not None and prev == STRONG_UP) else cfg.adx_trend_min
        down_min = exit_adx if (exit_adx is not None and prev == STRONG_DOWN) else cfg.adx_trend_min
        if rv[i] > cfg.high_vol_ratio * (sum(window) / len(window)):
            labels[i] = HIGH_VOL
        elif closes[i] > e20[i] > e50[i] and e50[i] > e50[i - k] and adx14[i] >= up_min:
            labels[i] = STRONG_UP
        elif closes[i] < e20[i] < e50[i] and e50[i] < e50[i - k] and adx14[i] >= down_min:
            labels[i] = STRONG_DOWN
        elif (cfg.adx_moderate_min is not None and adx14[i] >= cfg.adx_moderate_min
              and closes[i] > e20[i] > e50[i] and e50[i] > e50[i - k]):
            labels[i] = MODERATE_UP
        elif (cfg.adx_moderate_min is not None and adx14[i] >= cfg.adx_moderate_min
              and closes[i] < e20[i] < e50[i] and e50[i] < e50[i - k]):
            labels[i] = MODERATE_DOWN
        elif adx14[i] < cfg.adx_sideways_max:
            labels[i] = SIDEWAYS
        else:
            labels[i] = UNSTABLE
    return _confirm(labels, confirm) if confirm > 1 else labels


def _confirm(raw: list[str | None], days: int) -> list[str | None]:
    """A label change counts only once the new raw label has held ``days`` days in a row."""
    out: list[str | None] = []
    accepted, run_label, run_len = None, None, 0
    for lab in raw:
        if lab is None:
            out.append(None)
            continue
        run_len = run_len + 1 if lab == run_label else 1
        run_label = lab
        if accepted is None or lab == accepted or run_len >= days:
            accepted = lab
        out.append(accepted)
    return out


def explain_current(bars: Sequence[dict]) -> dict:
    """``current_regime`` plus the numbers behind it (dashboard badge)."""
    out = current_regime(bars)
    cfg = _cfg()
    if len(bars) < cfg.min_daily_bars:
        return out
    st = get_config().strategy("pullback")
    closes = [b["close"] for b in bars]
    e20, e50 = ema(closes, st.ema_fast), ema(closes, st.ema_slow)
    a = adx(bars, cfg.adx_period)
    rv = realized_vol(closes, 20)
    k = cfg.ema_slope_bars
    window = [v for v in rv[-120:] if v is not None]
    base = sum(window) / len(window) if window else None
    r = lambda v, n=2: round(v, n) if v is not None else None  # noqa: E731
    out.update({
        "date": bars[-1]["ts"][:10], "close": r(closes[-1]), "ema_fast": r(e20[-1]), "ema_slow": r(e50[-1]),
        "ema_fast_period": st.ema_fast, "ema_slow_period": st.ema_slow,
        "ema_slow_rising": bool(e50[-1] is not None and e50[-1 - k] is not None and e50[-1] > e50[-1 - k]),
        "adx": r(a[-1], 1), "adx_trend_min": cfg.adx_trend_min, "adx_sideways_max": cfg.adx_sideways_max,
        "adx_moderate_min": cfg.adx_moderate_min,
        "vol_ratio": r(rv[-1] / base) if rv[-1] and base else None, "high_vol_ratio": cfg.high_vol_ratio,
    })
    return out


def regime_at(labels: Sequence[str | None], i: int) -> str:
    """Regime on bar ``i`` from precomputed ``label_series`` labels, with the same
    instability check as ``current_regime`` (backtests: one label pass per stock)."""
    cfg = _cfg()
    if i + 1 < cfg.min_daily_bars:
        return UNSTABLE
    recent = [l for l in labels[max(0, i - cfg.instability_window + 1):i + 1] if l is not None]
    if not recent:
        return UNSTABLE
    changes = _changes(recent)
    return UNSTABLE if changes >= cfg.instability_changes else recent[-1]


def high_vol_trends(bars: Sequence[dict]) -> list[str | None]:
    """Per bar: "up" / "down" when close, EMA20 and EMA50 are aligned, else None."""
    st = get_config().strategy("pullback")
    closes = [b["close"] for b in bars]
    e20, e50 = ema(closes, st.ema_fast), ema(closes, st.ema_slow)
    out: list[str | None] = []
    for c, f, sl in zip(closes, e20, e50):
        if f is None or sl is None:
            out.append(None)
        elif c > f > sl:
            out.append("up")
        elif c < f < sl:
            out.append("down")
        else:
            out.append(None)
    return out


def current_regime(bars: Sequence[dict], high_vol_trend: bool = False, confirm_days: int | None = None,
                   adx_trend_exit: float | None = None) -> dict:
    """Regime for the last bar, with the instability check. Fails closed (UNSTABLE)
    when there isn't enough history. ``high_vol_trend``: see the module docstring;
    ``confirm_days`` / ``adx_trend_exit``: hysteresis overrides (see ``label_series``)."""
    cfg = _cfg()
    if len(bars) < cfg.min_daily_bars:
        return {"regime": UNSTABLE, "reason": f"only {len(bars)} daily bars (need {cfg.min_daily_bars})"}
    labels = label_series(bars, confirm_days, adx_trend_exit)
    n = cfg.instability_window
    trends = high_vol_trends(bars) if high_vol_trend else [None] * len(bars)
    pairs = [(l, t) for l, t in zip(labels[-n:], trends[-n:]) if l is not None]
    if not pairs:
        return {"regime": UNSTABLE, "reason": "indicators not ready"}
    recent = [l for l, _ in pairs]
    families = [t if (l == HIGH_VOL and t) else l for l, t in pairs]
    changes = _changes(families)
    out_trend = (pairs[-1][1] if recent[-1] == HIGH_VOL else direction(recent[-1])) if high_vol_trend else None
    if changes >= cfg.instability_changes:
        return {"regime": UNSTABLE, "reason": f"regime changed {changes} times in {cfg.instability_window} days",
                "raw": recent[-1]}
    out = {"regime": recent[-1], "reason": "", "raw": recent[-1]}
    if high_vol_trend:
        out["trend"] = out_trend
    return out
