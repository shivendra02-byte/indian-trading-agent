"""One open trade per instrument (backtest + live selection), score bands, the
train/test split and the weekly post-mortem."""

import unittest
from datetime import date, datetime

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login


def trade(symbol, signal_ts, exit_ts, score=70, decision="passed", status="closed", pnl=1.0, setup="pullback"):
    return {"symbol": symbol, "signal_ts": signal_ts, "exit_ts": exit_ts, "score": score, "decision": decision,
            "status": status, "pnl_r": pnl if status == "closed" else None, "setup": setup, "direction": "long",
            "regime": "SIDEWAYS", "exit_reason": "target", "mfe_r": 1.0, "mae_r": 0.5}


class PositionBookTests(unittest.TestCase):
    def test_second_signal_while_in_a_trade_is_left_out(self):
        from backend.backtest.engine import apply_position_books
        trades = [
            trade("AAA", "2026-01-05T10:15:00", "2026-01-06T11:15:00"),
            trade("AAA", "2026-01-05T11:15:00", "2026-01-06T12:15:00"),     # stacked on the first
            trade("AAA", "2026-01-06T11:15:00", "2026-01-07T10:15:00"),     # first one exited at this bar
            trade("BBB", "2026-01-05T11:15:00", "2026-01-05T14:15:00"),     # other symbol: free
        ]
        dropped = apply_position_books(trades, per_scan=5)
        by_ts = {(t["symbol"], t["signal_ts"]): t for t in trades}
        self.assertFalse(by_ts[("AAA", "2026-01-05T10:15:00")]["stacked_all"])
        self.assertTrue(by_ts[("AAA", "2026-01-05T11:15:00")]["stacked_all"])
        self.assertFalse(by_ts[("AAA", "2026-01-06T11:15:00")]["stacked_all"])
        self.assertFalse(by_ts[("BBB", "2026-01-05T11:15:00")]["stacked_all"])
        self.assertEqual(dropped["all_trackable"], 1)
        self.assertFalse(by_ts[("AAA", "2026-01-05T11:15:00")]["shortlisted"])

    def test_no_fill_frees_the_symbol_and_open_blocks_it(self):
        from backend.backtest.engine import apply_position_books
        trades = [trade("AAA", "2026-01-05T10:15:00", None, status="no_fill"),
                  trade("AAA", "2026-01-05T11:15:00", None, status="open"),
                  trade("AAA", "2026-01-08T11:15:00", "2026-01-09T10:15:00")]
        apply_position_books(trades, per_scan=5)
        self.assertEqual([t["stacked_all"] for t in trades], [False, False, True])

    def test_books_are_kept_per_group(self):
        """A shadow (soft-rejected) trade doesn't block a later passed one in the passed group."""
        from backend.backtest.engine import apply_position_books
        trades = [trade("AAA", "2026-01-05T10:15:00", "2026-01-07T10:15:00", decision="soft_rejected"),
                  trade("AAA", "2026-01-05T12:15:00", "2026-01-06T10:15:00")]
        apply_position_books(trades, per_scan=5)
        passed = trades[1]
        self.assertTrue(passed["stacked_all"])            # all-trackable book already holds AAA
        self.assertFalse(passed["stacked_passed"])        # passed book doesn't
        self.assertTrue(passed["shortlisted"])

    def test_shortlist_takes_next_best_symbol_when_top_one_is_busy(self):
        from backend.backtest.engine import apply_position_books
        trades = [trade("AAA", "2026-01-05T10:15:00", "2026-01-07T10:15:00", score=90),
                  trade("AAA", "2026-01-05T11:15:00", "2026-01-06T10:15:00", score=95),
                  trade("BBB", "2026-01-05T11:15:00", "2026-01-06T10:15:00", score=65)]
        apply_position_books(trades, per_scan=1)
        picked = [(t["symbol"], t["signal_ts"]) for t in trades if t["shortlisted"]]
        self.assertEqual(picked, [("AAA", "2026-01-05T10:15:00"), ("BBB", "2026-01-05T11:15:00")])

    def test_summary_excludes_stacked_and_reports_bands_and_split(self):
        from backend.backtest.engine import apply_position_books, summarize
        trades = [trade("AAA", "2026-01-05T10:15:00", "2026-01-06T10:15:00", score=72, pnl=2.0),
                  trade("AAA", "2026-01-05T11:15:00", "2026-01-06T11:15:00", score=55, pnl=-1.0),
                  trade("BBB", "2026-03-02T10:15:00", "2026-03-03T10:15:00", score=45, pnl=-1.0)]
        apply_position_books(trades, per_scan=5)
        s = summarize(trades, "stock_futures_proxy", split_date="2026-02-01")
        self.assertEqual(s["groups"]["all_trackable"]["n"], 2)
        self.assertEqual(s["stacked_signals"], 1)
        self.assertEqual(set(s["by_score_band"]), {"70-80", "40-50"})
        self.assertEqual(s["by_period"]["train"]["groups"]["all_trackable"]["n"], 1)
        self.assertEqual(s["by_period"]["test"]["groups"]["all_trackable"]["avg_r"], -1.0)
        self.assertEqual(s["by_period"]["split_date"], "2026-02-01")

    def test_score_band_edges(self):
        from backend.backtest.engine import score_band
        self.assertEqual([score_band(v) for v in (None, 39.9, 60, 79.99, 100)],
                         ["no score", "0-40", "60-70", "70-80", "80-100"])


class LiveSelectionTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.journal.store import add_records
        base = dict(instrument_token=1, exchange="NFO", signal_date="2026-11-20", signal_ts="2026-11-20T10:15:00",
                    direction="long", regime="SIDEWAYS", score=70, entry=100.0, stop=96.0, target=108.0,
                    entry_date="2026-11-20", fill_until="2026-11-20T12:15:00", time_stop_date="2026-11-25")
        add_records([
            {**base, "segment": "stock_futures", "symbol": "AAA", "setup": "pullback", "decision": "shortlisted"},
            {**base, "segment": "stock_futures", "symbol": "BBB", "setup": "pullback", "decision": "soft_rejected"},
            {**base, "segment": "cash_swing", "symbol": "CCC", "setup": "pullback", "decision": "shortlisted"},
        ])

    def rows(self):
        return [{"symbol": s, "segment": "stock_futures", "score": sc, "stages": []}
                for s, sc in (("AAA", 90), ("BBB", 85), ("BBB", 80), ("CCC", 75), ("DDD", 70))]

    def test_busy_symbol_and_repeat_are_skipped(self):
        from backend.signals.selection import choose_shortlist, open_positions
        self.assertEqual(open_positions(["stock_futures"]), {("stock_futures", "AAA")})
        rows = self.rows()
        chosen = choose_shortlist(rows, 3, lambda r: r["segment"])
        self.assertEqual([r["symbol"] for r in chosen], ["BBB", "CCC", "DDD"])   # CCC is busy only in cash_swing
        self.assertEqual(rows[0]["selection_skip"], "already in a trade on AAA")
        self.assertIn("ranked higher", rows[2]["selection_skip"])
        self.assertEqual(rows[0]["stages"][-1]["stage"], "selection")

    def test_switch_off_keeps_old_behaviour(self):
        from unittest import mock
        from backend.signals.selection import choose_shortlist
        rows = self.rows()
        with mock.patch("backend.signals.selection.get_config") as cfg:
            cfg.return_value.global_.limits.one_open_trade_per_instrument = False
            chosen = choose_shortlist(rows, 2, lambda r: r["segment"])
        self.assertEqual([r["symbol"] for r in chosen], ["AAA", "BBB"])

    def test_live_config_has_the_rule_on(self):
        from backend.decision.config import get_config
        self.assertTrue(get_config().global_.limits.one_open_trade_per_instrument)


class PostmortemScheduleTests(unittest.TestCase):
    def test_due_saturday_from_8_and_sunday_once_per_week(self):
        from backend.journal.postmortem import postmortem_due
        sat = datetime(2026, 11, 28, 7, 59)
        self.assertIsNone(postmortem_due(sat, None))
        self.assertEqual(postmortem_due(sat.replace(hour=8, minute=0), None), date(2026, 11, 23))
        self.assertEqual(postmortem_due(datetime(2026, 11, 29, 20, 0), None), date(2026, 11, 23))
        self.assertIsNone(postmortem_due(datetime(2026, 11, 29, 20, 0), date(2026, 11, 23)))
        self.assertIsNone(postmortem_due(datetime(2026, 11, 27, 16, 0), None))   # Friday


class SchedulerPostmortemTests(IsolatedStateTestCase, unittest.TestCase):
    def test_tick_runs_it_once_on_saturday(self):
        from unittest import mock
        from backend.scheduler import MarketDataScheduler
        sched = MarketDataScheduler()
        state = {"paused": True, "kite_connected": False, "instruments_fresh": False, "last_morning_attempt": None,
                 "done": {}, "scans_done": {}}
        with mock.patch("backend.journal.postmortem.run_weekly_postmortem",
                        return_value={"status": "ok", "closed": 4, "ai_status": "ok"}) as run, \
                mock.patch.object(MarketDataScheduler, "_state", return_value=state), \
                mock.patch("backend.signals.cash_swing.latest_run", return_value=None):
            ran = sched.tick(datetime(2026, 11, 28, 8, 5))
            again = sched.tick(datetime(2026, 11, 28, 9, 0))
        self.assertEqual(ran, ["postmortem:weekly"])
        self.assertEqual(again, [])
        run.assert_called_once_with(date(2026, 11, 23))
        self.assertEqual(sched.last_results["postmortem:weekly"]["closed"], 4)


class TagTests(unittest.TestCase):
    def rec(self, **kw):
        base = {"direction": "long", "pnl_r": -1.0, "mfe_r": 0.2, "exit_reason": "stop", "target": 108.0,
                "extra": {}}
        return {**base, **kw}

    def tags(self, rec, bars=(), events=()):
        from backend.journal.postmortem import tag_trade
        return tag_trade(rec, list(bars), list(events))

    def test_loss_tags(self):
        self.assertEqual(self.tags(self.rec()), ["bad_entry"])
        self.assertEqual(self.tags(self.rec(mfe_r=1.2, pnl_r=0.0, exit_reason="breakeven_stop")), ["gave_back_profit"])
        self.assertIn("gap_loss", self.tags(self.rec(exit_reason="gap_stop", pnl_r=-1.4)))
        self.assertIn("stop_too_tight", self.tags(self.rec(), bars=[{"high": 108.5, "low": 101}]))
        self.assertIn("against_trend", self.tags(self.rec(extra={"context": {"day": -0.5}})))
        self.assertIn("event_hit", self.tags(self.rec(), events=["RBI policy 2026-12-05"]))

    def test_short_side_and_win_tags(self):
        short = self.rec(direction="short", target=92.0, extra={"htf_trend": 0.5})
        self.assertEqual(self.tags(short, bars=[{"high": 99, "low": 91.5}]),
                         ["bad_entry", "stop_too_tight", "against_trend"])
        self.assertEqual(self.tags(self.rec(pnl_r=2.0, mfe_r=2.1, exit_reason="target")), ["clean_win"])
        self.assertEqual(self.tags(self.rec(pnl_r=0.5, mfe_r=2.4, exit_reason="trail_stop")), ["left_on_table"])
        self.assertEqual(self.tags(self.rec(pnl_r=-0.3, mfe_r=0.8, exit_reason="time_stop")), ["clean_loss"])


class WeeklyPostmortemTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.journal.store import add_records, list_records, update_record
        base = dict(instrument_token=1, exchange="NFO", signal_date="2026-11-23", direction="long",
                    regime="SIDEWAYS", score=70, entry=100.0, stop=96.0, target=108.0, entry_date="2026-11-23",
                    fill_until="2026-11-23T12:15:00", time_stop_date="2026-11-26", segment="stock_futures")
        add_records([
            {**base, "symbol": "AAA", "setup": "pullback", "decision": "shortlisted", "signal_ts": "2026-11-23T10:15:00"},
            {**base, "symbol": "BBB", "setup": "breakout", "decision": "shortlisted", "signal_ts": "2026-11-23T10:15:00"},
            {**base, "symbol": "CCC", "setup": "breakout", "decision": "soft_rejected", "signal_ts": "2026-11-23T10:15:00"},
            {**base, "symbol": "DDD", "setup": "pullback", "decision": "shortlisted", "signal_ts": "2026-11-16T10:15:00",
             "signal_date": "2026-11-16"},
        ])
        outcomes = {"AAA": ("2026-11-25T11:15:00", "target", 2.0, 2.1), "BBB": ("2026-11-24T10:15:00", "stop", -1.0, 0.1),
                    "CCC": ("2026-11-24T14:15:00", "time_stop", 0.4, 0.9), "DDD": ("2026-11-18T10:15:00", "stop", -1.0, 0.3)}
        for r in list_records():
            exit_ts, reason, pnl, mfe = outcomes[r["symbol"]]
            update_record(r["id"], {"track_status": "closed", "fill_ts": r["signal_ts"], "exit_ts": exit_ts,
                                    "exit_reason": reason, "pnl_r": pnl, "mfe_r": mfe, "mae_r": 0.5,
                                    "pnl_rupees": pnl * 10_000})
        self.ids = {r["symbol"]: r["id"] for r in list_records()}

    def run_pm(self, **kw):
        from backend.journal.postmortem import run_weekly_postmortem
        return run_weekly_postmortem(date(2026, 11, 28), tagger=lambda r: ["clean_win" if r["pnl_r"] > 0 else "bad_entry"],
                                     **kw)

    def test_week_summary_books_and_previous_weeks(self):
        from backend.journal.postmortem import get_postmortem
        from backend.journal.store import set_user_action
        set_user_action(self.ids["AAA"], "taken", fill_price=100.5)
        calls = []
        result = self.run_pm(llm_call=lambda p: (calls.append(p), ("## What went right\nok", "fake-model"))[1])
        self.assertEqual((result["week_start"], result["closed"], result["ai_status"]), ("2026-11-23", 3, "ok"))
        pm = get_postmortem("2026-11-23")
        s = pm["summary"]
        self.assertEqual((s["books"]["paper"]["n"], s["books"]["shadow"]["n"], s["books"]["taken"]["n"]), (2, 1, 1))
        self.assertEqual(s["previous_4_weeks"]["paper"]["n"], 1)          # DDD, the week before
        self.assertEqual(s["tags"]["bad_entry"], 1)
        self.assertEqual(s["user_marked"], {"taken": 1, "skipped": 0, "unmarked_paper": 1})
        self.assertEqual(pm["ai_model"], "fake-model")
        self.assertIn("too few trades", calls[0])
        self.assertEqual({t["symbol"] for t in pm["trades"]}, {"AAA", "BBB", "CCC"})

    def test_ai_is_skipped_while_paused_and_rerun_replaces(self):
        from backend.journal.postmortem import get_postmortem, list_postmortems
        from backend.system_state import set_paused
        set_paused(True, "test")
        self.assertTrue(self.run_pm(llm_call=lambda p: self.fail("AI called while paused"))["ai_status"]
                        .startswith("skipped"))
        set_paused(False)
        self.run_pm(with_ai=False)
        self.assertEqual(len(list_postmortems()), 1)
        self.assertEqual(get_postmortem("2026-11-23")["ai_status"], "skipped: not requested")

    def test_adherence_flags_too_many_positions(self):
        from unittest import mock
        from backend.journal.postmortem import _adherence
        with mock.patch("backend.journal.postmortem.get_config") as cfg:
            g = cfg.return_value.global_
            g.limits.max_new_trades_day, g.limits.max_new_trades_week = 1, 15
            g.risk.max_open_positions, g.capital, g.drawdown.daily_loss_pct = 1, 1_000_000, 2.0
            out = _adherence(date(2026, 11, 23), date(2026, 11, 29))
        self.assertEqual(out["max_open_at_once"], 2)
        self.assertTrue(any("2 shortlisted trades" in i for i in out["issues"]))
        self.assertTrue(any("open at once" in i for i in out["issues"]))

    def test_api_marks_action_and_runs_postmortem(self):
        with fresh_test_client() as client:
            login(client)
            self._api_checks(client)

    def _api_checks(self, client):
        h = csrf_headers(client)
        r = client.put(f"/api/journal/{self.ids['BBB']}/action", json={"action": "skipped", "note": "gap risk"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["user_action"], "skipped")
        self.assertEqual(client.put("/api/journal/99999/action", json={"action": "taken"}, headers=h).status_code, 404)
        self.assertEqual(client.put(f"/api/journal/{self.ids['BBB']}/action", json={"action": "maybe"}, headers=h).status_code, 422)
        r = client.post("/api/journal/postmortem/run", json={"week_start": "2026-11-26", "with_ai": False},
                        headers=h)
        self.assertEqual(r.json()["week_start"], "2026-11-23")
        self.assertEqual(client.get("/api/journal/postmortem").json()["weeks"][0]["week_start"], "2026-11-23")
        got = client.get("/api/journal/postmortem/2026-11-25").json()
        self.assertEqual(got["summary"]["user_marked"]["skipped"], 1)
        self.assertEqual(client.get("/api/journal/postmortem/2026-01-05").status_code, 404)


if __name__ == "__main__":
    unittest.main()
