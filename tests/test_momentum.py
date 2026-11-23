"""Momentum book: ranking, buffer, trend + hourly entry rules, stop/trail, sizing, portfolio backtest, API."""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

from backend.momentum import rules
from backend.momentum.backtest import run
from backend.momentum.book import compute_ranking
from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login

CFG = {**rules.defaults(), "top_n": 2, "buffer_n": 3, "max_positions_per_side": 2, "min_turnover_cr": 0,
       "long_enabled": True, "short_enabled": True, "risk_per_trade_pct": 1.0}


def daily(closes, start=date(2023, 1, 2), vol=1_000_000):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"ts": d.isoformat() + "T00:00:00", "open": c * 0.999, "high": c * 1.01, "low": c * 0.99,
                    "close": c, "volume": vol})
        d += timedelta(days=1)
    return out


def trend(n, growth, wiggle=0.0, start=100.0):
    return [start * math.exp(growth * i) * (1 + wiggle * math.sin(i / 3)) for i in range(n)]


class RankingTests(unittest.TestCase):
    def test_rank_groups_and_buffer(self):
        rows = [{"symbol": f"S{i}", "score": float(i)} for i in range(8)]
        ranked = rules.rank(rows, CFG)
        self.assertEqual([r["symbol"] for r in ranked if r["side"] == "long"], ["S7", "S6"])
        self.assertEqual([r["symbol"] for r in ranked if r["side"] == "short"], ["S1", "S0"])
        s5 = next(r for r in ranked if r["symbol"] == "S5")
        self.assertTrue(rules.still_in_buffer(s5, "long", CFG))          # rank 3 ≤ buffer 3
        s4 = next(r for r in ranked if r["symbol"] == "S4")
        self.assertFalse(rules.still_in_buffer(s4, "long", CFG))
        self.assertFalse(rules.still_in_buffer(None, "long", CFG))

    def test_score_skips_the_last_month_and_is_relative(self):
        bench = trend(200, 0.0)
        strong = trend(200, 0.002)
        sc = rules.momentum_score(strong, bench, CFG)
        self.assertGreater(sc["score"], 0)
        crash = strong[:-21] + [strong[-22] * 0.5] * 21     # last month ignored
        self.assertEqual(rules.momentum_score(crash, bench, CFG)["score"], sc["score"])

    def test_compute_ranking_drops_stale_data(self):
        nifty = daily(trend(200, 0.0))
        uni = [{"symbol": "UP"}, {"symbol": "DOWN"}, {"symbol": "OLD"}]
        series = {"UP": daily(trend(200, 0.003)), "DOWN": daily(trend(200, -0.003)), "OLD": daily(trend(190, 0.003))}
        as_of = date.fromisoformat(nifty[-1]["ts"][:10])
        ranked = compute_ranking(uni, lambda u: series[u["symbol"]], nifty, as_of, CFG)
        self.assertEqual([r["symbol"] for r in ranked], ["UP", "DOWN"])


class EntryExitTests(unittest.TestCase):
    def test_trend_filter(self):
        self.assertTrue(rules.trend_ok(daily(trend(120, 0.003)), 1, CFG)[0])
        self.assertFalse(rules.trend_ok(daily(trend(120, 0.003)), -1, CFG)[0])
        self.assertTrue(rules.trend_ok(daily(trend(120, -0.003)), -1, CFG)[0])

    def test_hourly_pullback_then_break(self):
        closes = [100 + 0.2 * i for i in range(60)]                 # trend: well above the 20 EMA
        closes += [111.8 - 0.4 * i for i in range(1, 7)]            # pullback into the EMA
        closes += [109.6, 113.5]                                    # base, then a close above the last 5 highs
        bars = [{"ts": f"2025-01-{1 + i // 7:02d}T{9 + i % 7:02d}:15:00", "open": c - 0.1, "high": c + 0.2,
                 "low": c - 0.2, "close": c} for i, c in enumerate(closes)]
        trig = rules.entry_trigger(bars, 1, CFG)
        self.assertIsNotNone(trig)
        self.assertLess(trig["pullback_extreme"], trig["entry"])
        self.assertIsNone(rules.entry_trigger(bars[:-1], 1, CFG))  # no break yet

    def test_follow_trails_and_stops(self):
        pos = {"side": "long", "entry": 100.0, "stop": 96.0, "initial_stop": 96.0, "best": 100.0, "atr": 2.0,
               "last_ts": "t00"}
        bars = [{"ts": f"t{i:02d}", "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5, "close": c}
                for i, (o, c) in enumerate([(101, 102), (102, 106), (106, 110), (110, 108), (107, 103)], 1)]
        out = rules.follow(pos, bars, CFG)
        self.assertEqual(out["exit"]["reason"], "trail_stop")
        self.assertAlmostEqual(out["exit"]["price"], 110 - 3 * 2.0)
        self.assertAlmostEqual(rules.pnl_r(pos, out["exit"]["price"]), 1.0)
        gap = rules.follow(pos, [{"ts": "t01", "open": 90, "high": 91, "low": 89, "close": 90}], CFG)
        self.assertEqual((gap["exit"]["reason"], gap["exit"]["price"]), ("stop", 90))

    def test_lot_sizing(self):
        self.assertEqual(rules.size_lots(1_000_000, 1.0, 1000, 980, 250), (2, ""))
        lots, why = rules.size_lots(1_000_000, 1.0, 5000, 4800, 125)
        self.assertEqual(lots, 0)
        self.assertIn("1 lot risks", why)


class BacktestTests(unittest.TestCase):
    def test_portfolio_backtest_runs_point_in_time(self):
        n = 400
        nifty = daily(trend(n, 0.0005))
        uni = [{"symbol": f"S{i}"} for i in range(6)]
        series = {f"S{i}": daily(trend(n, (i - 2.5) * 0.0015, wiggle=0.03)) for i in range(6)}
        start = date.fromisoformat(nifty[200]["ts"][:10])
        end = date.fromisoformat(nifty[-1]["ts"][:10])
        res = run(start, end, uni, nifty, lambda u: series[u["symbol"]], lambda u: [], cfg=CFG,
                  daily_only=True, split_date=date.fromisoformat(nifty[300]["ts"][:10]))
        self.assertEqual(res["status"], "ok")
        s = res["summary"]
        self.assertGreater(s["book"]["trades"], 0)
        self.assertIn("sharpe_annual", s["book"])
        self.assertEqual(set(s["by_side"]), {"long", "short"})
        self.assertIn("train", s["by_period"])
        closed_r = sum(t["pnl_r"] for t in res["trades"] if t["exit_reason"] != "open_at_end")
        open_r = sum(t["pnl_r"] for t in res["trades"] if t["exit_reason"] == "open_at_end")
        self.assertAlmostEqual(sum(r for _, r in res["equity"]), closed_r + open_r, places=1)
        longs = [t for t in res["trades"] if t["side"] == "long"]
        self.assertTrue(all(t["symbol"] in ("S4", "S5", "S3") for t in longs))


class ApiTests(IsolatedStateTestCase, unittest.TestCase):
    def test_status_endpoint(self):
        with fresh_test_client() as client:
            login(client)
            r = client.get("/api/momentum/")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["long_enabled"])
            self.assertFalse(body["short_enabled"])
            self.assertEqual(body["open"], [])
            bad = client.post("/api/momentum/backtests", json={"start_date": "2025-01-01", "end_date": "2024-01-01"},
                              headers=csrf_headers(client))
            self.assertEqual(bad.status_code, 400)


class HourlyRunTests(IsolatedStateTestCase, unittest.TestCase):
    def test_skipped_while_paused(self):
        from backend.momentum import book
        with mock.patch("backend.system_state.is_paused", return_value=True):
            self.assertEqual(book.hourly_run()["reason"], "paused")
            self.assertEqual(book.daily_run()["reason"], "paused")


if __name__ == "__main__":
    unittest.main()
