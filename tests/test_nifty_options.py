"""NIFTY option buying on 5-minute bars: setups, 15-minute filter, stop/target, scheduler, backtest."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from backend.signals import nifty_options as no


def day_bars(day: str, closes: list[float], start=(9, 15)) -> list[dict]:
    t = datetime.fromisoformat(f"{day}T{start[0]:02d}:{start[1]:02d}:00")
    out, prev = [], closes[0]
    for c in closes:
        o = prev
        out.append({"ts": t.isoformat(timespec="seconds"), "open": o, "high": max(o, c) + 2, "low": min(o, c) - 2,
                    "close": c, "volume": 0})
        prev, t = c, t + timedelta(minutes=5)
    return out


def trending_history(days=("2026-11-18", "2026-11-19", "2026-11-20"), slope=1.0, start=24000.0):
    bars, level = [], start
    for d in days:
        closes = [level + slope * i for i in range(75)]
        bars += day_bars(d, closes)
        level = closes[-1]
    return bars, level


CFG = {**no.settings(), "setups": list(no.SETUPS)}


class SetupTests(unittest.TestCase):
    def test_orb_first_close_beyond_range(self):
        hist, level = trending_history()
        today = day_bars("2026-11-23", [level, level + 3, level, level + 2, level + 3, level + 6])
        bars = hist + today
        s, why = no._setup_direction("opt_orb", bars, today, CFG)
        self.assertEqual(s, 1, why)
        later = today + day_bars("2026-11-23", [level + 9], start=(9, 45))
        self.assertEqual(no._setup_direction("opt_orb", hist + later, later, CFG)[0], 0)   # only the first close

    def test_vwap_ema_break(self):
        hist, level = trending_history()
        closes = [level + i for i in range(10)] + [level + 9, level + 8, level + 9, level + 15]
        today = day_bars("2026-11-23", closes)
        s, why = no._setup_direction("opt_vwap_ema", hist + today, today, CFG)
        self.assertEqual(s, 1, why)

    def test_supertrend_flip(self):
        hist, level = trending_history(slope=-1.0)
        today = day_bars("2026-11-23", [level + 40 * i for i in range(1, 6)])
        bars = hist + today
        dirs = no.supertrend_dirs(bars, 10, 3.0)
        flips = [i for i in range(1, len(dirs)) if dirs[i] and dirs[i - 1] and dirs[i] != dirs[i - 1]]
        self.assertTrue(flips and dirs[flips[-1]] == 1)

    def test_15m_trend_filter_and_levels(self):
        hist, level = trending_history()
        today = day_bars("2026-11-23", [level, level + 3, level, level + 2, level + 3, level + 6])
        rows = {r["setup"]: r for r in no.candidates(hist + today, {**CFG, "setups": ["opt_orb"]})}
        r = rows["opt_orb"]
        self.assertTrue(r["trackable"], r.get("reject_reason"))
        self.assertEqual(r["direction"], "long")
        self.assertLess(r["stop"], r["entry"])
        self.assertAlmostEqual(r["target"] - r["entry"], 2 * (r["entry"] - r["stop"]), places=1)
        down, lvl = trending_history(slope=-1.0)
        t2 = day_bars("2026-11-23", [lvl, lvl + 3, lvl, lvl + 2, lvl + 3, lvl + 6])
        r2 = no.candidates(down + t2, {**CFG, "setups": ["opt_orb"]})[0]
        self.assertFalse(r2["trackable"])
        self.assertEqual(r2["reject_reason"], "15-minute trend does not agree")

    def test_15m_bars_only_completed(self):
        bars = day_bars("2026-11-23", [100, 101, 102, 103, 104])
        b15 = no.bars_15m(bars, datetime(2026, 11, 23, 9, 40))
        self.assertEqual(len(b15), 1)
        self.assertEqual(b15[0]["close"], 102)


class SchedulerTests(unittest.TestCase):
    def test_5minute_sync_then_scan(self):
        from backend.scheduler import due_jobs
        now = datetime(2026, 11, 23, 10, 32)
        base = {"paused": False, "kite_connected": True, "instruments_fresh": True, "last_morning_attempt": None,
                "done": {}, "scans_done": {}}
        self.assertNotIn("candles:NSE:5minute", due_jobs(now, base))              # off unless enabled
        jobs = due_jobs(now, {**base, "nifty_options": True})
        self.assertIn("candles:NSE:5minute", jobs)
        done = {"candles:NSE:5minute": datetime(2026, 11, 23, 10, 30)}
        self.assertIn("scan:nifty_opt", due_jobs(now, {**base, "nifty_options": True, "done": done}))
        scans = {"nifty_opt": datetime(2026, 11, 23, 10, 30)}
        self.assertNotIn("scan:nifty_opt",
                         due_jobs(now, {**base, "nifty_options": True, "done": done, "scans_done": scans}))


class BacktestTests(unittest.TestCase):
    def test_runner_finds_signals_and_exits_same_day(self):
        from backend.backtest import engine
        hist, level = trending_history(days=tuple(f"2026-11-{d:02d}" for d in (10, 11, 12, 13, 16, 17, 18)))
        today = day_bars("2026-11-19", [level, level + 3, level, level + 2, level + 3, level + 6]
                         + [level + 6 + 2 * i for i in range(1, 70)])
        bars = hist + today
        with mock.patch.object(engine, "spot_instrument", return_value={"instrument_token": 1}), \
                mock.patch.object(engine, "get_candles", return_value=bars), \
                mock.patch.object(engine, "_market_blocked", return_value=False):
            trades = engine.run_nifty_options(date(2026, 11, 19), date(2026, 11, 19), [], [])
        self.assertTrue(trades)
        self.assertTrue(all(t["signal_ts"].startswith("2026-11-19") for t in trades))
        closed = [t for t in trades if t["status"] == "closed"]
        self.assertTrue(closed)
        self.assertTrue(all(t["exit_ts"][:10] == "2026-11-19" for t in closed))
        per_setup = {}
        for t in trades:
            per_setup[t["setup"]] = per_setup.get(t["setup"], 0) + 1
        self.assertTrue(all(n <= CFG["max_signals_per_setup_day"] for n in per_setup.values()))


if __name__ == "__main__":
    unittest.main()


from tests._support import IsolatedStateTestCase  # noqa: E402


class LiveScanTests(IsolatedStateTestCase, unittest.TestCase):
    def test_scan_journals_shadow_signal_with_atm_option(self):
        from backend.db import get_db
        from backend.journal.store import list_records
        from backend.marketdata.store import ensure_marketdata_tables
        ensure_marketdata_tables()
        hist, level = trending_history()
        today = day_bars("2026-11-23", [level, level + 3, level, level + 2, level + 3, level + 6])
        strike = round((level + 6) / 50) * 50
        with get_db() as conn:
            conn.execute("INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (777, "NFO", "NFO-OPT", f"NIFTY25NOV{strike}CE", "NIFTY", "CE", "2026-11-25",
                          float(strike), 65, 0.05, "2026-11-23"))
        with mock.patch("backend.marketdata.candles.get_candles", return_value=hist + today), \
                mock.patch("backend.marketdata.instruments.spot_instrument", return_value={"instrument_token": 1}):
            result = no.run_scan(now=datetime(2026, 11, 23, 9, 47))
        self.assertEqual(result["status"], "ok", result)
        recs = list_records("segment = 'nifty_options'")
        self.assertTrue(recs)
        orb = [r for r in recs if r["setup"] == "opt_orb"][0]
        self.assertEqual(orb["direction"], "long")
        self.assertEqual(orb["extra"]["tradingsymbol"], f"NIFTY25NOV{strike}CE")
        self.assertEqual(orb["extra"]["bar_interval"], "5minute")
        self.assertEqual(orb["decision"], "shortlisted")
        self.assertIsNone(orb.get("panel_decision"))      # shadow: never sent to the AI
        self.assertIn({"instrument_token": 777, "tradingsymbol": f"NIFTY25NOV{strike}CE"},
                      no.open_option_contracts())
