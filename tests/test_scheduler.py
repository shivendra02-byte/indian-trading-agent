"""Scheduler timing decisions (pure) and one tick against a fake Kite client."""

import unittest
from datetime import date, datetime

from backend.scheduler import bar_ends, due_jobs, latest_closed_bar
from tests._support import IsolatedStateTestCase


def state(**overrides):
    base = {"paused": False, "kite_connected": True, "instruments_fresh": True,
            "last_morning_attempt": None, "done": {}}
    base.update(overrides)
    return base


class BarScheduleTests(unittest.TestCase):
    def test_nse_hourly_bar_ends_include_short_last_bar(self):
        ends = [e.strftime("%H:%M") for e in bar_ends("NSE", "60minute", date(2026, 11, 23))]
        self.assertEqual(ends, ["10:15", "11:15", "12:15", "13:15", "14:15", "15:15", "15:30"])

    def test_mcx_evening_only_holiday(self):
        ends = [e.strftime("%H:%M") for e in bar_ends("MCX", "60minute", date(2026, 11, 24))]
        self.assertEqual(ends[0], "18:00")
        self.assertEqual(ends[-1], "23:55")
        self.assertEqual(list(bar_ends("NSE", "60minute", date(2026, 11, 24))), [])

    def test_latest_closed_bar_waits_for_buffer(self):
        self.assertEqual(latest_closed_bar("NSE", "60minute", datetime(2026, 11, 23, 10, 16)), None)
        self.assertEqual(latest_closed_bar("NSE", "60minute", datetime(2026, 11, 23, 10, 17)),
                         datetime(2026, 11, 23, 10, 15))
        self.assertEqual(latest_closed_bar("NSE", "day", datetime(2026, 11, 23, 15, 41)),
                         datetime(2026, 11, 23, 15, 30))


class DueJobsTests(unittest.TestCase):
    def test_nothing_on_weekend_when_paused_or_before_kite_login(self):
        self.assertEqual(due_jobs(datetime(2026, 11, 21, 11, 0), state()), [])
        self.assertEqual(due_jobs(datetime(2026, 11, 23, 11, 0), state(paused=True)), [])
        self.assertEqual(due_jobs(datetime(2026, 11, 23, 11, 0), state(kite_connected=False)), [])

    def test_morning_refresh_first_and_retry_spacing(self):
        now = datetime(2026, 11, 23, 9, 0)
        self.assertEqual(due_jobs(now, state(instruments_fresh=False)), ["morning_refresh"])
        recent = state(instruments_fresh=False, last_morning_attempt=datetime(2026, 11, 23, 8, 50))
        self.assertEqual(due_jobs(now, recent), [])
        self.assertEqual(due_jobs(datetime(2026, 11, 23, 8, 0), state(instruments_fresh=False)), [])

    def test_candle_jobs_fire_once_per_closed_bar(self):
        now = datetime(2026, 11, 23, 11, 17)
        jobs = due_jobs(now, state())
        self.assertIn("candles:NSE:60minute", jobs)
        self.assertIn("candles:NSE:10minute", jobs)
        self.assertIn("candles:MCX:60minute", jobs)
        self.assertNotIn("candles:NSE:day", jobs)
        done = {"candles:NSE:60minute": datetime(2026, 11, 23, 11, 15),
                "candles:NSE:10minute": datetime(2026, 11, 23, 11, 15),
                "candles:MCX:60minute": datetime(2026, 11, 23, 11, 0)}
        self.assertEqual(due_jobs(now, state(done=done)), ["scan:nse_1h", "scan:mcx_1h"])
        scans = {"nse_1h": datetime(2026, 11, 23, 11, 15), "mcx_1h": datetime(2026, 11, 23, 11, 0)}
        self.assertEqual(due_jobs(now, state(done=done, scans_done=scans)), [])

    def test_mcx_only_day_runs_mcx_jobs(self):
        jobs = due_jobs(datetime(2026, 11, 24, 18, 3), state())
        self.assertEqual(jobs, ["candles:MCX:60minute"])


class SchedulerTickTests(IsolatedStateTestCase, unittest.TestCase):
    def test_tick_runs_morning_refresh_then_candles(self):
        from unittest.mock import patch
        from backend.scheduler import MarketDataScheduler
        from tests.test_marketdata import FakeKite

        fake = FakeKite()
        sched = MarketDataScheduler()
        with patch("backend.marketdata.jobs.get_authenticated_client", return_value=fake), \
             patch("backend.brokers.kite.get_kite_status", return_value={"connected_today": True}), \
             patch("backend.marketdata.instruments._today", return_value=date(2026, 11, 20)), \
             patch("backend.marketdata.candles.REQUEST_GAP_SECONDS", 0), \
             patch("backend.marketdata.candles._time.sleep", lambda s: None), \
             patch("backend.signals.events._yahoo_results_date", return_value=None):
            self.assertEqual(sched.tick(datetime(2026, 11, 20, 9, 0)), ["morning_refresh"])
            ran = sched.tick(datetime(2026, 11, 20, 12, 20))
        self.assertIn("candles:NSE:60minute", ran)
        self.assertEqual(sched.last_results["candles:NSE:60minute"]["status"], "ok")
        self.assertEqual(sched.done["candles:NSE:60minute"], datetime(2026, 11, 20, 12, 15))
        self.assertIsNone(sched.status()["waiting_for"])


if __name__ == "__main__":
    unittest.main()
