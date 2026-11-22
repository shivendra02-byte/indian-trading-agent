"""Rule backtester: point-in-time views, rule-set override, history backfill, end-to-end runs."""

import math
import random
import unittest
from datetime import date, datetime, timedelta

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login
from datetime import timedelta  # noqa: F811
from tests.test_signals import make_bars
from tradingagents.utils.market_calendar import is_trading_day

NSE_STARTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]


def walk(n, start, seed, drift=0.0006, vol=0.006):
    rnd = random.Random(seed)
    out, p = [], start
    for i in range(n):
        p *= 1 + drift + vol * rnd.gauss(0, 1) + 0.003 * math.sin(i / 9)
        out.append(p)
    return out


def hourly_series(first: date, last: date, start=100.0, seed=1, volume=40_000):
    days, d = [], first
    while d <= last:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    closes = walk(len(days) * 7, start, seed, drift=0.0001, vol=0.004)
    bars, prev, k = [], closes[0], 0
    for d in days:
        for s in NSE_STARTS:
            c = closes[k]
            bars.append({"date": f"{d.isoformat()}T{s}:00", "open": prev, "high": max(prev, c) * 1.002,
                         "low": min(prev, c) * 0.998, "close": c, "volume": volume * (1 + (k % 5))})
            prev, k = c, k + 1
    return bars


def daily_from_hourly(hourly):
    days = {}
    for b in hourly:
        d = b["date"][:10]
        x = days.get(d)
        if x is None:
            days[d] = {"date": f"{d}T00:00:00", "open": b["open"], "high": b["high"], "low": b["low"],
                       "close": b["close"], "volume": b["volume"]}
        else:
            x.update(high=max(x["high"], b["high"]), low=min(x["low"], b["low"]), close=b["close"],
                     volume=x["volume"] + b["volume"])
    return [days[k] for k in sorted(days)]


class IncrementalViewTests(unittest.TestCase):
    def test_views_never_use_later_bars(self):
        from backend.backtest.engine import hours_view, weekly_view
        from backend.marketdata.candles import resample_hours, resample_weekly
        hourly = [{**b, "ts": b["date"]} for b in hourly_series(date(2026, 9, 1), date(2026, 10, 30))]
        daily = [{**b, "ts": b["date"]} for b in daily_from_hourly(hourly)]
        for hours in (2, 4):
            view = hours_view(hourly, hours)
            for i in range(0, len(hourly), 3):
                self.assertEqual(view.upto(i, limit=500), resample_hours(hourly[:i + 1], "NSE", hours)[-500:])
        wview = weekly_view(daily)
        for i in range(len(daily)):
            self.assertEqual(wview.upto(i), resample_weekly(daily[:i + 1])[-300:])


class RuleSetTests(unittest.TestCase):
    def test_baseline_rule_set_differs_and_override_is_scoped(self):
        from backend.decision.config import available_rule_sets, get_config, use_config
        sets = available_rule_sets()
        self.assertIn("v1_baseline", sets)
        current = get_config().rule_version
        with use_config(sets["v1_baseline"]):
            cfg = get_config()
            self.assertNotEqual(cfg.rule_version, current)
            self.assertEqual(cfg.instrument("stock_futures").confirmation_tfs, [])
            self.assertEqual(cfg.strategy("range_reversal").lookback, 60)
            self.assertFalse(cfg.global_.system.live_order_execution)
        self.assertEqual(get_config().rule_version, current)

    def test_every_rule_set_loads_and_stays_paper_only(self):
        from backend.decision.config import available_rule_sets, get_config, use_config
        for name, path in available_rule_sets().items():
            with self.subTest(rule_set=name), use_config(path):
                cfg = get_config()
                self.assertTrue(cfg.global_.system.paper_trading_only)
                self.assertFalse(cfg.global_.system.live_order_execution)

    def test_fixed_2r_targets_exactly_two_r(self):
        from backend.decision.config import available_rule_sets, get_config, use_config
        with use_config(available_rule_sets()["fixed_2r"]):
            g = get_config().global_.geometry
            self.assertEqual((g.max_rr, g.no_level_rr), (2.0, 2.0))

    def test_summary_drawdown_and_groups(self):
        from backend.backtest.engine import max_drawdown_r, summarize
        rows = [{"pnl_r": r, "exit_ts": f"2026-01-0{i + 1}", "status": "closed", "decision": "passed",
                 "setup": "pullback", "direction": "long", "regime": "SIDEWAYS", "exit_reason": "target",
                 "soft_checks": {"regime": True}, "trail_pnl_r": None, "shortlisted": i < 2}
                for i, r in enumerate([2.0, -1.0, -1.0, 3.0])]
        self.assertEqual(max_drawdown_r(rows), 2.0)
        s = summarize(rows)
        self.assertEqual(s["groups"]["all_trackable"]["total_r"], 3.0)
        self.assertEqual(s["groups"]["shortlisted"]["n"], 2)
        self.assertIn("regime", s["soft_checks"])


class FakeHistoryKite:
    def __init__(self, bars):
        self.bars, self.calls = bars, []

    def historical_data(self, token, start, end, interval, continuous=False, oi=False):
        self.calls.append((start, end))
        return [b for b in self.bars if start <= datetime.fromisoformat(b["date"]) < end]


class BackfillTests(IsolatedStateTestCase, unittest.TestCase):
    def test_backfill_extends_history_backwards_in_chunks(self):
        from backend.backtest.data import backfill_series, first_stored
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        ensure_marketdata_tables()
        hourly = hourly_series(date(2025, 6, 2), date(2026, 11, 20))
        save_candles(7, "60minute", [b for b in hourly if b["date"] >= "2026-10-01"])
        client = FakeHistoryKite(hourly)
        saved = backfill_series(client, 7, "60minute", datetime(2025, 6, 2), now=datetime(2026, 11, 21, 9),
                                sleep=lambda s: None)
        self.assertEqual(saved, sum(1 for b in hourly if b["date"] < "2026-10-01"))
        self.assertEqual(first_stored(7, "60minute"), datetime(2025, 6, 2, 9, 15))
        self.assertGreaterEqual(len(client.calls), 2)
        self.assertEqual(backfill_series(client, 7, "60minute", datetime(2025, 6, 2), now=datetime(2026, 11, 21, 9),
                                         sleep=lambda s: None), 0)


class EndToEndTests(IsolatedStateTestCase, unittest.TestCase):
    SEED = 3

    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "STOCK1", "STOCK1", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "STOCK199DECFUT", "STOCK1", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-20")])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in
                                     make_bars(walk(600, 20000, seed=7, drift=0.0004, vol=0.008),
                                               end=date(2026, 11, 20))])
        hourly = hourly_series(date(2025, 11, 3), date(2026, 11, 20), start=1500, seed=self.SEED)
        early = make_bars(walk(320, 1000, seed=5), end=date(2025, 10, 31), volume=2_000_000)
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in early] + daily_from_hourly(hourly))
        save_candles(501, "60minute", hourly)

    def _run(self, segment, rule_set):
        from backend.backtest import engine
        from backend.decision.config import available_rule_sets
        engine.ensure_tables()
        with self.db.get_db() as conn:
            run_id = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status) "
                                  "VALUES (?, ?, '2026-05-01', '2026-10-30', 'running')",
                                  (segment, rule_set)).lastrowid
        summary = engine.run_backtest(run_id, segment, available_rule_sets()[rule_set],
                                      date(2026, 5, 1), date(2026, 10, 30))
        return summary, engine.get_run(run_id, with_trades=True)

    def test_futures_proxy_runs_both_rule_sets_without_look_ahead(self):
        results = {}
        for rule_set in ("current", "v1_baseline"):
            summary, run = self._run("stock_futures_proxy", rule_set)
            self.assertEqual(run["status"], "done")
            self.assertTrue(summary["limitations"])
            for t in run["trades"]:
                self.assertTrue("2026-05-01" <= t["signal_ts"][:10] <= "2026-10-30")
                if t.get("exit_ts"):
                    self.assertGreater(t["exit_ts"], t["signal_ts"])
                self.assertNotIn(t["signal_ts"][:10], ("2026-08-05", "2026-10-07"))  # RBI days blocked
            results[rule_set] = summary
        self.assertGreater(results["current"]["trades"], 0)
        self.assertGreater(results["v1_baseline"]["trades"], 0)

    def test_routes(self):
        with fresh_test_client() as client:
            login(client)
            sets = client.get("/api/rule-backtests/rule-sets").json()
            self.assertIn("v1_baseline", sets["rule_sets"])
            bad = client.post("/api/rule-backtests", headers=csrf_headers(client),
                              json={"segment": "cash_swing", "rule_set": "nope",
                                    "start_date": "2026-05-01", "end_date": "2026-06-01"})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(client.get("/api/rule-backtests").json()["runs"], [])
            self.assertEqual(client.get("/api/rule-backtests/99").status_code, 404)
            self.assertEqual(client.get("/api/rule-backtests/history").json()["status"], "idle")
            self.assertIn("cup_handle", sets["segments"])


class IndexAndDailyOnlyTests(EndToEndTests):
    SEED = 17

    test_futures_proxy_runs_both_rule_sets_without_look_ahead = None
    test_routes = None

    def test_index_futures_without_volume(self):
        from backend.marketdata.candles import save_candles
        save_candles(256265, "60minute", hourly_series(date(2025, 11, 3), date(2026, 11, 20), start=20000, seed=9,
                                                      volume=0))
        summary, run = self._run("index_futures_proxy", "current")
        self.assertEqual(run["status"], "done", run.get("progress"))
        self.assertIn("volume", " ".join(summary["limitations"]))
        for t in run["trades"]:
            self.assertEqual(t["symbol"], "NIFTY")
            self.assertEqual(t["untested"], ["volume", "oi"])
            self.assertIsNone((t["soft_checks"] or {}).get("volume"))
        funnel = summary["funnel"]
        self.assertGreater(funnel["counts"]["bars_evaluated"], 0)
        self.assertGreater(sum(funnel["regime_days"].values()), 0)
        self.assertTrue(funnel["rejections"])
        self.assertEqual(summary["exit_mode"], "fixed")

    def test_index_futures_with_sideways_mean_reversion_and_managed_exit(self):
        from backend.marketdata.candles import save_candles
        save_candles(256265, "60minute", hourly_series(date(2025, 11, 3), date(2026, 11, 20), start=20000, seed=9,
                                                      volume=0))
        summary, run = self._run("index_futures_proxy", "sideways_mr_1r")
        self.assertEqual(run["status"], "done", run.get("progress"))
        self.assertEqual(summary["exit_mode"], "breakeven_trail")
        for t in run["trades"]:
            if t["status"] == "closed":
                self.assertIsNotNone(t["fixed_pnl_r"])     # the plain exit is kept for comparison
            if t["regime"] == "SIDEWAYS" and t["setup"] in ("pullback", "breakout"):
                self.assertNotEqual(t["decision"], "passed")

    def test_cash_swing_uses_daily_bars_before_hourly_history(self):
        from backend.backtest import engine
        from backend.decision.config import available_rule_sets
        engine.ensure_tables()
        with self.db.get_db() as conn:
            run_id = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status) "
                                  "VALUES ('cash_swing', 'two_stage', '2025-08-15', '2026-10-30', 'running')").lastrowid
        summary = engine.run_backtest(run_id, "cash_swing", available_rule_sets()["two_stage"],
                                      date(2025, 8, 15), date(2026, 10, 30))
        trades = engine.get_run(run_id, with_trades=True)["trades"]
        self.assertIn("by_setup_regime", summary)
        self.assertTrue(trades)
        for t in trades:
            if t["data"] == "daily_only":
                self.assertLess(t["signal_ts"], "2025-12-15")
            if t["signal_ts"] < "2025-11-03":
                self.assertEqual(t["data"], "daily_only")
                self.assertEqual(t["untested"], ["4h"])


class DailyOnlyOptionTests(EndToEndTests):
    SEED = 17

    test_futures_proxy_runs_both_rule_sets_without_look_ahead = None
    test_routes = None

    def test_daily_only_models_every_entry_on_daily_bars(self):
        from backend.backtest import engine
        from backend.decision.config import available_rule_sets
        engine.ensure_tables()
        with self.db.get_db() as conn:
            run_id = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status) "
                                  "VALUES ('cash_swing', 'current', '2026-05-01', '2026-10-30', 'running')").lastrowid
        summary = engine.run_backtest(run_id, "cash_swing", available_rule_sets()["current"],
                                      date(2026, 5, 1), date(2026, 10, 30), daily_only=True)
        trades = engine.get_run(run_id, with_trades=True)["trades"]
        self.assertTrue(summary["daily_only"])
        self.assertIn("2026", summary["by_year"])
        self.assertTrue(trades)
        self.assertTrue(all(t["data"] == "daily_only" and t["untested"] == ["4h"] for t in trades))


class CupHandleBacktestTests(IsolatedStateTestCase, unittest.TestCase):
    def test_cup_handle_backtest_finds_the_breakout(self):
        from backend.backtest import engine
        from backend.decision.config import available_rule_sets
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from tests.test_two_stage_and_cup import cup_bars, flat_nifty
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "CUPCO", "CUPCO", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "CUPCO99DECFUT", "CUPCO", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-20")])
        bars = [{**b, "volume": b["volume"] * 10} for b in cup_bars(end=date(2026, 9, 1))]
        after, d, price = [], date(2026, 9, 2), bars[-1]["close"]
        while d <= date(2026, 11, 20):
            if is_trading_day(d):
                price *= 1.006   # reaches +20% well inside the 8-week hold
                after.append({"ts": f"{d.isoformat()}T00:00:00", "open": price * 0.999, "high": price * 1.003,
                              "low": price * 0.997, "close": price, "volume": 10_000_000})
            d += timedelta(days=1)
        bars += after
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in bars])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in flat_nifty(bars)])
        engine.ensure_tables()
        with self.db.get_db() as conn:
            run_id = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status) "
                                  "VALUES ('cup_handle', 'current', '2026-08-01', '2026-11-20', 'running')").lastrowid
        summary = engine.run_backtest(run_id, "cup_handle", available_rule_sets()["current"],
                                      date(2026, 8, 1), date(2026, 11, 20))
        trades = engine.get_run(run_id, with_trades=True)["trades"]
        self.assertEqual([t["signal_ts"] for t in trades], ["2026-09-01"], summary)
        self.assertEqual(trades[0]["data"], "daily_only")
        self.assertEqual((trades[0]["status"], trades[0]["exit_reason"]), ("closed", "target"), trades[0])


class CashSwingEndToEndTests(EndToEndTests):
    SEED = 17   # this synthetic path produces cash swing setups

    test_futures_proxy_runs_both_rule_sets_without_look_ahead = None
    test_routes = None

    def test_cash_swing_runs(self):
        summary, run = self._run("cash_swing", "current")
        self.assertEqual(run["status"], "done")
        self.assertGreater(summary["trades"], 0)
        for t in run["trades"]:
            self.assertEqual(t["direction"], "long")
            if t.get("exit_ts"):
                self.assertGreater(t["exit_ts"][:10], t["signal_ts"][:10])


if __name__ == "__main__":
    unittest.main()
