"""Bullish Wolfe Wave detection, trade construction, per-book exit rule (synthetic bars, no network)."""

import unittest
from datetime import date, timedelta

from backend.decision.config import exit_rule_for, get_config
from backend.signals import wolfe

# close path vertices: (bar index, close)
WAVE = [(0, 120), (40, 100.5), (50, 109.5), (60, 95.5), (70, 102.5), (80, 90.0), (84, 93.0)]


def make_bars(vertices=WAVE):
    closes = []
    for (i0, c0), (i1, c1) in zip(vertices, vertices[1:]):
        for i in range(i0, i1):
            closes.append(c0 + (c1 - c0) * (i - i0) / (i1 - i0))
    closes.append(vertices[-1][1])
    start = date(2025, 1, 1)
    return [{"ts": (start + timedelta(days=i)).isoformat() + "T00:00:00", "open": c, "high": c + 0.5,
             "low": c - 0.5, "close": c, "volume": 1_000_000} for i, c in enumerate(closes)]


class WolfeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = wolfe.settings()

    def test_detects_on_the_day_point_5_is_confirmed(self):
        bars = make_bars()[:83]                 # point 5 at 80, confirmed by bar 82
        p = wolfe.detect(bars, self.cfg)
        self.assertTrue(p["found"], p.get("reason"))
        self.assertEqual([q["index"] for q in p["points"]], [40, 50, 60, 70, 80])
        self.assertTrue(p["converging"])
        self.assertAlmostEqual(p["time_ratio"], 1.0)
        self.assertGreater(p["target_line14"], bars[-1]["close"])

    def test_not_found_a_bar_early_or_late(self):
        bars = make_bars()
        self.assertFalse(wolfe.detect(bars[:82], self.cfg)["found"])
        self.assertFalse(wolfe.detect(bars[:84], self.cfg)["found"])

    def test_candidate_levels(self):
        bars = make_bars()[:83]
        c = wolfe.candidate(bars, self.cfg)
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertEqual(c["direction"], "long")
        self.assertLess(c["stop"], c["pattern"]["points"][4]["price"])
        self.assertGreaterEqual(c["rr"], self.cfg["min_rr"])
        self.assertEqual(c["score"], sum(c["factors"].values()))

    def test_rejects_diverging_lines(self):
        # 2-4 line flatter than 1-3: an expanding pattern, not a wedge
        bars = make_bars([(0, 120), (40, 100.5), (50, 104.5), (60, 95.5), (70, 103.5), (80, 89.0), (84, 92.0)])[:83]
        p = wolfe.detect(bars, self.cfg)
        self.assertFalse(p["found"])

    def test_rejects_asymmetric_waves(self):
        bars = make_bars([(0, 120), (40, 100.5), (45, 109.5), (50, 95.5), (80, 102.5), (110, 85.0), (114, 88.0)])[:113]
        p = wolfe.detect(bars, self.cfg)
        self.assertFalse(p["found"])
        self.assertIn("symmetric", p["reason"])

    def test_rejects_target_too_close(self):
        cfg = {**self.cfg, "min_rr": 50.0}
        c = wolfe.candidate(make_bars()[:83], cfg)
        self.assertFalse(c["valid"])
        self.assertIn("1–4 line", c["reject_reason"])

    def test_find_all_is_point_in_time(self):
        found = wolfe.find_all(make_bars(), self.cfg)
        self.assertEqual([c["s"] for c in found], [82])

    def test_own_exit_rule(self):
        self.assertEqual(exit_rule_for("wolfe")["mode"], "breakeven_trail")
        self.assertEqual(exit_rule_for(None), get_config().global_.exit_management.model_dump())
        # cash swing has its own trailing exit too since 23 Nov 2026 (no time stop)
        self.assertEqual(exit_rule_for("cash_swing")["mode"], "breakeven_trail")
        self.assertEqual(exit_rule_for("not_an_instrument"), get_config().global_.exit_management.model_dump())

    def test_registered_as_cash_book(self):
        from backend.journal import panel, postmortem, tracker
        for mod in (tracker, panel, postmortem):
            self.assertIn("wolfe", mod.CASH_SEGMENTS)
        from backend.backtest.engine import SEGMENTS
        self.assertIn("wolfe", SEGMENTS)


if __name__ == "__main__":
    unittest.main()


class WolfeScanNiftyVolumeTests(unittest.TestCase):
    def test_nifty_index_bars_without_volume_do_not_block(self):
        """Kite returns volume 0 for indices; the Wolfe scan must not need it (23 Nov 2026: it blocked)."""
        import inspect
        src = inspect.getsource(wolfe.run_scan)
        self.assertIn("data_quality(nifty_bars, signal_date, need_volume=False)", src)
