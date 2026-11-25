"""24 Nov 2026: every MCX contract was rejected with "only N daily bars" — a continuous daily
series that came back short is downloaded again over the whole lookback."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests._support import IsolatedStateTestCase

NOW = datetime(2026, 11, 24, 21, 5)
TOKEN = 99001


class DailyKite:
    """Continuous daily history: returns a bar for every weekday in the requested range."""

    def __init__(self):
        self.calls = []

    def historical_data(self, token, start, stop, interval, continuous=False, oi=False):
        self.calls.append((start, stop, continuous))
        rows, d = [], start.replace(hour=0, minute=0, second=0, microsecond=0)
        while d < stop - timedelta(days=1):
            if d.weekday() < 5:
                rows.append({"date": d, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10})
            d += timedelta(days=1)
        return rows


PLAN = [{"label": "GOLDM FUT", "token": TOKEN, "interval": "day", "exchange": "MCX", "lookback_days": 400,
         "continuous": True, "oi": False}]


class RepairTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.marketdata import jobs
        from backend.marketdata.store import ensure_marketdata_tables
        ensure_marketdata_tables()
        jobs._REPAIRED.clear()
        self.addCleanup(jobs._REPAIRED.clear)

    def _seed_short(self):
        from backend.marketdata.candles import save_candles
        save_candles(TOKEN, "day", [{"date": NOW - timedelta(days=i), "open": 1, "high": 1, "low": 1, "close": 1}
                                    for i in range(1, 6)])

    def test_short_series_downloaded_again_in_full(self):
        from backend.marketdata import jobs
        from backend.marketdata.candles import count_stored
        self._seed_short()
        kite = DailyKite()
        with mock.patch.object(jobs, "build_sync_plan", return_value=PLAN), \
                mock.patch("backend.marketdata.candles._time.sleep"):
            out = jobs.repair_continuous(kite, now=NOW)
        self.assertTrue(out[0]["repaired"])
        self.assertEqual(out[0]["before"], 5)
        self.assertGreaterEqual(count_stored(TOKEN, "day"), 250)
        self.assertTrue(kite.calls[0][2])                                        # continuous
        self.assertLess(kite.calls[0][0], NOW - timedelta(days=390))              # from the full lookback

        with mock.patch.object(jobs, "build_sync_plan", return_value=PLAN):
            again = jobs.repair_continuous(kite, now=NOW)
        self.assertNotIn("repaired", again[0])                                    # long enough now

    def test_tried_once_a_day(self):
        from backend.marketdata import jobs

        class Empty(DailyKite):
            def historical_data(self, *a, **k):
                self.calls.append(a)
                return []
        self._seed_short()
        kite = Empty()
        with mock.patch.object(jobs, "build_sync_plan", return_value=PLAN), \
                mock.patch("backend.marketdata.candles._time.sleep"):
            jobs.repair_continuous(kite, now=NOW)
            n = len(kite.calls)
            jobs.repair_continuous(kite, now=NOW)
            self.assertEqual(len(kite.calls), n)                                  # not again today
            jobs.repair_continuous(kite, now=NOW + timedelta(days=1))
            self.assertGreater(len(kite.calls), n)                                # tomorrow again


if __name__ == "__main__":
    unittest.main()
