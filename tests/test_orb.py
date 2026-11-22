"""Opening range breakout: signal rules, exits, the live scan, tracking and the backtest."""

import unittest
from datetime import date, datetime, timedelta

from tests._support import IsolatedStateTestCase
from tests.test_fno_scan import FakeQuoteKite
from tests.test_signals import make_bars

STARTS = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 10)
          if "09:15" <= f"{h:02d}:{m:02d}" <= "15:20"]
STARTS = ["09:15", "09:25", "09:35", "09:45", "09:55"] + [s for s in
          (f"{h:02d}:{m:02d}" for h in range(10, 16) for m in (5, 15, 25, 35, 45, 55)) if s <= "15:25"]


def day_bars(day: date, closes, opening=(100.0, 101.0), volume=1000, last_volume=None):
    """10-minute bars: three opening-range bars between ``opening`` low/high, then ``closes``."""
    lo, hi = opening
    out = []
    for i, ts in enumerate(STARTS[:3]):
        mid = (lo + hi) / 2
        out.append({"ts": f"{day.isoformat()}T{ts}:00", "open": mid, "high": hi if i == 1 else mid + 0.1,
                    "low": lo if i == 2 else mid - 0.1, "close": mid, "volume": volume})
    prev = (lo + hi) / 2
    for ts, c in zip(STARTS[3:], closes):
        out.append({"ts": f"{day.isoformat()}T{ts}:00", "open": prev, "high": max(prev, c) + 0.05,
                    "low": min(prev, c) - 0.05, "close": c, "volume": volume})
        prev = c
    if last_volume is not None:
        out[-1]["volume"] = last_volume
    return out


def daily_atr3(end=date(2026, 11, 20), n=60, price=100.0):
    bars = make_bars([price] * n, end=end, volume=50_000_000)
    for b in bars:
        b.update(high=price + 1.5, low=price - 1.5)
    return bars


class EvaluateOrbTests(unittest.TestCase):
    def test_long_breakout_with_range_stop_and_2r_target(self):
        from backend.signals.orb import evaluate_orb
        today = date(2026, 11, 23)
        bars = day_bars(today, [100.6, 101.4])   # 09:45 inside, 09:55 closes above 101
        c = evaluate_orb(bars, daily_atr3())
        self.assertTrue(c["trackable"], c.get("reject_reason"))
        self.assertEqual((c["direction"], c["stop_type"], c["stop"]), ("long", "range", 100.0))
        self.assertAlmostEqual(c["target"], round(101.4 + 2 * 1.4, 2))

    def test_wide_range_uses_midpoint_stop(self):
        from backend.signals.orb import evaluate_orb
        bars = day_bars(date(2026, 11, 23), [101.0, 103.3], opening=(100.0, 103.0))   # 1 ATR wide
        c = evaluate_orb(bars, daily_atr3())
        self.assertEqual((c["stop_type"], c["stop"]), ("midpoint", 101.5))

    def test_rejections(self):
        from backend.signals.orb import evaluate_orb
        today = date(2026, 11, 23)
        inside = evaluate_orb(day_bars(today, [100.5, 100.7]), daily_atr3())
        self.assertEqual(inside["reject_reason"], "no close outside the opening range")
        second = evaluate_orb(day_bars(today, [101.3, 100.8, 101.4]), daily_atr3())
        self.assertIn("first breakout", second["reject_reason"])
        gap = day_bars(today, [100.6, 101.4])
        gap[0]["open"] = 104.0
        self.assertIn("gap", evaluate_orb(gap, daily_atr3())["reject_reason"])
        narrow = evaluate_orb(day_bars(today, [100.4], opening=(100.0, 100.1)), daily_atr3())
        self.assertIn("ATR outside", narrow["reject_reason"])

    def test_short_and_volume_scoring(self):
        from backend.signals.orb import evaluate_orb
        today = date(2026, 11, 23)
        prev = [b for k in range(1, 6) for b in day_bars(today - timedelta(days=7 * k), [100.5, 100.5])]
        quiet = evaluate_orb(day_bars(today, [100.4, 99.6], last_volume=1000), daily_atr3(), prev_bars=prev)
        loud = evaluate_orb(day_bars(today, [100.4, 99.6], last_volume=3000), daily_atr3(), prev_bars=prev)
        self.assertEqual(loud["direction"], "short")
        self.assertFalse(quiet["valid"])                      # strict mode: volume is a soft check
        self.assertTrue(loud["valid"])
        self.assertGreater(loud["score"], quiet["score"])

    def test_mark_exit_stops_at_exit_time(self):
        from backend.signals.orb import mark_exit
        bars = [{"ts": f"2026-11-23T{t}:00"} for t in ("15:05", "15:15", "15:25")]
        out = mark_exit(bars, "15:15", 10)
        self.assertEqual([b["ts"][11:16] for b in out], ["15:05"])   # the 15:05–15:15 bar is the exit bar
        self.assertEqual([b["eod"] for b in out], [True])


class OrbScanTests(IsolatedStateTestCase, unittest.TestCase):
    TODAY = date(2026, 11, 23)

    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from backend.marketdata.instruments import refresh_ban_list
        from backend.signals import events
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-23"),
                (501, "NSE", "NSE", "LIQ", "LIQ", "EQ", None, 0, 1, 0.05, "2026-11-23"),
                (601, "NFO", "NFO-FUT", "LIQ26DECFUT", "LIQ", "FUT", "2026-12-29", 0, 500, 0.05, "2026-11-23")])
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in daily_atr3()])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in daily_atr3(price=20000)])
        future_bars = day_bars(self.TODAY, [100.6, 101.4], last_volume=3000)
        later = [{"ts": f"2026-11-23T{t}:00", "open": 101.4, "high": 101.6, "low": 101.2, "close": 101.5,
                  "volume": 1000} for t in STARTS[5:]]
        self.later = later
        save_candles(601, "10minute", [{**b, "date": b["ts"]} for b in future_bars])
        save_candles(256265, "10minute", [{**b, "date": b["ts"], "open": 20000, "close": 20050,
                                           "high": 20060, "low": 19990} for b in future_bars])
        refresh_ban_list(fetch=lambda: "Securities in Ban For Trade Date 23-NOV-2026:\n")
        events.refresh_results(["LIQ"], fetch=lambda s: date(2027, 1, 20), today=self.TODAY)

    def test_scan_journals_one_orb_per_day_and_tracks_it(self):
        from backend.journal.store import list_records
        from backend.journal.tracker import track_open_records
        from backend.marketdata.candles import save_candles
        from backend.signals.orb import run_orb_scan
        now = datetime(2026, 11, 23, 10, 8)              # 09:55–10:05 bar closed
        out = run_orb_scan(now=now, client=FakeQuoteKite(spread=0.05))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["journal_records"], 1, out)
        rec = list_records("setup = 'orb'")[0]
        self.assertEqual((rec["segment"], rec["direction"], rec["time_stop_date"]),
                         ("stock_futures", "long", "2026-11-23"))
        self.assertEqual(rec["extra"]["bar_interval"], "10minute")
        again = run_orb_scan(now=datetime(2026, 11, 23, 10, 18), client=FakeQuoteKite(spread=0.05), force=True)
        self.assertEqual(again.get("journal_records", 0), 0)    # one ORB per stock per day
        # Price holds above entry all day: filled, then closed at the 15:15 exit.
        save_candles(601, "10minute", [{**b, "date": b["ts"]} for b in self.later])
        dip = {"ts": "2026-11-23T10:05:00", "open": 101.4, "high": 101.5, "low": 101.3, "close": 101.4,
               "volume": 900}
        save_candles(601, "10minute", [{**dip, "date": dip["ts"]}])
        track_open_records()
        rec = list_records("setup = 'orb'")[0]
        self.assertEqual(rec["track_status"], "closed", rec)
        self.assertEqual(rec["exit_reason"], "time_stop")
        self.assertTrue(rec["exit_ts"].startswith("2026-11-23T15:05"))   # bar ending 15:15


class OrbBacktestTests(IsolatedStateTestCase, unittest.TestCase):
    def test_orb_backtest_first_breakout_per_day(self):
        from backend.backtest import engine
        from backend.decision.config import available_rule_sets
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from tradingagents.utils.market_calendar import is_trading_day
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "LIQ", "LIQ", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "LIQ99DECFUT", "LIQ", "FUT", "2099-12-31", 0, 500, 0.05, "2026-11-20")])
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in daily_atr3(n=400)])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in daily_atr3(n=400, price=20000)])
        rows, d = [], date(2026, 10, 1)
        while d <= date(2026, 10, 30):
            if is_trading_day(d):
                bars = day_bars(d, [100.6, 101.4, 101.8, 102.6] + [104.5] * 30)   # breakout then target
                rows += bars
            d += timedelta(days=1)
        save_candles(501, "10minute", [{**b, "date": b["ts"]} for b in rows])
        engine.ensure_tables()
        with self.db.get_db() as conn:
            run_id = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status) "
                                  "VALUES ('orb_proxy', 'current', '2026-10-01', '2026-10-30', 'running')").lastrowid
        summary = engine.run_backtest(run_id, "orb_proxy", available_rule_sets()["current"],
                                      date(2026, 10, 1), date(2026, 10, 30))
        trades = engine.get_run(run_id, with_trades=True)["trades"]
        days = {t["signal_ts"][:10] for t in trades}
        self.assertEqual(len(days), len(trades))                    # one per stock per day
        self.assertTrue(trades)
        self.assertTrue(all(t["signal_ts"][11:16] == "09:55" for t in trades))
        self.assertTrue(all(t["exit_reason"] == "target" for t in trades if t["status"] == "closed"))
        self.assertIn("orb long", summary["by_setup"])


if __name__ == "__main__":
    unittest.main()
