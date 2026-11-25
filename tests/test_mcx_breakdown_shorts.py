"""MCX breakdown shorts (agreed 25 Nov 2026): breakout shorts allowed in HIGH_VOL for MCX,
and a swing stop wider than 3 ATR placed at 3 ATR instead of rejecting the trade."""

import math
import unittest

from tests.test_signals import make_bars

BD = {"enabled": True, "extra_regimes": ["HIGH_VOL"], "cap_stop_atr": 3.0}


def breakdown_bars(drop=0.035, volume_mult=2.5):
    closes = [100 * (1 - 0.004) ** i * (1 + 0.01 * math.sin(i / 3)) for i in range(260)]
    base = closes[-1]
    closes += [base * (1 + 0.004 * math.sin(i)) for i in range(25)]   # tight consolidation
    closes.append(base * (1 - drop))                                  # wide-range breakdown close
    bars = make_bars(closes, wiggle=0.004)
    bars[-1].update(open=base, high=base * 1.001, low=base * (1 - drop - 0.001),
                    volume=int(1_000_000 * volume_mult))
    return bars


def short_breakout(cands):
    return next(c for c in cands if c["setup"] == "breakout" and c["direction"] == "short")


class BreakdownShortTests(unittest.TestCase):
    def setUp(self):
        from backend.marketdata.candles import resample_weekly
        self.bars = breakdown_bars()
        self.htf = resample_weekly(self.bars)

    def run_eval(self, regime, **kw):
        from backend.signals.setups import evaluate
        return short_breakout(evaluate(self.bars, self.htf, regime, directions=("long", "short"),
                                       is_stock=False, **kw))

    def test_rejected_without_the_mcx_rule(self):
        c = self.run_eval("HIGH_VOL")
        self.assertFalse(c["valid"])
        self.assertIn("stop too wide", c["reject_reason"])

    def test_capped_stop_and_allowed_in_high_vol(self):
        c = self.run_eval("HIGH_VOL", breakdown_shorts=BD)
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertAlmostEqual(c["stop_atr"], 3.0, places=2)
        self.assertGreater(c["stop"], c["entry"])
        self.assertIn("capped at 3 ATR", c["stop_type"])
        self.assertGreaterEqual(c["rr"], 2.0)

    def test_strong_down_gets_the_cap_too(self):
        self.assertIn("stop too wide", self.run_eval("STRONG_DOWN")["reject_reason"])
        self.assertTrue(self.run_eval("STRONG_DOWN", breakdown_shorts=BD)["valid"])

    def test_still_needs_volume_and_other_regimes_stay_closed(self):
        from backend.marketdata.candles import resample_weekly
        from backend.signals.setups import evaluate
        weak = breakdown_bars(volume_mult=1.1)
        c = short_breakout(evaluate(weak, resample_weekly(weak), "HIGH_VOL", is_stock=False,
                                    breakdown_shorts=BD))
        self.assertFalse(c["valid"])
        self.assertIn("volume", c["reject_reason"])
        self.assertFalse(self.run_eval("UNSTABLE", breakdown_shorts=BD)["valid"])

    def test_tight_swing_stop_is_not_changed(self):
        from backend.signals.setups import _context, geometry
        bars = breakdown_bars(drop=0.012)
        ctx = _context(bars, "breakout")
        self.assertEqual(geometry(bars, ctx, -1)["stop"], geometry(bars, ctx, -1, cap_stop_atr=3.0)["stop"])

    def test_live_config_enables_it_for_mcx_only(self):
        from backend.signals.fno import breakdown_shorts
        self.assertEqual(breakdown_shorts("mcx")["cap_stop_atr"], 3.0)
        self.assertIsNone(breakdown_shorts("stock_futures"))
        self.assertIsNone(breakdown_shorts("index_futures"))


if __name__ == "__main__":
    unittest.main()
