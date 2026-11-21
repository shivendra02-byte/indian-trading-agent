"""Range trades for sideways markets: range targets, 1.5R minimum, the stock's own regime."""

import math
import unittest

from tests.test_signals import make_bars


def range_bars():
    """A clean 95–105 range (period 8) whose last bar is a rejection candle at support."""
    closes = [100 - 5 * math.cos(2 * math.pi * (i - 299) / 8) for i in range(300)]
    bars = make_bars(closes, wiggle=0.0)
    prev = closes[0]
    for b, c in zip(bars, closes):
        o = (prev + c) / 2
        b.update(open=o, high=max(o, c) + 0.3, low=min(o, c) - 0.3)
        prev = c
    bars[-1].update(open=95.0, low=94.8, high=95.9, close=95.7)
    return bars


def range_candidate(rule_set, regime="SIDEWAYS", stock_regime="SIDEWAYS", **overrides):
    from backend.decision.config import available_rule_sets, get_config, use_config
    from backend.marketdata.candles import resample_weekly
    from backend.signals.setups import evaluate
    bars = range_bars()
    with use_config(available_rule_sets()[rule_set]):
        st = get_config().strategy("range_reversal")
        saved = dict(st.model_extra)
        st.model_extra.update(overrides)
        try:
            cands = evaluate(bars, resample_weekly(bars), regime, directions=("long",), stock_regime=stock_regime)
        finally:
            st.model_extra.clear()
            st.model_extra.update(saved)
    return next(c for c in cands if c["setup"] == "range_reversal")


class SidewaysRuleSetTests(unittest.TestCase):
    def test_target_is_the_opposite_side_of_the_range(self):
        c = range_candidate("sideways_v1")
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertAlmostEqual(c["next_level"], 105.3, places=1)
        self.assertLess(c["target"], 105.3)                 # just inside the range edge
        self.assertGreaterEqual(c["rr"], 1.5)

    def test_middle_target_needs_1_5r(self):
        c = range_candidate("sideways_v1", target="range_middle")
        self.assertFalse(c["trackable"])
        self.assertIn("no room for 1.5R before the middle of the range", c["reject_reason"])

    def test_stock_regime_decides_for_range_trades(self):
        # NIFTY falling hard but the stock itself is ranging: no regime penalty in sideways_v1.
        own = range_candidate("sideways_v1", regime="STRONG_DOWN", stock_regime="SIDEWAYS")
        self.assertTrue(own["valid"])
        self.assertEqual(own["setup_regime"], "SIDEWAYS")
        self.assertNotIn("required_regime", own.get("penalties") or {})
        # The live rules still judge on NIFTY and reject outside a sideways market.
        live = range_candidate("current", regime="STRONG_DOWN", stock_regime="SIDEWAYS")
        self.assertFalse(live["trackable"])
        self.assertIn("requires a SIDEWAYS regime", live["reject_reason"])
        # Stock trending in sideways_v1: scored, with the required-regime penalty.
        trending = range_candidate("sideways_v1", regime="SIDEWAYS", stock_regime="STRONG_UP")
        self.assertEqual(trending["penalties"].get("required_regime"), 15)

    def test_regime_at_matches_current_regime(self):
        from backend.signals.regime import current_regime, label_series, regime_at
        from tests.test_signals import uptrend
        bars = make_bars(uptrend(320))
        labels = label_series(bars)
        for i in (210, 260, 319):
            self.assertEqual(regime_at(labels, i), current_regime(bars[:i + 1])["regime"], i)


if __name__ == "__main__":
    unittest.main()
