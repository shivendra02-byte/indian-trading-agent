"""Every research strategy, with how to run it live (evening, shadow) and in a backtest.

``live_after``: which evening run updates it — "nse" after the NSE daily scans (15:45+),
"mcx" after the MCX daily bars are complete (after the evening close), "both" = each time.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from backend.research import config as rcfg

LIMITS_FUTURES = [
    "MCX: Kite's continuous daily series is the front contract joined at each expiry, not back-adjusted — "
    "rolls can show as small price jumps.",
    "NIFTY/BANKNIFTY: traded on the index's own daily bars (spot); futures basis not modelled, a monthly "
    "roll cost is charged instead.",
    "Fills at the next day's open; no slippage beyond the cost model; lot sizes are today's.",
]


def _trend(key: str) -> dict:
    from backend.research import trend
    return {
        "key": key, "page": "trend", "kind": "trades", "live_after": "both",
        "live": lambda today=None: trend.live(key, today),
        "backtest": lambda params, progress=None: trend.backtest(key, params, progress),
        "status": lambda: trend.status(key),
        "limitations": LIMITS_FUTURES,
    }


def _entries() -> dict[str, dict]:
    out = {"trend_ewma": _trend("trend_ewma"), "tsmom": _trend("tsmom")}
    try:
        from backend.research import pairs
        out["pairs_stocks"] = pairs.entry("pairs_stocks")
        out["pairs_metals"] = pairs.entry("pairs_metals")
    except ImportError:
        pass
    try:
        from backend.research import cash_momentum
        out["cash_momentum"] = cash_momentum.entry()
    except ImportError:
        pass
    try:
        from backend.research import risk_switch
        out["risk_switch"] = risk_switch.entry()
    except ImportError:
        pass
    return out


def all_entries() -> dict[str, dict]:
    entries = _entries()
    strategies = rcfg.load().get("strategies") or {}
    for key, e in entries.items():
        s = strategies.get(key) or {}
        e["label"] = s.get("label", key)
        e["enabled"] = bool(s.get("enabled", False))
    return entries


def get(key: str) -> dict:
    entries = all_entries()
    if key not in entries:
        raise KeyError(f"unknown research strategy {key}")
    return entries[key]


def run_live(group: str, today: date | None = None, runner: Callable | None = None) -> dict:
    """Evening run of every enabled strategy for ``group`` ("nse" / "mcx"). Never raises."""
    from backend.research.journal import log_run
    from backend.system_state import is_paused
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    results = {}
    for key, e in all_entries().items():
        if not e["enabled"] or e["live_after"] not in (group, "both"):
            continue
        try:
            res = (runner or (lambda entry: entry["live"](today)))(e)
            res = {k: v for k, v in res.items() if k != "states"}
            log_run(key, res.get("status", "ok"), res)
        except Exception as exc:   # one strategy failing never stops the others
            res = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            log_run(key, "error", res)
        results[key] = res
    return {"status": "ok", "strategies": results}
