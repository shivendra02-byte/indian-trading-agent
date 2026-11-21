"""Rule backtester: point-in-time views, rule-set override, history backfill, end-to-end runs."""

import math
import random
import unittest
from datetime import date, datetime, timedelta

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login
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
