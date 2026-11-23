"""Fibonacci pullback after a break of structure, and the change-of-character flip (synthetic bars)."""

import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.decision.config import use_config
from backend.signals import setups

ROOT = Path(__file__).resolve().parents[1]
FIB = ROOT / "config/experiments/fib_pullback.yaml"
CHOCH = ROOT / "config/experiments/fib_choch.yaml"

# close path: rise, swing high 120 at 60, dip 112 at 66, impulse to 132 at 80, pull back to ~119.5
UP = [(0, 90), (60, 120), (66, 112), (80, 132), (86, 119.5)]


def path(vertices):
    closes = []
    for (i0, c0), (i1, c1) in zip(vertices, vertices[1:]):
        closes += [c0 + (c1 - c0) * (i - i0) / (i1 - i0) for i in range(i0, i1)]
    closes.append(vertices[-1][1])
    start, bars, prev = date(2025, 1, 1), [], closes[0]
    for i, c in enumerate(closes):
        o = (prev + c) / 2          # open halfway, so each vertex is a strict fractal swing
        bars.append({"ts": (start + timedelta(days=i)).isoformat() + "T00:00:00", "open": o,
                     "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": c, "volume": 1000})
        prev = c
    return bars


def bar(prev_close, o, h, l, c, i):
    return {"ts": (date(2025, 1, 1) + timedelta(days=i)).isoformat() + "T00:00:00",
            "open": o, "high": h, "low": l, "close": c, "volume": 1000}


def run(fn, bars, s, rule_set=FIB):
    with use_config(rule_set):
        name = "fib_pullback" if fn is setups._fib_pullback else "choch_flip"
        ctx = setups._context(bars, name)
        return fn(bars, ctx, s), ctx


class FibPullbackTests(unittest.TestCase):
    def setUp(self):
        self.bars = path(UP)
        n = len(self.bars)
        self.signal = bar(119.5, 119.4, 121.3, 118.6, 121.2, n)   # dips into 60-70%, closes strongly back up

    def test_long_signal_in_zone(self):
        (ok, why, _, soft), ctx = run(setups._fib_pullback, self.bars + [self.signal], 1)
        self.assertTrue(ok, why)
        self.assertEqual(ctx["fib"]["extreme"], round(132 + 0.3, 2))
        self.assertLess(ctx["stop_anchor"], ctx["fib"]["zone"][0])
        self.assertIn("golden_zone", soft)

    def test_needs_a_rejection_candle(self):
        weak = bar(119.5, 119.6, 119.8, 118.6, 119.0, len(self.bars))
        (ok, why, _, _), _ = run(setups._fib_pullback, self.bars + [weak], 1)
        self.assertFalse(ok)
        self.assertEqual(why, "no rejection candle")

    def test_one_entry_per_break(self):
        n = len(self.bars)
        second = bar(121.2, 120.0, 120.9, 118.9, 120.8, n + 1)
        (ok, why, _, _), _ = run(setups._fib_pullback, self.bars + [self.signal, second], 1)
        self.assertFalse(ok)
        self.assertEqual(why, "zone already used for this break")

    def test_not_in_zone_when_shallow(self):
        shallow = path([(0, 90), (60, 120), (66, 112), (80, 132), (84, 127)])
        sig = bar(127, 126.9, 128.5, 126.5, 128.4, len(shallow))
        (ok, why, _, _), _ = run(setups._fib_pullback, shallow + [sig], 1)
        self.assertFalse(ok)
        self.assertEqual(why, "not in the retracement zone")

    def test_fixed_r_target(self):
        with use_config(FIB):
            bars = self.bars + [self.signal]
            ctx = setups._context(bars, "fib_pullback")
            ok, *_ = setups._fib_pullback(bars, ctx, 1)
            geo = setups.geometry(bars, ctx, 1)
        self.assertTrue(ok)
        self.assertTrue(geo["ok"], geo.get("reason"))
        self.assertAlmostEqual(geo["rr"], 2.5)

    def test_not_checked_in_live_rules(self):
        self.assertNotIn("fib_pullback", setups.active_setups())
        with use_config(FIB):
            self.assertIn("fib_pullback", setups.active_setups())
            self.assertNotIn("choch_flip", setups.active_setups())


class ChochFlipTests(unittest.TestCase):
    def test_short_after_failed_bullish_structure(self):
        bars = path(UP + [(88, 121.5), (92, 113.0)])   # bounce from the zone, then down to near the origin
        n = len(bars)
        sig = bar(113.0, 113.0, 113.1, 110.5, 110.8, n)   # first close below the impulse origin (~111.7)
        (ok, why, _, _), ctx = run(setups._choch_flip, bars + [sig], -1, CHOCH)
        self.assertTrue(ok, why)
        self.assertEqual(ctx["fib"]["failed_direction"], "long")
        self.assertGreater(ctx["stop_anchor"], sig["close"])

    def test_no_flip_without_a_close_beyond_origin(self):
        bars = path(UP + [(88, 121.5), (92, 113.0)])
        sig = bar(113.0, 113.0, 113.5, 112.2, 112.4, len(bars))
        (ok, why, _, _), _ = run(setups._choch_flip, bars + [sig], -1, CHOCH)
        self.assertFalse(ok)
        self.assertEqual(why, "no close beyond the impulse origin")


if __name__ == "__main__":
    unittest.main()
