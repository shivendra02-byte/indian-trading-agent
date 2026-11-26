"""The 1.5 ATR trailing-stop experiment (asked 26 Nov 2026): same rules as live, tighter trail."""

import unittest

from backend.decision.config import available_rule_sets, exit_rule_for, get_config, use_config

BOOKS = ("stock_futures", "index_futures", "mcx", "cash_swing", "cup_handle", "cash_reversal", "vcp",
         "nifty_options")


class TrailExperimentTests(unittest.TestCase):
    def test_only_the_trail_distance_changes(self):
        sets = available_rule_sets()
        self.assertIn("trail_1_5_atr", sets)
        live = {b: exit_rule_for(b) for b in BOOKS}
        with use_config(sets["trail_1_5_atr"]):
            for b in BOOKS:
                rule = exit_rule_for(b)
                self.assertEqual(rule["trail_atr"], 1.5, b)
                self.assertEqual({k: v for k, v in rule.items() if k != "trail_atr"},
                                 {k: v for k, v in live[b].items() if k != "trail_atr"}, b)
            g = get_config().global_
            self.assertEqual(g.my_trades.trail_atr, 2.0)          # your own trades keep their setting
        for b in BOOKS:
            self.assertEqual(exit_rule_for(b)["trail_atr"], 2.0, b)   # live unchanged


if __name__ == "__main__":
    unittest.main()
