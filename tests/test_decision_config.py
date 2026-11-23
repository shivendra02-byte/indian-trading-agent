"""Decision engine configuration: validation, safety switches, versioning, effect on decisions."""

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from backend.decision.config import DEFAULT_PATH, get_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("TRADINGAGENTS_DECISION_CONFIG")
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TRADINGAGENTS_DECISION_CONFIG", None)
        else:
            os.environ["TRADINGAGENTS_DECISION_CONFIG"] = self._old
        self.tmp.cleanup()

    def write(self, mutate) -> Path:
        data = yaml.safe_load(DEFAULT_PATH.read_text())
        mutate(data)
        path = Path(self.tmp.name) / f"cfg{len(os.listdir(self.tmp.name))}.yaml"
        path.write_text(yaml.safe_dump(data))
        os.environ["TRADINGAGENTS_DECISION_CONFIG"] = str(path)
        return path

    def test_default_file_loads_with_agreed_values(self):
        cfg = get_config()
        g = cfg.global_
        self.assertEqual((g.risk.risk_per_trade_pct, g.risk.max_open_positions, g.risk.max_open_risk_pct),
                         (1.0, 10, 10.0))   # raised from 3 / 3% on 22 Nov 2026
        self.assertEqual((g.limits.max_new_trades_day, g.limits.max_new_trades_week, g.limits.max_ai_evaluations_day),
                         (5, 15, 7))   # 7 AI reviews total since 23 Nov 2026 (per-book shares)
        self.assertEqual((g.scoring.min_shortlist_score, g.scoring.min_rr), (60, 2.0))
        self.assertFalse(g.system.live_order_execution)
        self.assertFalse(cfg.instrument("stock_options").enabled)
        self.assertEqual(cfg.instrument("stock_futures").confirmation_tfs, ["2h", "4h"])
        self.assertEqual(cfg.instrument("index_options").confirmation_tfs, ["30m", "1h"])
        self.assertEqual(cfg.instrument("cash_swing").shortlist_per_scan, 2)
        self.assertEqual([cfg.strategy(s).volume_multiplier for s in ("pullback", "breakout", "range_reversal")],
                         [1.0, 1.5, 1.2])
        self.assertRegex(cfg.rule_version, r"^v1-[0-9a-f]{8}$")

    def test_safety_switches_cannot_be_turned_on(self):
        for key in ("live_order_execution", "averaging_down", "forced_trades"):
            self.write(lambda d, k=key: d["global"]["system"].__setitem__(k, True))
            with self.assertRaises(Exception, msg=key):
                get_config()

    def test_invalid_values_are_rejected(self):
        self.write(lambda d: d["global"]["scoring"]["weights"].__setitem__("htf_trend", 30))  # sum 105
        with self.assertRaises(Exception):
            get_config()
        self.write(lambda d: d["strategies"]["pullback"].__setitem__("ema_fast", 60))  # fast >= slow
        with self.assertRaises(Exception):
            get_config()
        self.write(lambda d: d["global"]["risk"].__setitem__("unknown_knob", 1))      # typo guard
        with self.assertRaises(Exception):
            get_config()

    def test_changed_file_changes_version_and_decisions(self):
        base = get_config().rule_version
        self.write(lambda d: d["global"]["scoring"].__setitem__("min_shortlist_score", 75))
        cfg = get_config()
        self.assertNotEqual(cfg.rule_version, base)
        from backend.signals.setups import min_score
        self.assertEqual(min_score(), 75)


class ComparableVolumeTests(unittest.TestCase):
    def test_intraday_volume_compares_same_time_slot(self):
        from backend.signals.setups import volume_ratio
        bars = []
        for day in range(1, 8):
            for slot, vol in (("09:15", 1000), ("10:15", 200), ("11:15", 200)):
                bars.append({"ts": f"2026-11-0{day}T{slot}:00", "volume": vol})
        bars.append({"ts": "2026-11-09T09:15:00", "volume": 1000})
        # Same-slot average is 1000 -> ratio 1.0 (a naive 20-bar average would say ~2.2x).
        self.assertAlmostEqual(volume_ratio(bars, 20, intraday=True), 1.0)


if __name__ == "__main__":
    unittest.main()
