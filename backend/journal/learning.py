"""Learning report over closed journal records (no AI, no rule changes).

Answers the six agreed questions with sample sizes. Anything with fewer than
MIN_SAMPLE closed trades is marked insufficient. This only *reports*; rule
changes go through backtest + out-of-sample paper validation and your approval.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from backend.journal.store import list_records
from backend.metrics import risk_metrics

MIN_SAMPLE = 30
TRENDING = ("STRONG_UP", "STRONG_DOWN")


def stats(rows: Iterable[dict]) -> dict:
    rows = [r for r in rows if r.get("pnl_r") is not None]
    n = len(rows)
    if not n:
        return {"n": 0, "sufficient": False}
    wins = [r["pnl_r"] for r in rows if r["pnl_r"] > 0]
    losses = [r["pnl_r"] for r in rows if r["pnl_r"] <= 0]
    gross_loss = -sum(losses)
    return {
        "n": n,
        "sufficient": n >= MIN_SAMPLE,
        "win_rate": round(100 * len(wins) / n, 1),
        "avg_r": round(sum(r["pnl_r"] for r in rows) / n, 3),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "avg_mfe_r": round(sum(r.get("mfe_r") or 0 for r in rows) / n, 2),
        "avg_mae_r": round(sum(r.get("mae_r") or 0 for r in rows) / n, 2),
        "pnl_rupees": round(sum(r.get("pnl_rupees") or 0 for r in rows), 0),
        **risk_metrics((r.get("exit_ts"), r["pnl_r"]) for r in rows),
    }


def _group(rows, key) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {k: stats(v) for k, v in groups.items()}


def _best(groups: dict, reverse: bool) -> dict | None:
    ranked = sorted(((k, v) for k, v in groups.items() if v["n"]), key=lambda kv: kv[1]["avg_r"], reverse=reverse)
    if not ranked:
        return None
    key, value = ranked[0]
    return {"group": key, **value}


def _vol_bucket(r) -> str:
    v = (r.get("extra") or {}).get("vol_ratio")
    if v is None:
        return "unknown"
    return "<1.0x" if v < 1 else "1.0-1.5x" if v < 1.5 else "1.5-2.0x" if v < 2 else ">=2.0x"


def report(segment: str | None = None) -> dict:
    where, params = "track_status = 'closed'", ()
    if segment:
        where, params = where + " AND segment = ?", (segment,)
    closed = list_records(where, params, limit=100_000)
    all_records = list_records("1=1" if not segment else "segment = ?", params[-1:] if segment else (), limit=100_000)

    by_setup_regime = _group(closed, lambda r: f"{r['setup']} | {r['regime']}")
    trending = {k: v for k, v in _group([r for r in closed if r["regime"] in TRENDING], lambda r: r["setup"]).items()}
    sideways = _group([r for r in closed if r["regime"] == "SIDEWAYS"], lambda r: r["setup"])
    breakouts = [r for r in closed if r["setup"] == "breakout"]
    pullbacks = [r for r in closed if r["setup"] == "pullback"]
    rejected = [r for r in closed if r["decision"] not in ("approved",)]
    profitable_rejected = [r for r in rejected if r["pnl_r"] > 0]

    return {
        "min_sample": MIN_SAMPLE,
        "records": len(all_records),
        "closed": len(closed),
        "overall": stats(closed),
        "q1_best_setup_trending": {"by_setup": trending, "best": _best(trending, True)},
        "q2_worst_setup_sideways": {"by_setup": sideways, "worst": _best(sideways, False)},
        "q3_volume_confirmation": {
            "passed_1_5x": stats(r for r in breakouts if (r.get("soft_checks") or {}).get("volume") is True),
            "failed_1_5x": stats(r for r in breakouts if (r.get("soft_checks") or {}).get("volume") is False),
            "by_volume_ratio": _group(breakouts, _vol_bucket),
        },
        "q4_rsi_filter": {
            "rsi_turn_passed": stats(r for r in pullbacks if (r.get("soft_checks") or {}).get("rsi_turn") is True),
            "rsi_turn_failed": stats(r for r in pullbacks if (r.get("soft_checks") or {}).get("rsi_turn") is False),
        },
        "q5_agents": {"note": "Available once the AI panel is live (Phase 4): stance accuracy and calibration per agent."},
        "q6_rejected_but_profitable": {
            "by_decision": _group(rejected, lambda r: r["decision"]),
            "profitable_count": len(profitable_rejected),
            "top_examples": sorted(
                ({k: r.get(k) for k in ("symbol", "signal_date", "setup", "regime", "decision", "reject_reason",
                                        "score", "pnl_r", "exit_reason")} for r in profitable_rejected),
                key=lambda x: x["pnl_r"], reverse=True)[:10],
        },
        "by_setup_and_regime": by_setup_regime,
        "exit_reasons": _group(closed, lambda r: r["exit_reason"]),
        "trailing_vs_fixed": {
            "fixed": stats(closed),
            "trailing": stats({"pnl_r": (r.get("trail") or {}).get("pnl_r")} for r in closed
                              if (r.get("trail") or {}).get("status") == "closed"),
        },
    }
