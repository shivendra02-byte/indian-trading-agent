"""Telegram warning when the hourly scans go quiet during market hours (asked 23 Nov 2026).

A stalled scheduler, a missing Kite login or a failing data sync otherwise just means
"no alerts", which looks the same as "no trades". ``stall_reason()`` is pure (unit-tested);
the scheduler calls ``check_and_warn()`` every tick and sends one message per stall.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from tradingagents.utils.market_calendar import session_windows

log = logging.getLogger(__name__)

STALL_AFTER = timedelta(minutes=75)     # an hourly scan should complete every ~60 min


def _window_now(exchange: str, now: datetime) -> tuple[datetime, datetime] | None:
    for open_t, close_t in session_windows(exchange, now.date()):
        start, end = datetime.combine(now.date(), open_t), datetime.combine(now.date(), close_t)
        if start <= now <= end:
            return start, end
    return None


def stall_reason(now: datetime, exchange: str, last_ok: datetime | None, started: datetime | None,
                 paused: bool, kite_connected: bool) -> tuple[str, datetime] | None:
    """``(message, since)`` when ``exchange`` is open, at least ``STALL_AFTER`` into the
    session, and no hourly scan has completed for ``STALL_AFTER``; else None. Paused is
    deliberate, so it never warns. ``since`` identifies the stall (one message each)."""
    if paused:
        return None
    window = _window_now(exchange, now)
    if not window or now - window[0] < STALL_AFTER:
        return None
    if not kite_connected:
        return (f"{exchange} is open but Kite is not logged in today: no data, no scans and no trade "
                f"alerts until you log in to Kite on the dashboard.", window[0])
    since = max(t for t in (last_ok, started, window[0]) if t is not None)
    if now - since < STALL_AFTER:
        return None
    return (f"No {exchange} hourly scan has completed since {since:%H:%M} "
            f"({int((now - since).total_seconds() // 60)} min). Trade alerts are not being produced; "
            f"check the sidebar status / Scheduler status.", since)


def check_and_warn(now: datetime, exchange: str, last_ok: datetime | None, started: datetime | None,
                   paused: bool, kite_connected: bool, alerted: dict) -> str | None:
    """Send the warning once per stall. ``alerted`` (exchange -> since) is kept by the caller."""
    found = stall_reason(now, exchange, last_ok, started, paused, kite_connected)
    if found is None:
        return None
    text, since = found
    if alerted.get(exchange) == since:
        return None
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(f"<b>⚠️ Trading system: scans stalled</b>\n{text}")
        status = "sent"
    except TelegramConfigError as exc:
        status = f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("stall warning failed: %s", exc)
        return f"failed: {exc}"          # not marked: retried on the next tick
    alerted[exchange] = since
    return status
