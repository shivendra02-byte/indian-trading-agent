"""Intraday spread trigger backtest (asked 27 Nov 2026)."""

import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from backend.options import mtf_backtest


def five_min(day="2026-11-24", n=75, start=100.0):
    t0 = datetime.fromisoformat(f"{day}T09:15:00")
    return [{"ts": (t0 + timedelta(minutes=5 * i)).isoformat(timespec="seconds"), "open": start + i,
             "high": start + i + 2, "low": start + i - 1, "close": start + i + 1, "volume": 10} for i in range(n)]


def hourly(days, start=100.0):
    out = []
    for d in days:
        for h in range(7):
            ts = datetime.fromisoformat(f"{d}T09:15:00") + timedelta(hours=h)
            out.append({"ts": ts.isoformat(timespec="seconds"), "open": start, "high": start + 5, "low": start - 5,
                        "close": start, "volume": 0})
    return out


class ResampleTests(unittest.TestCase):
    def test_five_minute_to_fifteen(self):
        m15 = mtf_backtest.resample_15(five_min(n=6))
        self.assertEqual([b["ts"][11:16] for b in m15], ["09:15", "09:30"])
        self.assertEqual((m15[0]["open"], m15[0]["close"], m15[0]["high"], m15[0]["low"]), (100, 103, 104, 99))
        self.assertEqual(m15[0]["volume"], 30)

    def test_view_only_completed_bars(self):
        m15 = mtf_backtest.resample_15(five_min(n=12))
        v = mtf_backtest._View(m15, 15)
        self.assertEqual(len(v.upto(datetime(2026, 11, 24, 9, 44), 100)), 1)      # 09:15 bar ends 09:30
        self.assertEqual(len(v.upto(datetime(2026, 11, 24, 9, 45), 100)), 2)
        self.assertEqual(len(v.upto(datetime(2026, 11, 24, 10, 15), 2)), 2)       # keep = last N


class RunTests(unittest.TestCase):
    """The replay plumbing with the market data and chains stubbed: signal → spread → exit."""

    def setUp(self):
        days = ["2026-11-23", "2026-11-24", "2026-11-25"]
        h1 = hourly(days)
        h1[-3]["close"] = 80.0                                   # 25 Nov: a 1H close below the invalidation
        self.data = {"h1": h1, "h4": [], "m15": mtf_backtest.resample_15(
            five_min("2026-11-23") + five_min("2026-11-24") + five_min("2026-11-25")),
            "daily": [{"ts": "2026-11-20T00:00:00", "close": 1}], "m15_source": "5minute resampled"}
        self.calls = []

    def _evaluate(self, regime, h1, h4, m15, now, cfg):
        self.calls.append(cfg["pullback_touch_atr"])
        fire = now.date() == date(2026, 11, 23) and now.time().hour == 10 and cfg["pullback_touch_atr"] == 0.75
        return {"signal": {"direction": "up", "invalid": 90.0, "signal_ts": m15[-1]["ts"]} if fire else None}

    def test_signal_opens_and_invalidation_closes(self):
        from backend.options import spreads as sp

        book = {"symbol": "NIFTY", "signal_date": "2026-11-23", "expiry": "2026-12-01", "strategy": "bull_put",
                "legs": [], "credit": 30.0, "width": 200, "lot_size": 65, "lots": 1, "regime": "STRONG_UP",
                "vix": 12.0, "exit_by": "2026-11-30", "ok": True}
        with mock.patch.object(mtf_backtest, "_bars", return_value=self.data), \
                mock.patch.object(mtf_backtest, "_regime_before", return_value="STRONG_UP"), \
                mock.patch("backend.options.spread_mtf.evaluate", side_effect=self._evaluate), \
                mock.patch("backend.options.store.expiries", return_value=["2026-12-01"]), \
                mock.patch("backend.options.store.chain", return_value=[]), \
                mock.patch("backend.options.store.vix_on", return_value=12.0), \
                mock.patch.object(sp, "candidate", return_value=book), \
                mock.patch.object(sp, "value", return_value=20.0), \
                mock.patch.object(sp, "exit_check", return_value=None), \
                mock.patch("backend.options.spread_research.cost_points", return_value=1.0):
            res = mtf_backtest.run(date(2026, 11, 23), date(2026, 11, 25), symbols=["NIFTY"],
                                   variants=[{"name": "live", "pullback_touch_atr": 0.25, "pullback_bars": 6},
                                             {"name": "wide", "pullback_touch_atr": 0.75, "pullback_bars": 6}])
        live, wide = res["variants"]
        self.assertEqual((live["signal_days"], live["all"]["trades"]), (0, 0))
        self.assertEqual((wide["signal_days"], wide["all"]["trades"]), (1, 1))
        t = wide["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_date"]), ("invalidated", "2026-11-25"))
        self.assertEqual(t["pnl_r"], round((30 - 20 - 1) / 30, 3))   # credit 30, bought back at 20, 1 point costs
        self.assertIn(0.25, self.calls)
        self.assertIn(0.75, self.calls)


if __name__ == "__main__":
    unittest.main()
