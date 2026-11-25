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
* ``scan:orb`` — after each synced 10-minute NSE bar from 09:55 to 14:50
  (opening range breakout on the most liquid stock futures).
* ``scan:nse_1h`` / ``scan:mcx_1h`` — after each completed 1h bar has been
  synced (futures and MCX hourly scans; open journal records are tracked too).
  After the NSE scan, open option spreads are checked on that 1H close.
* ``candles:NSE:15minute`` then ``scan:spreads`` — option spreads in intraday mode
  (24 Nov 2026): index 15-minute bars, then the multi-timeframe entry scan, for bars
  ending inside the spread entry window.
* ``postmortem:weekly`` — Saturday from 08:00 (Sunday catches up), once a week:
  the weekly post-mortem over the journal. Runs on weekends and while paused
  (it only reads the journal; its AI review is skipped while paused).

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
BAR_LENGTH = {"5minute": timedelta(minutes=5), "10minute": timedelta(minutes=10), "15minute": timedelta(minutes=15),
              "60minute": timedelta(hours=1)}
# (exchange, interval) pairs the scheduler keeps fresh.
# ORB: first possible signal bar ends 09:55 (after the 30-minute range); no entry in the last 30 minutes.
ORB_FIRST_SIGNAL_END = time(9, 55)
ORB_LAST_SIGNAL_END = time(14, 50)
SCHEDULE = ((NSE, "10minute"), (NSE, "60minute"), (NSE, "day"), (MCX, "60minute"), (MCX, "day"))


def _nifty_options_on() -> bool:
    try:
        from backend.signals.nifty_options import settings
        return bool(settings()["enabled"])
    except Exception:
        return False


def _spreads_intraday() -> dict | None:
    """Entry window of the intraday option-spread scan, or None when it's off."""
    try:
        from backend.options.spreads import intraday_on, settings
        if not intraday_on():
            return None
        ic = settings()["intraday"]
        return {"first": time.fromisoformat(ic["first_signal"]), "last": time.fromisoformat(ic["last_signal"])}
    except Exception:
        return None


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
    bar10 = latest_closed_bar(NSE, "10minute", now)
    synced10 = done.get("candles:NSE:10minute")
    if (bar10 and synced10 and synced10 >= bar10 and "candles:NSE:10minute" not in jobs
            and ORB_FIRST_SIGNAL_END <= bar10.time() <= ORB_LAST_SIGNAL_END
            and state.get("scans_done", {}).get("orb") != bar10):
        jobs.append("scan:orb")
    if state.get("nifty_options"):   # NIFTY option buying: 5-minute bars, then the scan
        bar5 = latest_closed_bar(NSE, "5minute", now)
        if bar5 and (done.get("candles:NSE:5minute") is None or done["candles:NSE:5minute"] < bar5):
            jobs.append("candles:NSE:5minute")
        elif bar5 and state.get("scans_done", {}).get("nifty_opt") != bar5:
            jobs.append("scan:nifty_opt")
    spreads_win = state.get("spreads_intraday")
    if spreads_win:   # option spreads: index 15-minute bars, then the multi-timeframe scan
        bar15 = latest_closed_bar(NSE, "15minute", now)
        if bar15 and (done.get("candles:NSE:15minute") is None or done["candles:NSE:15minute"] < bar15):
            jobs.append("candles:NSE:15minute")
        elif (bar15 and spreads_win["first"] <= bar15.time() <= spreads_win["last"]
              and state.get("scans_done", {}).get("spreads") != bar15
              and "candles:NSE:60minute" not in jobs):
            jobs.append("scan:spreads")
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
        self.options_attempt: datetime | None = None
        self.started_at: datetime | None = None
        self.scan_ok_at: dict[str, datetime] = {}      # exchange -> last hourly scan that completed
        self.stall_alerted: dict[str, datetime] = {}   # exchange -> stall already warned about
        self.stall: dict[str, str] = {}                # exchange -> current stall message

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
            "nifty_options": _nifty_options_on(),
            "spreads_intraday": _spreads_intraday(),
        }

    def tick(self, now: datetime | None = None) -> list[str]:
        from backend.marketdata import jobs

        now = now or datetime.now(IST).replace(tzinfo=None)
        self.last_tick = now
        if self.started_at is None:
            self.started_at = now
        if self.scans_done.get("cash_swing") != now.date():
            from backend.signals.cash_swing import latest_run
            run = latest_run(now.date().isoformat())
            if run and run["status"] in ("ok", "blocked"):
                self.scans_done["cash_swing"] = now.date()  # already ran today (e.g. before a restart)
        ran = []
        week = self._postmortem_week(now)
        if week:
            from backend.journal.postmortem import run_weekly_postmortem
            try:
                result = run_weekly_postmortem(week)
            except Exception as exc:   # never let it stop the scheduler
                result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            self.scans_done["postmortem"] = week
            self.last_results["postmortem:weekly"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
            ran.append("postmortem:weekly")
        ran += self._option_data(now)
        ran += self._weekly_backup(now)
        state = self._state(now)
        self.waiting_for = self._waiting_reason(now, state)
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
                if key == "nse_1h":
                    self._momentum("hourly", now)
                    self._spreads_follow(now)
                if result.get("status") not in ("error", "busy") and result.get("reason") != "paused":
                    self.scan_ok_at[exchange] = now
            elif job == "scan:spreads":
                from backend.options.spreads import scan_intraday
                try:
                    result = scan_intraday(now=now)
                except Exception as exc:  # never let it stop the scheduler
                    result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                self.scans_done["spreads"] = latest_closed_bar(NSE, "15minute", now)
            elif job == "scan:orb":
                from backend.signals.orb import run_orb_scan
                result = run_orb_scan(now=now)
                self.scans_done["orb"] = latest_closed_bar(NSE, "10minute", now)
            elif job == "scan:nifty_opt":
                from backend.signals.nifty_options import run_scan as run_opt_scan
                try:
                    result = run_opt_scan(now=now)
                except Exception as exc:  # never let it stop the scheduler
                    result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                self.scans_done["nifty_opt"] = latest_closed_bar(NSE, "5minute", now)
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
                    try:
                        from backend.signals.wolfe import run_scan as run_wolfe_scan
                        wolfe = run_wolfe_scan(now=now)
                    except Exception as exc:  # never let it stop the scheduler
                        wolfe = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                    self.last_results["scan:wolfe"] = {"at": now.isoformat(timespec="seconds"), **_brief(wolfe)}
                    self._divergence("nse", now)
                    self._momentum("daily", now)
            else:
                _, exchange, interval = job.split(":")
                bar = latest_closed_bar(exchange, interval, now)
                result = jobs.candle_sync(intervals=(interval,), exchanges=(exchange,), now=now)
                if result.get("status") != "busy":
                    self.done[job] = bar
                    if exchange == MCX and interval == "day":
                        self._divergence("mcx", now)   # MCX daily bars are complete after the evening close
                    if interval in ("10minute", "60minute"):
                        self._my_trades(exchange, interval, now)
            self.last_results[job] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
            ran.append(job)
            if result.get("status") == "busy":
                break  # try the rest on the next tick
        self._watchdog(now, state)
        return ran

    def _spreads_follow(self, now: datetime) -> None:
        """Open option spreads on each NSE 1H close: target / stop / invalidation / exit day."""
        try:
            from backend.options.spreads import follow_intraday
            result = follow_intraday(now=now)
        except Exception as exc:  # never let it stop the scheduler
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        self.last_results["options:spreads_1h"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}

    def _my_trades(self, exchange: str, interval: str, now: datetime) -> None:
        """Trailing stops of your own futures trades after each synced bar."""
        try:
            from backend.my_trades import update_trades
            result = update_trades(exchange=exchange, interval=interval)
        except Exception as exc:  # never let it stop the scheduler
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        self.last_results[f"my_trades:{exchange}"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
    def _watchdog(self, now: datetime, state: dict) -> None:
        """Telegram warning when the hourly scans go quiet during market hours (one per stall)."""
        from backend.watchdog import check_and_warn, stall_reason
        for exchange in (NSE, MCX):
            try:
                found = stall_reason(now, exchange, self.scan_ok_at.get(exchange), self.started_at,
                                     state["paused"], state["kite_connected"])
                if found is None:
                    self.stall.pop(exchange, None)
                    continue
                self.stall[exchange] = found[0]
                status = check_and_warn(now, exchange, self.scan_ok_at.get(exchange), self.started_at,
                                        state["paused"], state["kite_connected"], self.stall_alerted)
                if status:
                    self.last_results[f"watchdog:{exchange}"] = {"at": now.isoformat(timespec="seconds"),
                                                                 "status": status, "reason": found[0][:200]}
            except Exception:   # never let it stop the scheduler
                log.exception("scan watchdog failed")

    def _divergence(self, group: str, now: datetime) -> None:
        """Information-only daily alerts on the day's completed bars: RSI divergence and
        chart patterns; after the NSE close also the ideas tracker."""
        from backend.signals.chart_patterns import run_scan as run_patterns
        from backend.signals.divergence import run_scan as run_divergence
        jobs_ = [(f"divergence:{group}", lambda: run_divergence(group, now=now)),
                 (f"patterns:{group}", lambda: run_patterns(group, now=now))]
        if group == "nse":
            from backend.ideas import refresh_ideas
            jobs_.append(("ideas:refresh", refresh_ideas))
        for name, job in jobs_:
            try:
                result = job()
            except Exception as exc:  # never let it stop the scheduler
                result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            self.last_results[name] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}

    def _momentum(self, kind: str, now: datetime) -> None:
        """Momentum book: entries/stops after each NSE hourly bar; ranking and
        end-of-day exits after the close."""
        from backend.momentum.book import daily_run, hourly_run
        try:
            result = (hourly_run if kind == "hourly" else daily_run)(now=now)
        except Exception as exc:  # never let it stop the scheduler
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        self.last_results[f"momentum:{kind}"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}

    def _weekly_backup(self, now: datetime) -> list[str]:
        """Weekly backups of the app's data (runs while paused and without Kite)."""
        from backend import backup
        if not backup.enabled():
            return []
        last = self.scans_done.get("backup")
        if isinstance(last, datetime) and now - last < timedelta(hours=6):
            return []                     # failed or busy recently: retry later
        try:
            if not backup.backup_due(now, backup.last_backup_at("essentials")):
                return []
            self.scans_done["backup"] = now
            result = backup.run_weekly_backup(now)
        except Exception as exc:   # never let it stop the scheduler
            self.scans_done["backup"] = now
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        self.last_results["backup:weekly"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
        return ["backup:weekly"]

    def _option_data(self, now: datetime) -> list[str]:
        """Evening NSE/BSE option file download (data only: no Kite login needed,
        runs while paused)."""
        from backend.options.jobs import daily_update, eod_due
        if not eod_due(now, self.scans_done.get("options_eod"), self.options_attempt):
            return []
        self.options_attempt = now
        try:
            result = daily_update(now=now)
        except Exception as exc:   # never let it stop the scheduler
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        ran = ["options:eod"]
        if result.get("complete"):
            self.scans_done["options_eod"] = now.date()
            try:
                from backend.options.spreads import run_eod
                spread = run_eod(now.date())
            except Exception as exc:   # never let it stop the scheduler
                spread = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            self.last_results["options:spreads"] = {"at": now.isoformat(timespec="seconds"), **_brief(spread)}
            ran.append("options:spreads")
        self.last_results["options:eod"] = {"at": now.isoformat(timespec="seconds"), **_brief(result)}
        return ran

    def _postmortem_week(self, now: datetime) -> date | None:
        from backend.journal.postmortem import exists, postmortem_due
        week = postmortem_due(now, self.scans_done.get("postmortem"))
        if week and exists(week):
            self.scans_done["postmortem"] = week   # already done (e.g. before a restart, or by hand)
            return None
        return week

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
            "last_scan_ok": {k: v.isoformat(timespec="minutes") for k, v in self.scan_ok_at.items()},
            "stalled": dict(self.stall),
            "last_results": self.last_results,
        }


def _brief(result: dict) -> dict:
    keep = ("status", "reason", "series_synced", "candles_saved", "valid_setups", "above_60", "shortlisted",
            "closed", "ai_status", "counts", "complete", "telegram", "new", "alert", "checked", "by_pattern", "moved", "hit")
    out = {k: result[k] for k in keep if k in result}
    if result.get("errors"):
        out["errors"] = result["errors"][:5]
    return out


scheduler = MarketDataScheduler()
