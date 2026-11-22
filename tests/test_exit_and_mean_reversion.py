"""Managed exit (breakeven / trailing after 1R), the sideways mean-reversion setup,
hard regime permissions and the backtest funnel."""

import unittest

from tests.test_journal import PLAN, bar
from tests.test_sideways import range_bars

BE_TRAIL = {"mode": "breakeven_trail", "breakeven_at_r": 1.0, "breakeven_lock_r": 0.0,
            "trail_start_r": 1.0, "trail_atr": 2.0}


class ManagedExitTests(unittest.TestCase):
    # PLAN: long, entry 100, stop 96 (R = 4), target 108, ATR 2.

    def sim(self, bars, exit_rule=None):
        from backend.journal.simulate import simulate
        return simulate(bars=bars, exit_rule=exit_rule, **PLAN)

    def test_breakeven_after_1r_turns_a_loser_into_a_scratch(self):
        bars = [bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                bar("2026-11-24T00:00", 100.5, 104.5, 100.2, 103),     # 1.125R in favour, closes there
                bar("2026-11-25T00:00", 103, 103.5, 95, 95.5)]         # back through the original stop
        fixed = self.sim(bars)
        self.assertEqual((fixed["exit_reason"], fixed["pnl_r"]), ("stop", -1.0))
        managed = self.sim(bars, {"mode": "breakeven", "breakeven_at_r": 1.0})
        self.assertEqual((managed["exit_reason"], managed["pnl_r"]), ("breakeven_stop", 0.0))
        self.assertEqual(managed["exit_mode"], "breakeven")
        self.assertEqual(managed["fixed"]["pnl_r"], -1.0)               # kept for comparison

    def test_stop_moves_only_after_the_bar_closes(self):
        # One bar reaches 1R and falls back below the entry: still the original stop.
        bars = [bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                bar("2026-11-24T00:00", 100.5, 104.5, 99.0, 99.2),
                bar("2026-11-25T00:00", 99.2, 99.5, 95, 95.5)]
        r = self.sim(bars, BE_TRAIL)
        self.assertEqual((r["exit_reason"], r["pnl_r"]), ("stop", -1.0))
        # A later close above the entry arms it.
        bars[2] = bar("2026-11-25T00:00", 99.2, 101, 98.8, 100.8)
        bars.append(bar("2026-11-26T00:00", 100.8, 101, 97, 97.5))
        r = self.sim(bars, BE_TRAIL)
        self.assertEqual((r["exit_reason"], r["pnl_r"]), ("breakeven_stop", 0.0))

    def test_lock_r_moves_the_stop_above_entry(self):
        bars = [bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                bar("2026-11-24T00:00", 100.5, 104.5, 100.6, 101.0),
                bar("2026-11-25T00:00", 101, 101.2, 98, 98.5)]
        r = self.sim(bars, {"mode": "breakeven", "breakeven_at_r": 1.0, "breakeven_lock_r": 0.1})
        self.assertEqual((r["exit_reason"], r["exit_price"], r["pnl_r"]), ("breakeven_stop", 100.4, 0.1))

    def test_trail_locks_in_profit_below_the_target(self):
        bars = [bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                bar("2026-11-24T00:00", 100.5, 105, 100.2, 104.8),
                bar("2026-11-25T00:00", 104.8, 107.6, 104.5, 107.5),   # best close 107.5 → trail 103.5
                bar("2026-11-26T00:00", 107.5, 107.8, 102, 102.5)]
        r = self.sim(bars, BE_TRAIL)
        self.assertEqual((r["exit_reason"], r["exit_price"]), ("trail_stop", 103.5))
        self.assertAlmostEqual(r["pnl_r"], 0.875)
        self.assertEqual(self.sim(bars)["status"], "open")             # fixed: still waiting for 108

    def test_target_still_wins(self):
        bars = [bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                bar("2026-11-24T00:00", 100.5, 104.5, 100.2, 104),
                bar("2026-11-25T00:00", 104, 108.5, 103.5, 108)]
        self.assertEqual(self.sim(bars, BE_TRAIL)["exit_reason"], "target")

    def test_short_side_is_mirrored(self):
        from backend.journal.simulate import simulate
        plan = {**PLAN, "direction": "short", "entry": 100.0, "stop": 104.0, "target": 92.0}
        bars = [bar("2026-11-23T09:15", 99, 100.5, 98.5, 99.5, False),
                bar("2026-11-24T00:00", 99.5, 99.8, 95.5, 97),
                bar("2026-11-25T00:00", 97, 105, 96.5, 104.5)]
        r = simulate(bars=bars, exit_rule={"mode": "breakeven"}, **plan)
        self.assertEqual((r["exit_reason"], r["pnl_r"]), ("breakeven_stop", 0.0))

    def test_config_validation(self):
        from pydantic import ValidationError
        from backend.decision.config import ExitCfg
        self.assertEqual(ExitCfg().mode, "fixed")
        with self.assertRaises(ValidationError):
            ExitCfg(mode="breakeven", breakeven_at_r=1.0, breakeven_lock_r=1.0)
        with self.assertRaises(ValidationError):
            ExitCfg(mode="breakeven_trail", breakeven_at_r=1.5, trail_start_r=1.0)

    def test_live_rules_keep_the_fixed_exit(self):
        from backend.decision.config import available_rule_sets, get_config, use_config
        with use_config(available_rule_sets()["current"]):
            self.assertEqual(get_config().global_.exit_management.mode, "fixed")
        for name in ("be_trail_1r", "sideways_mr_1r"):
            with use_config(available_rule_sets()[name]):
                em = get_config().global_.exit_management
                self.assertEqual((em.mode, em.breakeven_at_r), ("breakeven_trail", 1.0), name)


def mr_bars(poke_low=92.6):
    """The 95–105 range with the previous bar dipping below the lower Bollinger band
    and the signal bar closing back inside with a rejection candle."""
    bars = range_bars()
    bars[-2]["low"] = poke_low
    return bars


def evaluate_with(rule_set, bars, regime="SIDEWAYS", stock_regime="SIDEWAYS", directions=("long",), **overrides):
    from backend.decision.config import available_rule_sets, get_config, use_config
    from backend.marketdata.candles import resample_weekly
    from backend.signals.setups import evaluate
    with use_config(available_rule_sets()[rule_set]):
        extra = get_config().strategy("mean_reversion").model_extra if overrides else None
        saved = dict(extra or {})
        if extra is not None:
            extra.update(overrides)
        try:
            return evaluate(bars, resample_weekly(bars), regime, directions=directions, stock_regime=stock_regime)
        finally:
            if extra is not None:
                extra.clear()
                extra.update(saved)


def pick(cands, setup):
    return next(c for c in cands if c["setup"] == setup)


class MeanReversionTests(unittest.TestCase):
    def test_found_with_stop_beyond_the_probe_and_band_target(self):
        c = pick(evaluate_with("sideways_mr_1r", mr_bars(), target="band_opposite"), "mean_reversion")
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertLess(c["stop"], 92.6)                                # below the probe low
        lower, mid, upper = c["band"]
        self.assertAlmostEqual(c["next_level"], round(upper, 2))
        self.assertGreaterEqual(c["rr"], 1.5)
        self.assertEqual(c["setup_regime"], "SIDEWAYS")

    def test_mean_target_needs_room(self):
        # Here the band middle is only ~1R away (stop below the probe low, 1 ATR minimum).
        c = pick(evaluate_with("sideways_mr_1r", mr_bars()), "mean_reversion")
        self.assertFalse(c["trackable"])
        self.assertIn("no room for 1.5R before the band middle", c["reject_reason"])

    def test_no_signal_without_a_move_outside_the_band(self):
        c = pick(evaluate_with("sideways_mr_1r", range_bars(), target="band_opposite"), "mean_reversion")
        self.assertFalse(c["trackable"])
        self.assertEqual(c["reject_reason"], "no move outside the band")

    def test_never_outside_a_sideways_regime_even_in_scored_mode(self):
        c = pick(evaluate_with("sideways_mr_1r", mr_bars(), stock_regime="STRONG_DOWN", target="band_opposite"),
                 "mean_reversion")
        self.assertFalse(c["trackable"])
        self.assertIn("requires a SIDEWAYS regime", c["reject_reason"])

    def test_not_checked_by_rule_sets_without_it(self):
        self.assertFalse(any(c["setup"] == "mean_reversion" for c in evaluate_with("current", mr_bars())))


class HardSidewaysPermissionTests(unittest.TestCase):
    def breakout(self, rule_set):
        from tests.test_signals import SetupTests
        bars = SetupTests.breakout_bars(None)
        return pick(evaluate_with(rule_set, bars, regime="SIDEWAYS", stock_regime="SIDEWAYS"), "breakout")

    def test_breakout_in_sideways_is_shadow_only(self):
        c = self.breakout("sideways_mr_1r")
        self.assertTrue(c["trackable"])                                 # still tracked for comparison
        self.assertFalse(c["valid"])
        self.assertIn("breakout long not allowed in SIDEWAYS", c["reject_reason"])
        self.assertEqual(next(s for s in c["stages"] if s["stage"] == "regime")["decision"], "fail")

    def test_scored_mode_without_the_hard_rule_still_allows_it(self):
        self.assertTrue(self.breakout("sideways_v1")["valid"])


class FunnelTests(unittest.TestCase):
    def test_reasons_are_grouped_without_numbers(self):
        from backend.backtest.engine import Funnel, _reason_key
        self.assertEqual(_reason_key("no room for 2R before the next level (24123.45)"),
                         "no room for NR before the next level")
        f = Funnel()
        f.candidates([
            {"setup": "pullback", "trackable": False, "reject_reason": "no pullback to the fast EMA"},
            {"setup": "pullback", "trackable": False, "reject_reason": "no pullback to the fast EMA"},
            {"setup": "breakout", "trackable": True, "valid": False,
             "reject_reason": "breakout long not allowed in SIDEWAYS; volume 1.1x < 2.0x"},
            {"setup": "breakout", "trackable": True, "valid": True},
            {"setup": None, "trackable": False, "reject_reason": "only 40 bars (need 60)"},
        ])
        f.regimes["SIDEWAYS"] += 3
        out = f.as_dict()
        self.assertEqual(out["counts"], {"candidates_rejected": 3, "candidates_trackable": 2, "candidates_valid": 1})
        self.assertEqual(out["rejections"]["pullback"], {"no pullback to the fast EMA": 2})
        self.assertEqual(out["rejections"]["all setups"], {"only N bars": 1})
        self.assertEqual(out["rejections"]["breakout (tracked, not valid)"],
                         {"breakout long not allowed in SIDEWAYS": 1, "volume Nx < Nx": 1})
        self.assertEqual(out["regime_days"], {"SIDEWAYS": 3})

    def test_summary_reports_fixed_exit_next_to_managed(self):
        from backend.backtest.engine import block
        rows = [{"pnl_r": 0.0, "fixed_pnl_r": -1.0, "exit_ts": "2026-01-02"},
                {"pnl_r": 2.0, "fixed_pnl_r": 2.0, "exit_ts": "2026-01-03"}]
        st = block(rows)
        self.assertEqual((st["avg_r"], st["fixed_avg_r"]), (1.0, 0.5))


if __name__ == "__main__":
    unittest.main()
