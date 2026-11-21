"""System-wide pause switch.

Paused means: no new scans, no new signals and no AI calls. Open paper trades
are still tracked (pure Python, no AI cost) and the dashboard stays up. The
server-level kill switch (``sudo ita-kill``) is separate and stops everything.
"""

from __future__ import annotations

import json
from datetime import datetime

from backend.db import get_setting, set_setting
from tradingagents.utils.market_calendar import IST

PAUSE_KEY = "system_pause"


def get_pause_state() -> dict:
    raw = get_setting(PAUSE_KEY)
    try:
        state = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        state = {}
    return {
        "paused": bool(state.get("paused")),
        "since": state.get("since"),
        "reason": state.get("reason"),
    }


def is_paused() -> bool:
    return get_pause_state()["paused"]


def set_paused(paused: bool, reason: str | None = None) -> dict:
    if paused:
        set_setting(PAUSE_KEY, json.dumps({
            "paused": True,
            "since": datetime.now(IST).isoformat(timespec="seconds"),
            "reason": (reason or "manual")[:200],
        }))
    else:
        set_setting(PAUSE_KEY, None)
    return get_pause_state()
