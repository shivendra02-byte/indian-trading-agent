"""Today-tab regime: engine regime with its numbers; old classifier explains a V-shaped recovery (23 Nov 2026)."""

import unittest
from datetime import date, timedelta
from unittest import mock

import numpy as np
import pandas as pd

from backend.signals.regime import explain_current


def _bars(closes):
    d0 = date(2025, 1, 1)
    return [{"ts": (d0 + timedelta(days=i)).isoformat() + "T00:00:00", "open": c, "high": c * 1.004,
             "low": c * 0.996, "close": c, "volume": 1} for i, c in enumerate(closes)]


class RegimeExplainTest(unittest.TestCase):
    def test_strong_rally_has_numbers(self):
        closes = list(np.linspace(20000, 26000, 260))
        out = explain_current(_bars(closes))
        self.assertIn(out["regime"], ("STRONG_UP", "HIGH_VOL", "UNSTABLE"))
        for k in ("close", "ema_fast", "ema_slow", "adx", "ema_slow_rising", "date"):
            self.assertIn(k, out)
        self.assertTrue(out["ema_slow_rising"])
        self.assertGreater(out["close"], out["ema_fast"])

    def test_short_history_fails_closed(self):
        out = explain_current(_bars([100.0] * 50))
        self.assertEqual(out["regime"], "UNSTABLE")

    def test_old_classifier_names_a_recovery(self):
        from backend import market_regime
        # Long decline, then a sharp ~10% rally: price above both SMAs, 50 SMA still below 200 SMA.
        trend = np.concatenate([np.linspace(26000, 22500, 200), np.linspace(22500, 22400, 40),
                                np.linspace(22400, 25500, 25)])
        noise = np.random.default_rng(7).normal(0, 0.008, len(trend))   # ~0.8% daily moves
        closes = list(trend * (1 + noise))
        idx = pd.date_range(end=pd.Timestamp("2026-11-20"), periods=len(closes), freq="B")
        hist = pd.DataFrame({"Close": closes}, index=idx)
        with mock.patch.object(market_regime.yf, "Ticker") as t:
            t.return_value.history.return_value = hist
            out = market_regime.classify_regime_for_date(date(2026, 11, 20))
        self.assertEqual(out["regime"], "SIDEWAYS", out)
        self.assertGreater(out["nifty_close"], out["sma_200"])
        self.assertLess(out["sma_50"], out["sma_200"])
        self.assertIn("Recovery not yet confirmed", out["reasoning"])


if __name__ == "__main__":
    unittest.main()
