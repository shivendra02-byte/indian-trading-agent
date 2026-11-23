"""Futures/MCX: no 3-day time stop; exit at target, stop or breakeven -> trailing stop (agreed 23 Nov 2026)."""

import unittest
from datetime import date

from backend.decision.config import get_config
from backend.journal.panel import _exit_line
from backend.journal.simulate import simulate
from backend.signals.fno import last_session_before


class NoTimeStopTest(unittest.TestCase):
    def test_config_per_instrument(self):
        cfg = get_config()
        for name in ("stock_futures", "index_futures", "mcx", "cash_swing", "cup_handle"):
            self.assertFalse(cfg.instrument(name).time_stop, name)
            self.assertEqual(cfg.exit_rule(name)["mode"], "breakeven_trail", name)
            self.assertEqual(cfg.exit_rule(name)["breakeven_at_r"], 1.0)
        self.assertEqual(cfg.instrument("cash_swing").max_hold_trading_days, 60)
        self.assertEqual(cfg.instrument("cup_handle").max_hold_trading_days, 120)

    def test_last_session_before_expiry_skips_holidays(self):
        # 24 Nov 2026 is an NSE holiday (Guru Nanak Jayanti): expiry on the 25th -> exit by the 23rd.
        self.assertEqual(last_session_before("NSE", date(2026, 11, 25), date(2026, 11, 20)), date(2026, 11, 23))
        self.assertEqual(last_session_before("NSE", date(2026, 11, 21), date(2026, 11, 20)), date(2026, 11, 20))

    def test_trade_runs_past_three_days_and_exits_on_trailing_stop(self):
        # Long 100, stop 95 (risk 5), target 120 (4R). It rallies for days, never hits the target,
        # then falls back: the old rule would have closed it on day 3; now the trail closes it.
        bars, price = [], 100.0
        days = [f"2026-12-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
        path = [101, 104, 107, 110, 112, 113, 108, 104]
        for d, close in zip(days, path):
            bars.append({"ts": f"{d}T15:15:00", "open": price, "high": max(price, close) + 0.5,
                         "low": min(price, close) - 0.5, "close": close, "eod": True})
            price = close
        out = simulate(direction="long", entry=100.0, stop=95.0, target=120.0, atr=2.0, bars=bars,
                       fill_until="2026-12-01T23:59:59", time_stop_date="2026-12-28",
                       exit_rule=get_config().exit_rule("stock_futures"))
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["exit_reason"], "trail_stop")
        self.assertGreater(out["exit_ts"][:10], "2026-12-03")
        self.assertGreater(out["pnl_r"], 1.0)

    def test_alert_says_no_time_stop(self):
        line = _exit_line({"segment": "mcx", "time_stop_date": "2026-12-18"})
        self.assertIn("no time stop", line)
        self.assertIn("breakeven at 1R", line)
        cash = _exit_line({"segment": "cash_swing", "time_stop_date": "2027-02-19"})
        self.assertIn("no time stop", cash)
        self.assertIn("safety exit after 60 trading days", cash)


if __name__ == "__main__":
    unittest.main()
