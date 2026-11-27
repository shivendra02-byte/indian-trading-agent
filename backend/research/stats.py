"""Summaries for research trades: the usual R statistics plus the two measures the papers
argue for — the worst quarter (DeePM: judge a rule by its worst windows) and the result in
NIFTY's worst quarters (Sepp & Lucic's "Bear Sharpe": does it help when the market falls?).

Trades are dicts with at least ``pnl_r`` and ``exit_date`` (closed) — see research.journal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Callable, Iterable

from backend.metrics import risk_metrics

MIN_SAMPLE = 30
BEAR_SHARE = 0.16          # the worst 16% of quarters (one sigma), as in Sepp & Lucic


def quarter_of(d: str) -> str:
    day = date.fromisoformat(str(d)[:10])
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def _max_drawdown(rs: list[float]) -> float:
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return round(dd, 2)


def stats(trades: Iterable[dict], min_sample: int = MIN_SAMPLE) -> dict:
    closed = sorted((t for t in trades if t.get("status", "closed") == "closed" and t.get("pnl_r") is not None),
                    key=lambda t: (str(t.get("exit_date") or ""), str(t.get("entry_date") or "")))
    n = len(closed)
    if not n:
        return {"n": 0, "sufficient": False}
    rs = [float(t["pnl_r"]) for t in closed]
    wins = [r for r in rs if r > 0]
    gross_loss = -sum(r for r in rs if r <= 0)
    quarters: dict[str, float] = defaultdict(float)
    for t in closed:
        if t.get("exit_date"):
            quarters[quarter_of(t["exit_date"])] += float(t["pnl_r"])
    days = [(date.fromisoformat(str(t["exit_date"])[:10]) - date.fromisoformat(str(t["entry_date"])[:10])).days
            for t in closed if t.get("exit_date") and t.get("entry_date")]
    worst_q = min(quarters.items(), key=lambda kv: kv[1]) if quarters else None
    return {
        "n": n,
        "sufficient": n >= min_sample,
        "win_rate": round(100 * len(wins) / n, 1),
        "avg_r": round(sum(rs) / n, 3),
        "total_r": round(sum(rs), 2),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_r": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_r": round(-gross_loss / (n - len(wins)), 2) if n > len(wins) else None,
        "max_drawdown_r": _max_drawdown(rs),
        "worst_quarter": worst_q[0] if worst_q else None,
        "worst_quarter_r": round(worst_q[1], 2) if worst_q else None,
        "quarters_positive_pct": round(100 * sum(1 for v in quarters.values() if v > 0) / len(quarters), 0)
        if quarters else None,
        "avg_days": round(sum(days) / len(days), 1) if days else None,
        "pnl_rupees": round(sum(float(t.get("pnl_rupees") or 0) for t in closed), 0),
        **risk_metrics((t.get("exit_date"), float(t["pnl_r"])) for t in closed),
    }


def grouped(trades: Iterable[dict], key: Callable[[dict], str | None]) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        k = key(t)
        if k is not None:
            groups[str(k)].append(t)
    return {k: stats(v) for k, v in sorted(groups.items())}


def bear_quarters(index_bars: list[dict], first: str | None = None, last: str | None = None,
                  share: float = BEAR_SHARE) -> dict:
    """NIFTY's quarterly returns from daily bars and the worst ``share`` of them."""
    by_q: dict[str, list[float]] = {}
    for b in index_bars:
        d = str(b["ts"])[:10]
        if (first and d < first[:10]) or (last and d > last[:10]):
            continue
        q = quarter_of(d)
        by_q.setdefault(q, [b["close"], b["close"]])
        by_q[q][1] = b["close"]
    rets = {}
    prev_close = None
    for q in sorted(by_q):
        start, end = by_q[q]
        base = prev_close or start
        rets[q] = (end / base - 1) * 100 if base else 0.0
        prev_close = end
    if not rets:
        return {"quarters": {}, "bear": []}
    k = max(1, round(len(rets) * share))
    bear = [q for q, _ in sorted(rets.items(), key=lambda kv: kv[1])[:k]]
    return {"quarters": {q: round(v, 2) for q, v in rets.items()}, "bear": sorted(bear)}


def bear_result(trades: Iterable[dict], bear: list[str]) -> dict | None:
    """Average R per quarter in NIFTY's worst quarters vs the other quarters (by exit date)."""
    if not bear:
        return None
    per_q: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.get("status", "closed") == "closed" and t.get("exit_date") and t.get("pnl_r") is not None:
            per_q[quarter_of(t["exit_date"])] += float(t["pnl_r"])
    if not per_q:
        return None
    in_bear = [per_q.get(q, 0.0) for q in bear]
    others = [v for q, v in per_q.items() if q not in bear]
    return {
        "bear_quarters": bear,
        "avg_r_per_bear_quarter": round(sum(in_bear) / len(in_bear), 2),
        "avg_r_per_other_quarter": round(sum(others) / len(others), 2) if others else None,
        "total_r_in_bear_quarters": round(sum(in_bear), 2),
    }


def summary(trades: list[dict], split_date: str | None = None, index_bars: list[dict] | None = None) -> dict:
    """Overall + by year / symbol / direction / exit, train vs test, and the bear-quarter check."""
    closed = [t for t in trades if t.get("status", "closed") == "closed" and t.get("pnl_r") is not None]
    out = {
        "all": stats(trades),
        "open": sum(1 for t in trades if t.get("status") == "open"),
        "open_r": round(sum(float(t.get("pnl_r") or 0) for t in trades if t.get("status") == "open"), 2),
        "by_year": grouped(closed, lambda t: str(t.get("entry_date"))[:4]),
        "by_symbol": grouped(closed, lambda t: t.get("symbol")),
        "by_direction": grouped(closed, lambda t: t.get("direction")),
        "by_exit": grouped(closed, lambda t: t.get("exit_reason")),
    }
    if split_date:
        out["by_period"] = {
            "train": stats(t for t in closed if str(t.get("entry_date")) < split_date),
            "test": stats(t for t in closed if str(t.get("entry_date")) >= split_date),
        }
    if index_bars and closed:
        first = min(str(t["entry_date"]) for t in closed)
        last = max(str(t["exit_date"]) for t in closed)
        bq = bear_quarters(index_bars, first, last)
        out["bear"] = bear_result(closed, bq["bear"])
    return out
