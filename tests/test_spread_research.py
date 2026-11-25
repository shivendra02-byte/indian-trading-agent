"""Option-spread research: costs, metrics on daily marks, exits on fixed entries, experiments A/B/C."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from backend.options import spread_research as research
from backend.options import spreads
from tests._support import IsolatedStateTestCase
from tests.test_option_spreads import _store
from tests.test_option_spreads import setUpModule as _pin_eod, tearDownModule as _unpin_eod


def setUpModule():
    _pin_eod()       # end-of-day rules and the NIFTY window these tests were written for


def tearDownModule():
    _unpin_eod()


def _row(day, strike, t, price, delta, expiry):
    return {"trade_date": day, "exchange": "NSE", "symbol": "NIFTY", "expiry": expiry, "strike": strike,
            "opt_type": t, "close": price, "settle": price, "delta": delta, "oi": 1000, "iv": 0.12}


def _week(expiry: date, win: bool) -> list[dict]:
    """One NIFTY weekly cycle: Wed/Thu/Fri/Mon chains before a Tuesday expiry. The put spread decays
    to a take profit in a winning week; in a losing week it blows through the stop on Thursday."""
    rows = []
    e = expiry.isoformat()
    for k, d in enumerate([expiry - timedelta(days=6), expiry - timedelta(days=5),
                           expiry - timedelta(days=4), expiry - timedelta(days=1)]):
        short, long_ = (45, 12) if k == 0 else ((20, 5) if win else (130, 40))
        rows += [_row(d.isoformat(), 25700, "PE", 90, -0.32, e), _row(d.isoformat(), 25500, "PE", short, -0.17, e),
                 _row(d.isoformat(), 25300, "PE", long_, -0.07, e), _row(d.isoformat(), 26300, "CE", 40, 0.18, e),
                 _row(d.isoformat(), 26500, "CE", 10, 0.06, e)]
    return rows


ENTRY = {"symbol": "NIFTY", "signal_date": "2025-11-19", "expiry": "2025-11-25", "strategy": "bull_put",
         "legs": [{"type": "PE", "strike": 25500, "side": "short", "price": 45.0, "delta": -0.17},
                  {"type": "PE", "strike": 25300, "side": "long", "price": 12.0}],
         "credit": 33.0, "width": 200.0, "lot_size": 65, "lots": 2, "regime": "STRONG_UP", "vix": 13.0,
         "exit_by": "2025-11-24"}


class PureTests(unittest.TestCase):
    def setUp(self):
        self.rcfg = research.settings()
        self.scfg = spreads.settings()

    def test_costs(self):
        prices = {(25500, "PE"): 20.0, (25300, "PE"): 5.0}
        c = research.cost_points(ENTRY, prices, self.rcfg["costs"])
        # 4 orders × 0.5 slippage = 2; brokerage 80 ₹ / 130 units ≈ 0.615; STT 0.1% × (45 + 5) = 0.05; …
        self.assertGreater(c, 2.6)
        self.assertLess(c, 2.9)
        only_entry = research.cost_points(ENTRY, None, self.rcfg["costs"])
        self.assertLess(only_entry, c)
        self.assertEqual(research._stt_pct("2026-05-01", self.rcfg["costs"]), 0.15)
        self.assertEqual(research._stt_pct("2024-06-01", self.rcfg["costs"]), 0.0625)

    def test_metrics_streak_payoff_drawdown_and_daily_sharpe(self):
        def t(day, r, inr):
            return {"exit_date": day, "pnl_r": r, "pnl_inr": inr, "status": "closed", "marks": [(day, inr)]}
        trades = [t("2025-01-06", 0.5, 500), t("2025-01-07", -1.0, -1000), t("2025-01-08", -1.0, -1000),
                  t("2025-01-09", -1.0, -1000), t("2025-01-20", 0.6, 600), t("2025-04-07", 3.0, 3000)]
        m = research.metrics(trades, "2025-01-01", "2025-04-30", 1_000_000)
        self.assertEqual(m["max_losing_streak"], 3)
        self.assertEqual(m["avg_loss_r"], -1.0)
        self.assertAlmostEqual(m["win_loss_ratio"], 1.37, places=2)
        self.assertEqual(m["max_drawdown_inr"], 3000)
        self.assertEqual(m["max_drawdown_days"], (date(2025, 4, 4) - date(2025, 1, 6)).days)   # recovered 7 Apr
        self.assertIsNotNone(m["sharpe_daily_annual"])
        self.assertIsNotNone(m["sharpe_per_trade"])
        self.assertEqual(m["consistency"], 0.5)                                # Q1 negative, Q2 positive

    def test_score_and_eligibility(self):
        good = {"trades": 30, "expectancy_r": 0.3, "profit_factor": 1.8, "consistency": 0.75, "max_drawdown_r": 3}
        small = {**good, "trades": 5}
        self.assertTrue(research.eligible(good, self.rcfg)[0])
        self.assertFalse(research.eligible(small, self.rcfg)[0])
        self.assertGreater(research.score(good, self.rcfg), research.score(small, self.rcfg))

    def test_grid_has_the_live_rule(self):
        grid = research._grid(self.rcfg)
        self.assertIn(research.live_rule(self.scfg), grid)
        self.assertTrue(all(r["take_profit_pct"] or r["stop_multiple"] or r["trail"] for r in grid))


class SimulateTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.scfg, self.rcfg = spreads.settings(), research.settings()

    def test_same_entry_different_exits(self):
        _store(_week(date(2025, 11, 25), win=False))
        chains = research.Chains()
        live = research.simulate_exit(ENTRY, None, self.scfg, self.rcfg, chains, with_costs=False)
        self.assertEqual((live["exit_reason"], live["exit_date"]), ("stop", "2025-11-20"))
        self.assertAlmostEqual(live["pnl_r"], (33 - 90) / 33, places=3)          # live 1R = one credit
        wide = research.simulate_exit(ENTRY, {"take_profit_pct": 50, "stop_multiple": None, "trail": None,
                                              "partial": None}, self.scfg, self.rcfg, chains, with_costs=False)
        self.assertEqual(wide["exit_reason"], "expiry_exit")
        self.assertEqual(wide["marks"][-1][0], wide["exit_date"])
        net = research.simulate_exit(ENTRY, None, self.scfg, self.rcfg, chains)
        self.assertLess(net["pnl_inr"], live["pnl_inr"])                       # costs taken off

    def test_partial_profit(self):
        _store(_week(date(2025, 11, 25), win=True))
        rule = {"take_profit_pct": 70, "stop_multiple": 2.0, "trail": None, "partial": {"at_pct": 30, "fraction": 0.5}}
        t = research.simulate_exit(ENTRY, rule, self.scfg, self.rcfg, research.Chains(), with_costs=False)
        # 18 points of profit (55%): half banked at 30%+, the rest runs to expiry
        self.assertEqual(t["exit_reason"], "expiry_exit")
        self.assertAlmostEqual(t["pnl_points_gross"], 18.0, places=2)


class ExperimentTests(IsolatedStateTestCase, unittest.TestCase):
    def test_run_end_to_end(self):
        weeks = [date(2025, 9, 2) + timedelta(weeks=i) for i in range(12)]
        rows = []
        for i, e in enumerate(weeks):
            rows += _week(e, win=(i % 3 != 2))
        _store(rows)
        with mock.patch.object(spreads, "_regime_on", return_value="STRONG_UP"), \
                mock.patch.object(spreads, "_blocked", return_value=None), \
                mock.patch("backend.options.store.vix_on", return_value=14.0):
            res = research.run(date(2025, 8, 25), date(2025, 11, 25), date(2025, 10, 15), ["NIFTY"])
        self.assertEqual(res["status"], "ok")
        base = res["baseline"]["all"]
        self.assertGreater(base["trades"], 8)
        for key in ("expectancy_r", "win_loss_ratio", "max_losing_streak", "max_drawdown_days",
                    "sharpe_daily_annual", "consistency"):
            self.assertIn(key, base)
        self.assertTrue(any(r["is_live"] for r in res["experiment_a"]["rules"]))
        self.assertTrue(res["experiment_b"]["cells"])
        self.assertIn("walk_forward", res["experiment_c"])
        self.assertTrue(res["trades"]["baseline"])
        self.assertNotIn("marks", res["trades"]["baseline"][0])

    def test_background_run_and_csv(self):
        from tests._support import csrf_headers, fresh_test_client, login
        with mock.patch.object(research, "run", return_value={"status": "ok", "trades": {"baseline": [
                {"symbol": "NIFTY", "strategy": "bull_put", "pnl_r": 0.5}], "all_strategies": []}}):
            with fresh_test_client() as client:
                login(client)
                r = client.post("/api/options/spreads/research", headers=csrf_headers(client), json={
                    "start_date": "2025-01-01", "end_date": "2025-12-31", "split_date": "2025-07-01"})
                self.assertEqual(r.status_code, 200, r.text)
                run_id = r.json()["id"]
                for _ in range(50):
                    if not client.get("/api/options/spreads/research").json()["running"]:
                        break
                    import time
                    time.sleep(0.05)
                got = client.get(f"/api/options/spreads/research/{run_id}").json()
                self.assertEqual(got["status"], "ok")
                self.assertNotIn("trades", got["result"])
                csv_text = client.get(f"/api/options/spreads/research/{run_id}/trades.csv").text
                self.assertTrue(csv_text.startswith("symbol,strategy"))
                self.assertIn("NIFTY,bull_put", csv_text)
                bad = client.post("/api/options/spreads/research", headers=csrf_headers(client), json={
                    "start_date": "2025-01-01", "end_date": "2025-12-31", "split_date": "2026-07-01"})
                self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
