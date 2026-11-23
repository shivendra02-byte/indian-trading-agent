"""Risk-adjusted performance measures for any list of trade results in R.

- ``sharpe_trade`` / ``sortino_trade``: mean R per trade divided by the standard
  deviation of R (Sortino: by the downside deviation, losses only). Comparable
  between strategies with similar holding periods.
- ``sharpe_annual`` / ``sortino_annual``: from a daily equity curve in R (each
  trade's R booked on its exit day, zero on weekdays without exits), annualised
  with sqrt(252). Comparable across strategies with different holding periods.
- ``calmar``: R per year divided by the largest peak-to-trough fall of
  cumulative R (trades in exit order).

All in R (1R = the planned risk), before tax. With fewer than 30 trades every
figure is noise; callers already flag that.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable

TRADING_DAYS = 252


def _std(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _downside(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum(min(v, 0.0) ** 2 for v in values) / (len(values) - 1))


def _ratio(num: float, den: float, digits: int = 2) -> float | None:
    return round(num / den, digits) if den > 1e-12 else None


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def risk_metrics(results: Iterable[tuple[str | None, float]]) -> dict:
    """``results``: (exit date as ISO text or None, R) pairs."""
    pairs = [(str(d)[:10] if d else None, float(r)) for d, r in results if r is not None]
    rs = [r for _, r in pairs]
    if len(rs) < 2:
        return {"sharpe_trade": None, "sortino_trade": None, "sharpe_annual": None,
                "sortino_annual": None, "calmar": None}
    mean = sum(rs) / len(rs)
    out = {"sharpe_trade": _ratio(mean, _std(rs, mean), 3),
           "sortino_trade": _ratio(mean, _downside(rs), 3),
           "sharpe_annual": None, "sortino_annual": None, "calmar": None}
    dated = sorted((d, r) for d, r in pairs if d)
    if len(dated) < 2:
        return out
    try:
        first, last = date.fromisoformat(dated[0][0]), date.fromisoformat(dated[-1][0])
    except ValueError:
        return out
    by_day: dict[str, float] = {}
    for d, r in dated:
        by_day[d] = by_day.get(d, 0.0) + r
    curve = [by_day.get(d.isoformat(), 0.0) for d in _weekdays(first, last)]
    if len(curve) >= 2:
        m = sum(curve) / len(curve)
        root = math.sqrt(TRADING_DAYS)
        sd, dd_dev = _std(curve, m), _downside(curve)
        out["sharpe_annual"] = round(m / sd * root, 2) if sd > 1e-12 else None
        out["sortino_annual"] = round(m / dd_dev * root, 2) if dd_dev > 1e-12 else None
    equity = peak = max_dd = 0.0
    for _, r in dated:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    years = max((last - first).days, 1) / 365.25
    out["calmar"] = _ratio(sum(rs) / years, max_dd)
    return out
