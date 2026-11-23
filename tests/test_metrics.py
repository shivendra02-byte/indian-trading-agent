"""Sharpe / Sortino / Calmar on R results."""

from __future__ import annotations

import unittest


class MetricsTests(unittest.TestCase):
    def test_per_trade_and_annual(self):
        from backend.metrics import risk_metrics
        rows = [("2026-01-05", 2.0), ("2026-01-06", -1.0), ("2026-01-12", 2.0), ("2026-01-13", -1.0)]
        m = risk_metrics(rows)
        self.assertAlmostEqual(m["sharpe_trade"], 0.5 / (3 ** 0.5), places=3)   # mean 0.5, sd sqrt(3)
        self.assertGreater(m["sortino_trade"], m["sharpe_trade"])                # only losses count as risk
        self.assertGreater(m["sharpe_annual"], 0)
        self.assertGreater(m["calmar"], 0)

    def test_losing_and_degenerate(self):
        from backend.metrics import risk_metrics
        self.assertLess(risk_metrics([("2026-01-05", -1.0), ("2026-01-06", 0.5), ("2026-01-07", -1.0)])["sharpe_trade"], 0)
        empty = risk_metrics([("2026-01-05", 1.0)])
        self.assertIsNone(empty["sharpe_trade"])
        same = risk_metrics([("2026-01-05", 1.0), ("2026-01-06", 1.0)])
        self.assertIsNone(same["sharpe_trade"])          # no variation: undefined, not infinite
        self.assertIsNone(same["calmar"])                # no drawdown
        undated = risk_metrics([(None, 1.0), (None, -1.0), (None, 2.0)])
        self.assertIsNotNone(undated["sharpe_trade"])
        self.assertIsNone(undated["sharpe_annual"])

    def test_stats_include_metrics(self):
        from backend.journal.learning import stats
        st = stats([{"pnl_r": 2.0, "exit_ts": "2026-01-05T15:00:00"}, {"pnl_r": -1.0, "exit_ts": "2026-01-08T15:00:00"},
                    {"pnl_r": 1.0, "exit_ts": "2026-01-09T15:00:00"}])
        for key in ("sharpe_trade", "sortino_trade", "sharpe_annual", "sortino_annual", "calmar"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()
