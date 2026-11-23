"""Trailing stop shown while open ("Stop now") + Telegram "move your stop-loss" (asked 23 Nov 2026)."""

import unittest
from unittest import mock

from backend.decision.config import get_config
from backend.journal import panel
from backend.journal.simulate import simulate


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "eod": True}


def _sim(bars):
    # long, entry 100, stop 95 (1R = 5), target 125 (5R), ATR 2 -> trail 4 below the best close
    return simulate(direction="long", entry=100.0, stop=95.0, target=125.0, atr=2.0, bars=bars,
                    fill_until="2026-12-01T23:59:59", time_stop_date="2026-12-31",
                    exit_rule=get_config().exit_rule("stock_futures"))


class StopStateTest(unittest.TestCase):
    def test_states_while_open(self):
        b = [_bar("2026-12-01", 100, 101, 99.5, 100.5)]
        r = _sim(b)
        self.assertEqual((r["status"], r["stop_state"], r["trail_stop"]), ("open", "initial", 95.0))
        b.append(_bar("2026-12-02", 100.5, 105.5, 100.2, 103.0))     # +1.1R high, close 103: breakeven
        r = _sim(b)
        self.assertEqual(r["stop_state"], "trailing" if r["trail_stop"] > 100 else "breakeven")
        self.assertGreaterEqual(r["trail_stop"], 100.0)
        b.append(_bar("2026-12-03", 103, 110.5, 102.8, 110.0))       # best close 110 -> trail 106
        r = _sim(b)
        self.assertEqual((r["stop_state"], r["trail_stop"], r["last_close"]), ("trailing", 106.0, 110.0))


class StopMoveAlertTest(unittest.TestCase):
    rec = {"id": 1, "symbol": "NIFTY", "segment": "index_futures", "setup": "pullback", "direction": "long",
           "stop": 95.0, "target": 125.0, "decision": "approved", "panel": {}, "extra": {}}

    def now(self, level, state="trailing"):
        return {"level": level, "state": state, "locked_r": round((level - 100) / 5, 2), "mfe_r": 2.0,
                "last_close": level + 4, "at": "2026-12-03T10:15:00"}

    def test_first_move_then_only_big_moves(self):
        with mock.patch.object(panel, "_send", return_value="sent") as send:
            first = panel.stop_move_alert(self.rec, self.now(100.0, "breakeven"), 5.0)
            self.assertEqual((first["alert"], first["alerted_level"]), ("sent", 100.0))
            self.assertIn("SL 95 → <b>100</b> (breakeven)", send.call_args[0][0])
            rec = {**self.rec, "extra": {"stop_now": first}}
            small = panel.stop_move_alert(rec, self.now(101.0), 5.0)      # +0.2R: below 0.25R
            self.assertEqual(send.call_count, 1)
            self.assertEqual(small["alerted_level"], 100.0)
            big = panel.stop_move_alert({**rec, "extra": {"stop_now": small}}, self.now(102.0), 5.0)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(big["alerted_level"], 102.0)

    def test_no_message_for_unalerted_or_initial(self):
        with mock.patch.object(panel, "_send", return_value="sent") as send:
            panel.stop_move_alert({**self.rec, "decision": "shortlisted"}, self.now(103.0), 5.0)
            panel.stop_move_alert(self.rec, self.now(95.0, "initial"), 5.0)
            send.assert_not_called()
            rules_only = {**self.rec, "decision": "shortlisted", "panel": {"alert": "sent"}}
            panel.stop_move_alert(rules_only, self.now(103.0), 5.0)    # "rules only" alert was sent
            send.assert_called_once()

    def test_failed_send_retries_next_run(self):
        with mock.patch.object(panel, "_send", return_value="failed: timeout"):
            out = panel.stop_move_alert(self.rec, self.now(100.0, "breakeven"), 5.0)
        self.assertIsNone(out["alerted_level"])
        self.assertEqual(out["alert"], "failed: timeout")


if __name__ == "__main__":
    unittest.main()


class TrackerStoresStopNowTest(unittest.TestCase):
    def test_open_record_gets_stop_now(self):
        from backend.journal import tracker
        rec = {"id": 7, "symbol": "NIFTY", "segment": "index_futures", "setup": "pullback", "direction": "long",
               "entry": 100.0, "stop": 95.0, "target": 125.0, "atr": 2.0, "exchange": "NFO",
               "fill_until": "2026-12-01T23:59:59", "time_stop_date": "2026-12-28", "decision": "approved",
               "panel": {}, "extra": {"tradingsymbol": "NIFTY26DECFUT"}}
        bars = [_bar("2026-12-01T10:15:00", 100, 101, 99.5, 100.5),
                _bar("2026-12-02T10:15:00", 100.5, 105.5, 100.2, 103.0),
                _bar("2026-12-03T10:15:00", 103, 110.5, 102.8, 110.0)]
        with mock.patch.object(tracker, "hourly_bars", return_value=(bars, None)), \
                mock.patch.object(panel, "_send", return_value="sent") as send:
            fields = tracker.track_record(rec)
        sn = fields["extra"]["stop_now"]
        self.assertEqual(fields["track_status"], "open")
        self.assertEqual((sn["level"], sn["state"], sn["locked_r"]), (106.0, "trailing", 1.2))
        self.assertEqual(sn["alerted_level"], 106.0)
        self.assertEqual(fields["extra"]["tradingsymbol"], "NIFTY26DECFUT")   # other extras kept
        self.assertIn("NIFTY26DECFUT", send.call_args[0][0])
