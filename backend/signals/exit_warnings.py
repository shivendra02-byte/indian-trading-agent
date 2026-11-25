"""Minervini top signals as exit warnings for the longs you hold (agreed 25 Nov 2026).

After the NSE close (with the daily cash scans) every symbol you are long in — shortlisted /
approved journal longs not closed, open long ideas, local positions — is checked on its daily
bars with ``stage.top_warnings()`` (climax run, churning, heavy-volume break of the 50-day SMA,
200-day SMA rolling over, distribution days). Each warning is stored once per symbol, kind and
day in ``top_warnings``; Telegram only when ``global.stage_analysis.top_warnings.alert`` is on
(off until the warnings are studied). Information only: never closes or changes a trade.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from backend.db import get_db
from backend.decision.config import get_config
from backend.signals import stage as stage_mod

log = logging.getLogger(__name__)


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS top_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, signal_date TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT,
                daily_stage TEXT, close REAL, alert_status TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE (symbol, kind, signal_date)
            )""")


def list_warnings(limit: int = 100) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM top_warnings ORDER BY signal_date DESC, id DESC LIMIT ?", (limit,)).fetchall()]


def build_alert(new: list[dict]) -> str:
    lines = ["<b>Exit warnings (Minervini top signals, daily)</b>",
             "<i>Information only, for longs you hold. Not reviewed by AI; your stops are unchanged.</i>", ""]
    for w in new:
        lines.append(f"• <b>{html.escape(w['symbol'])}</b> {html.escape(w['kind'].replace('_', ' '))}: "
                     f"{html.escape(w['detail'])}")
    return "\n".join(lines)


def run(now: datetime | None = None, notify=None) -> dict:
    """Check every held long; store new warnings and alert them if configured."""
    from backend.marketdata.candles import get_candles
    from backend.signals.divergence import instruments, open_long_symbols
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import IST

    cfg = get_config().global_.stage_analysis.top_warnings
    if not cfg.enabled:
        return {"status": "skipped", "reason": "top warnings disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    ensure_table()
    now = now or datetime.now(IST).replace(tzinfo=None)
    held = open_long_symbols()
    tokens = {i["symbol"]: i["token"] for i in instruments("nse", now.date()) if i["symbol"] in held}
    new = []
    for symbol, token in sorted(tokens.items()):
        bars = get_candles(token, "day", limit=400)
        if not bars:
            continue
        day = str(bars[-1]["ts"])[:10]
        info = stage_mod.classify(bars)
        for w in stage_mod.top_warnings(bars):
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO top_warnings (symbol, signal_date, kind, detail, daily_stage, close) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (symbol, day, w["kind"], w["detail"], info.get("label"),
                                                  bars[-1]["close"]))
                if cur.rowcount:
                    new.append({**w, "symbol": symbol, "id": cur.lastrowid})
    alert = "none"
    if new:
        if cfg.alert:
            alert = (notify or _send)(new)
        else:
            alert = "not sent: alert off (shadow)"
        with get_db() as conn:
            conn.executemany("UPDATE top_warnings SET alert_status = ? WHERE id = ?", [(alert, w["id"]) for w in new])
    return {"status": "ok", "held_checked": len(tokens), "new": len(new), "alert": alert}


def _send(new: list[dict]) -> str:
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(build_alert(new))
        return "sent"
    except TelegramConfigError as exc:
        return f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("top warning alert failed: %s", exc)
        return f"failed: {exc}"
