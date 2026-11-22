"""Deleting backtest runs, and your own fills/exits in the weekly post-mortem."""

import json
import unittest
from datetime import date

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login


class DeleteRunTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.backtest import engine
        engine.ensure_tables()
        with self.db.get_db() as conn:
            for status in ("done", "error", "running", "interrupted"):
                conn.execute("INSERT INTO rule_backtests (segment, rule_set, rule_version, start_date, end_date, status, "
                             "progress, options) VALUES ('cash_swing', 'current', 'v1', '2026-01-01', '2026-06-01', ?, '', ?)",
                             (status, json.dumps({})))
        self.engine = engine

    def ids(self):
        with self.db.get_db() as conn:
            return {r["status"]: r["id"] for r in conn.execute("SELECT id, status FROM rule_backtests")}

    def test_delete_one_and_refuse_running(self):
        ids = self.ids()
        self.assertEqual(self.engine.delete_run(ids["done"]), "deleted")
        self.assertEqual(self.engine.delete_run(ids["running"]), "running")
        self.assertEqual(self.engine.delete_run(99999), "not_found")
        self.assertNotIn("done", self.ids())

    def test_delete_all_finished_keeps_running(self):
        self.assertEqual(self.engine.delete_finished_runs(), 3)
        self.assertEqual(list(self.ids()), ["running"])

    def test_api(self):
        ids = self.ids()
        with fresh_test_client() as client:
            login(client)
            h = csrf_headers(client)
            # App startup marks running runs as interrupted (a restart); make one running again.
            with self.db.get_db() as conn:
                conn.execute("UPDATE rule_backtests SET status = 'running' WHERE id = ?", (ids["running"],))
            self.assertEqual(client.delete(f"/api/rule-backtests/{ids['error']}", headers=h).json(), {"deleted": ids["error"]})
            self.assertEqual(client.delete(f"/api/rule-backtests/{ids['running']}", headers=h).status_code, 409)
            self.assertEqual(client.delete("/api/rule-backtests/99999", headers=h).status_code, 404)
            self.assertEqual(client.delete("/api/rule-backtests", headers=h).json(), {"deleted": 2})
            self.assertEqual(len(client.get("/api/rule-backtests").json()["runs"]), 1)


class UserResultTests(unittest.TestCase):
    def rec(self, **kw):
        base = {"direction": "long", "entry": 100.0, "stop": 96.0, "pnl_r": 1.0, "user_action": "taken",
                "user_fill_price": 100.4, "user_exit_price": 106.4}
        return {**base, **kw}

    def test_long_and_short(self):
        from backend.journal.postmortem import user_result
        self.assertEqual(user_result(self.rec()), {"entry_diff_r": 0.1, "user_r": 1.5, "deviation_r": 0.5,
                                                   "user_tag": "your_exit_better"})
        short = self.rec(direction="short", entry=100.0, stop=104.0, user_fill_price=99.6, user_exit_price=101.6,
                         pnl_r=-1.0)
        self.assertEqual(user_result(short), {"entry_diff_r": 0.1, "user_r": -0.5, "deviation_r": 0.5,
                                              "user_tag": "your_exit_better"})
        self.assertEqual(user_result(self.rec(user_exit_price=104.4, pnl_r=1.0))["user_tag"], "same_as_rules")
        self.assertEqual(user_result(self.rec(user_exit_price=101.0))["user_tag"], "your_exit_worse")

    def test_needs_taken_and_fill(self):
        from backend.journal.postmortem import user_result
        self.assertEqual(user_result(self.rec(user_action="skipped")), {})
        self.assertEqual(user_result(self.rec(user_fill_price=None)), {})
        self.assertEqual(set(user_result(self.rec(user_exit_price=None))), {"entry_diff_r"})


class ExecutionSummaryTests(IsolatedStateTestCase, unittest.TestCase):
    def test_postmortem_compares_your_trades(self):
        from backend.journal.postmortem import get_postmortem, run_weekly_postmortem
        from backend.journal.store import add_records, list_records, set_user_action, update_record
        base = dict(instrument_token=1, exchange="NFO", signal_date="2026-11-23", signal_ts="2026-11-23T10:15:00",
                    direction="long", regime="SIDEWAYS", score=70, entry=100.0, stop=96.0, target=108.0,
                    entry_date="2026-11-23", fill_until="2026-11-23T12:15:00", time_stop_date="2026-11-26",
                    segment="stock_futures", setup="pullback", decision="shortlisted")
        add_records([{**base, "symbol": "AAA"}, {**base, "symbol": "BBB"}])
        for r in list_records():
            update_record(r["id"], {"track_status": "closed", "exit_ts": "2026-11-25T11:15:00", "exit_reason": "target",
                                    "pnl_r": 2.0, "mfe_r": 2.0})
        ids = {r["symbol"]: r["id"] for r in list_records()}
        set_user_action(ids["AAA"], "taken", fill_price=100.0, exit_price=104.0)      # you took 1R, rules 2R
        set_user_action(ids["BBB"], "skipped", note="results tomorrow")
        run_weekly_postmortem(date(2026, 11, 28), with_ai=False)
        pm = get_postmortem("2026-11-23")
        ex = pm["summary"]["your_execution"]
        self.assertEqual((ex["taken_with_exit"], ex["your_avg_r"], ex["rules_avg_r_same_trades"],
                          ex["total_deviation_r"], ex["exit_worse"]), (1, 1.0, 2.0, -1.0, 1))
        self.assertEqual(ex["skip_notes"], [{"symbol": "BBB", "note": "results tomorrow"}])
        aaa = next(t for t in pm["trades"] if t["symbol"] == "AAA")
        self.assertEqual((aaa["user_r"], aaa["deviation_r"]), (1.0, -1.0))


if __name__ == "__main__":
    unittest.main()
