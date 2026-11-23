"""No silent days: stall warning, limit-block note, AI usage counter (asked 23 Nov 2026)."""

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from backend import watchdog

MON = date(2026, 11, 23)   # NSE trading day (24 Nov is the holiday)


def at(h, m):
    return datetime(2026, 11, 23, h, m)


class StallReasonTest(unittest.TestCase):
    def test_quiet_before_75_minutes_into_the_session(self):
        self.assertIsNone(watchdog.stall_reason(at(10, 20), "NSE", None, at(8, 0), False, True))

    def test_warns_when_no_scan_for_75_minutes(self):
        msg, since = watchdog.stall_reason(at(12, 0), "NSE", at(10, 17), at(8, 0), False, True)
        self.assertIn("since 10:17", msg)
        self.assertEqual(since, at(10, 17))

    def test_recent_scan_or_restart_is_fine(self):
        self.assertIsNone(watchdog.stall_reason(at(12, 0), "NSE", at(11, 17), at(8, 0), False, True))
        self.assertIsNone(watchdog.stall_reason(at(12, 0), "NSE", None, at(11, 30), False, True))

    def test_paused_closed_and_no_kite(self):
        self.assertIsNone(watchdog.stall_reason(at(12, 0), "NSE", None, at(8, 0), True, True))
        self.assertIsNone(watchdog.stall_reason(at(16, 30), "NSE", None, at(8, 0), False, True))
        msg, _ = watchdog.stall_reason(at(11, 0), "NSE", None, at(8, 0), False, False)
        self.assertIn("Kite is not logged in", msg)

    def test_one_message_per_stall(self):
        alerted = {}
        with mock.patch("backend.notifications.telegram.send_html_message") as send:
            self.assertEqual(watchdog.check_and_warn(at(12, 0), "NSE", at(10, 17), at(8, 0), False, True, alerted), "sent")
            self.assertIsNone(watchdog.check_and_warn(at(12, 30), "NSE", at(10, 17), at(8, 0), False, True, alerted))
            self.assertEqual(send.call_count, 1)
            # scans resume, then stall again later: a new message
            self.assertEqual(watchdog.check_and_warn(at(14, 40), "NSE", at(13, 17), at(8, 0), False, True, alerted), "sent")
            self.assertEqual(send.call_count, 2)


class LimitNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TRADINGAGENTS_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_once_per_reason_per_day(self):
        from backend.journal import panel
        rec = {"symbol": "RELIANCE", "segment": "stock_futures", "setup": "pullback", "direction": "long", "extra": {}}
        self.assertEqual(panel._notice_key("daily trade limit reached (5/5)"), "daily trade limit reached")
        with mock.patch.object(panel, "get_db", side_effect=_db(self.tmp.name)), \
                mock.patch.object(panel, "_send", return_value="sent") as send:
            self.assertEqual(panel.limit_notice(MON, "daily trade limit reached (5/5)", rec), "sent")
            self.assertIsNone(panel.limit_notice(MON, "daily trade limit reached (6/5)", rec))
            self.assertEqual(panel.limit_notice(MON, "open position limit reached (10/10)", rec), "sent")
            self.assertEqual(panel.limit_notice(date(2026, 11, 25), "daily trade limit reached (5/5)", rec), "sent")
        self.assertEqual(send.call_count, 3)
        self.assertIn("RELIANCE", send.call_args_list[0][0][0])

    def test_failed_send_is_retried(self):
        from backend.journal import panel
        rec = {"symbol": "TCS", "segment": "stock_futures", "setup": "breakout", "direction": "short", "extra": {}}
        with mock.patch.object(panel, "get_db", side_effect=_db(self.tmp.name)):
            with mock.patch.object(panel, "_send", return_value="failed: timeout"):
                panel.limit_notice(MON, "daily loss stop hit (₹-25,000)", rec)
            with mock.patch.object(panel, "_send", return_value="sent") as send:
                self.assertEqual(panel.limit_notice(MON, "daily loss stop hit (₹-26,000)", rec), "sent")
                send.assert_called_once()


def _db(folder):
    import contextlib
    import sqlite3

    @contextlib.contextmanager
    def connect():
        conn = sqlite3.connect(os.path.join(folder, "notices.db"))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    return lambda: connect()


if __name__ == "__main__":
    unittest.main()
