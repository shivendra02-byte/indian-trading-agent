"""Market data scheduler: runs data jobs at candle closes on trading days.

A single background thread wakes every ``TICK_SECONDS`` and asks
``due_jobs()`` what should run now. Decisions are pure functions of the clock,
the market calendar and what has already run, so a missed tick (restart, slow
job, busy lock) simply catches up on the next one.

Jobs:
* ``morning_refresh`` — instrument master + ban list, once per trading day,
  from 08:30 or as soon as Kite is logged in after that.
* ``candles:<EXCHANGE>:<interval>`` — after each candle closes (+ a short
  buffer so Kite has the bar): NSE 10-minute (indices) and 1-hour; MCX 1-hour;
  daily bars after each exchange's close.
* ``scan:cash_swing`` — once per NSE trading day from 15:45 (followed by the
  cup-and-handle scan on the same daily bars), after today's
  daily candles are stored.
* ``scan:nse_1h`` / ``scan:mcx_1h`` — after each completed 1h bar has been
  synced (futures and MCX hourly scans; open journal records are tracked too).

Nothing runs while the system is paused, on weekends/holidays, or before
today's Kite login. Disable with ``TRADINGAGENTS_SCHEDULER=off``.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, time, timedelta
from typing import Iterator

from tradingagents.utils.market_calendar import IST, MCX, NSE, session_windows

log = logging.getLogger(__name__)

TICK_SECONDS = 30
CANDLE_BUFFER = timedelta(minutes=2)       # Kite publishes a bar shortly after it closes
DAILY_BUFFER = timedelta(minutes=10)
MORNING_REFRESH_AT = time(8, 30)
MORNING_RETRY = timedelta(minutes=15)
CASH_SWING_SCAN_AT = time(15, 45)
BAR_LENGTH = {"10minute": timedelta(minutes=10), "60minute": timedelta(hours=1)}
# (exchange, interval) pairs the scheduler keeps fresh.
SCHEDULE = ((NSE, "10minute"), (NSE, "60minute"), (NSE, "day"), (MCX, "60minute"), (MCX, "day"))


def scheduler_enabled() -> bool:
    return os.getenv("TRADINGAGENTS_SCHEDULER", "on").strip().lower() not in {"off", "0", "false", "no"}


def bar_ends(exchange: str, interval: str, d: date) -> Iterator[datetime]:
    """Close times of every bar on ``d``; the last bar of a session is cut at the close."""
    if interval == "day":
        windows = session_windows(exchange, d)
        if windows:
            yield datetime.combine(d, windows[-1][1])
        return
    step = BAR_LENGTH[interval]
    for open_t, close_t in session_windows(exchange, d):
        t = datetime.combine(d, open_t) + step
        close = datetime.combine(d, close_t)
        while t < close:
            yield t
            t += step
        yield close


def latest_closed_bar(exchange: str, interval: str, now: datetime) -> datetime | None:
    buffer = DAILY_BUFFER if interval == "day" else CANDLE_BUFFER
    ends = [e for e in bar_ends(exchange, interval, now.date()) if e + buffer <= now]
    return ends[-1] if ends else None


def due_jobs(now: datetime, state: dict) -> list[str]:
    """Jobs to run at ``now`` (naive IST). ``state`` keys:

    instruments_fresh (bool), kite_connected (bool), paused (bool),
    last_morning_attempt (datetime|None), done (dict job -> bar end handled).
    """
    if state.get("paused"):
        return []
    today = now.date()
    trading = [ex for ex in (NSE, MCX) if session_windows(ex, today)]
    if not trading or not state.get("kite_connected"):
        return []
    jobs = []
    last_try = state.get("last_morning_attempt")
    if (not state.get("instruments_fresh") and now.time() >= MORNING_REFRESH_AT
            and (last_try is None or now - last_try >= MORNING_RETRY)):
        jobs.append("morning_refresh")
        return jobs  # candles wait for today's instrument master (active contracts)
    if not state.get("instruments_fresh"):
        return jobs
    done = state.get("done", {})
    for exchange, interval in SCHEDULE:
        bar = latest_closed_bar(exchange, interval, now)
        key = f"candles:{exchange}:{interval}"
        if bar and (done.get(key) is None or done[key] < bar):
            jobs.append(key)
    for exchange, key in ((NSE, "nse_1h"), (MCX, "mcx_1h")):
        bar = latest_closed_bar(exchange, "60minute", now)
        synced = done.get(f"candles:{exchange}:60minute")
        if (bar and synced and synced >= bar and f"candles:{exchange}:60minute" not in jobs
                and state.get("scans_done", {}).get(key) != bar):
            jobs.append(f"scan:{key}")
    nse_close = latest_closed_bar(NSE, "day", now)
    day_synced = done.get("candles:NSE:day")
    if (nse_close and now.time() >= CASH_SWING_SCAN_AT and day_synced and day_synced >= nse_close
            and "candles:NSE:day" not in jobs and state.get("scans_done", {}).get("cash_swing") != today):
        jobs.append("scan:cash_swing")
    return jobs


class MarketDataScheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.done: dict[str, datetime] = {}
        self.last_morning_attempt: datetime | None = None
        self.last_tick: datetime | None = None
        self.last_results: dict[str, dict] = {}
        self.waiting_for: str | None = None
        self.scans_done: dict[str, date | datetime] = {}

    # --- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="marketdata-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad tick kill the scheduler
                log.exception("scheduler tick failed")
            self._stop.wait(TICK_SECONDS)

    # --- one tick --------------------------------------------------------------
    def _state(self, now: datetime) -> dict:
        from backend.brokers.kite import get_kite_status
        from backend.marketdata.instruments import instrument_summary
        from backend.system_state import is_paused

        return {
            "paused": is_paused(),
            "kite_connected": get_kite_status()["connected_today"],
            "instruments_fresh": instrument_summary()["fresh_today"],
            "last_morning_attempt": self.last_morning_attempt,
            "done": self.done,
            "scans_done": self.scans_done,
        }

    def tick(self, now: datetime | None = None) -> list[str]:
        from backend.marketdata import jobs

        now = now or datetime.now(IST).replace(tzinfo=None)
        self.last_tick = now
        if self.scans_done.get("cash_swing") != now.date():
            from backend.signals.cash_swing import latest_run
            run = latest_run(now.date().isoformat())
            if run and run["status"] in ("ok", "blocked"):
                self.scans_done["cash_swing"] = now.date()  # already ran today (e.g. before a restart)
        state = self._state(now)
        self.waiting_for = self._waiting_reason(now, state)
        ran = []
        for job in due_jobs(now, state):
            if job == "morning_refresh":
                self.last_morning_attempt = now
                result = jobs.morning_refresh()
            elif job in ("scan:nse_1h", "scan:mcx_1h"):
                from backend.signals.fno import run_hourly_scan
                key = job.split(":")[1]
                exchange = NSE if key == "nse_1h" else MCX
                result = run_hourly_scan("nse" if key == "nse_1h" else "mcx", now=now)
                self.scans_done[key] = latest_closed_bar(exchange, "60minute", now)
            elif job == "scan:cash_swing":
                from backend.signals.cash_swing import run_scan
                result = run_scan(now=now)
                if result.get("status") != "skipped" or result.get("reason") != "paused":
                    self.scans_done["cash_swing"] = now.date()
                    # Cup and handle runs on the same daily bars, even when the swing scan
                    # is blocked by an event inside its 7-day window.
                    try:
                        from backend.signals.cup_handle import run_scan as run_cup_scan
                        cup = run_cup_scan(now=now)
                    except Exception as exc:  # never let it stop the scheduler
                        cup = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                    self.last_results["scan:cup_handle"] = {"at": now.isoformat(timespec="seconds"), **_brief(cup)}
            else:
                _, exchange, interval = job.split(":")
                bar = latest_closed_bar(exchange, interval, now)
                result = jobs.candle_sync(intervals=(interval,), exchanges=(exchange,), now=now)
                if result.get("status") != "busy":
                    self.done[job] = bar
            self.last_results[job] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
            ran.append(job)
            if result.get("status") == "busy":
                break  # try the rest on the next tick
        return ran

    @staticmethod
    def _waiting_reason(now: datetime, state: dict) -> str | None:
        if state["paused"]:
            return "paused"
        if not any(session_windows(ex, now.date()) for ex in (NSE, MCX)):
            return "market holiday or weekend"
        if not state["kite_connected"]:
            return "Kite login for today"
        if not state["instruments_fresh"] and now.time() < MORNING_REFRESH_AT:
            return "morning refresh at 08:30"
        return None

    def status(self) -> dict:
        return {
            "enabled": scheduler_enabled(),
            "running": self.running,
            "last_tick": self.last_tick.isoformat(timespec="seconds") if self.last_tick else None,
            "waiting_for": self.waiting_for,
            "last_bar_synced": {k: v.isoformat(timespec="minutes") for k, v in self.done.items() if v},
            "last_results": self.last_results,
        }


def _brief(result: dict) -> dict:
    keep = ("status", "reason", "series_synced", "candles_saved", "valid_setups", "above_60", "shortlisted")
    out = {k: result[k] for k in keep if k in result}
    if result.get("errors"):
        out["errors"] = result["errors"][:5]
    return out


scheduler = MarketDataScheduler()
