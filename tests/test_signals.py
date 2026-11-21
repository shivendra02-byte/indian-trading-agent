"""Rule engine tests on synthetic bars: indicators, regime, setups, geometry,
event fail-closed behaviour and an end-to-end cash swing scan."""

import math
import unittest
from datetime import date, datetime, timedelta

from tests._support import IsolatedStateTestCase
from tradingagents.utils.market_calendar import is_trading_day


def trading_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def make_bars(closes, end=date(2026, 11, 20), volume=1_000_000, wiggle=0.01):
    days = trading_days(end, len(closes))
    bars, prev = [], closes[0]
    for i, (d, c) in enumerate(zip(days, closes)):
        o = prev
        hi = max(o, c) * (1 + wiggle * (0.5 + 0.5 * math.sin(i)))
        lo = min(o, c) * (1 - wiggle * (0.5 + 0.5 * math.cos(i)))
        bars.append({"ts": f"{d.isoformat()}T00:00:00", "open": o, "high": hi, "low": lo, "close": c,
                     "volume": volume})
        prev = c
    return bars


def uptrend(n=300, start=100.0, step=0.004):
    return [start * (1 + step) ** i * (1 + 0.01 * math.sin(i / 3)) for i in range(n)]


class IndicatorTests(unittest.TestCase):
    def test_ema_rsi_atr_basics(self):
        from backend.signals.indicators import atr, ema, rsi
        self.assertEqual(ema([1, 2, 3, 4, 5], 5)[-1], 3)
        self.assertEqual(rsi(list(range(1, 30)), 14)[-1], 100.0)
        bars = [{"high": 11, "low": 9, "close": 10}] * 20
        self.assertAlmostEqual(atr(bars, 14)[-1], 2.0)

    def test_swing_points_confirmed_only(self):
        from backend.signals.indicators import swing_points
        highs = [1, 2, 5, 2, 1, 2, 3]
        bars = [{"high": h, "low": h - 1} for h in highs]
        sh, _ = swing_points(bars, 2)
        self.assertEqual(sh, [(2, 5)])


class RegimeTests(unittest.TestCase):
    def test_strong_uptrend_and_insufficient_history(self):
        from backend.signals.regime import current_regime
        self.assertEqual(current_regime(make_bars(uptrend(300, step=0.006)))["regime"], "STRONG_UP")
        self.assertEqual(current_regime(make_bars(uptrend(100)))["regime"], "UNSTABLE")

    def test_flat_market_is_sideways(self):
        from backend.signals.regime import current_regime
        flat = [100 * (1 + 0.02 * math.sin(i / 2)) for i in range(300)]
        self.assertEqual(current_regime(make_bars(flat))["regime"], "SIDEWAYS")


class SetupTests(unittest.TestCase):
    def breakout_bars(self, volume_mult=2.5):
        closes = uptrend(260, step=0.004) 
        base = closes[-1]
        closes += [base * (1 + 0.004 * math.sin(i)) for i in range(25)]  # tight consolidation
        closes.append(base * 1.012)                                      # breakout close
        bars = make_bars(closes, wiggle=0.004)
        bars[-1]["volume"] = int(1_000_000 * volume_mult)
        bars[-1]["open"] = base
        bars[-1]["high"] = base * 1.013
        bars[-1]["low"] = base * 0.999
        return bars

    def test_breakout_long_is_found_and_scored(self):
        from backend.signals.setups import evaluate
        from backend.marketdata.candles import resample_weekly
        bars = self.breakout_bars()
        cands = evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",), is_stock=False)
        brk = next(c for c in cands if c["setup"] == "breakout")
        self.assertTrue(brk["valid"], brk.get("reject_reason"))
        self.assertGreaterEqual(brk["rr"], 2.0)
        self.assertLess(brk["stop"], brk["entry"])
        self.assertEqual(set(brk["factors"]), {"htf_trend", "setup_quality", "volume_oi", "rr_stop",
                                               "regime_fit", "relative_strength"})
        self.assertIsNone(brk["factors"]["relative_strength"])  # not a stock: scored out of 90

    def test_breakout_needs_volume_and_regime_permission(self):
        from backend.signals.setups import evaluate
        from backend.marketdata.candles import resample_weekly
        bars = self.breakout_bars(volume_mult=1.1)
        brk = next(c for c in evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",))
                   if c["setup"] == "breakout")
        self.assertIn("volume", brk["reject_reason"])
        bars = self.breakout_bars()
        for regime in ("UNSTABLE", "STRONG_DOWN"):
            cands = evaluate(bars, resample_weekly(bars), regime, directions=("long",))
            self.assertFalse(any(c["valid"] for c in cands), regime)
        sideways = evaluate(bars, resample_weekly(bars), "SIDEWAYS", directions=("long",))
        self.assertTrue(next(c for c in sideways if c["setup"] == "breakout")["valid"])  # 2.5x >= 2x

    def test_trend_pullback_long(self):
        from backend.signals.setups import evaluate
        from backend.marketdata.candles import resample_weekly
        closes = [100 * 1.004 ** i * (1 + 0.03 * math.sin((i + 18) / 3)) for i in range(280)]
        base = closes[-1]
        closes += [base * (1 - 0.03 * (i + 1) / 3) for i in range(3)]
        closes.append(closes[-1] * 1.03)
        bars = make_bars(closes, wiggle=0.005)
        bars[-1].update(open=closes[-2], low=closes[-2] * 0.997, high=closes[-1] * 1.001)
        pb = next(c for c in evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",))
                  if c["setup"] == "pullback")
        self.assertTrue(pb["valid"], pb.get("reject_reason"))
        self.assertGreaterEqual(pb["score"], 60)
        # Same chart in a sideways regime: pullbacks are not allowed.
        side = next(c for c in evaluate(bars, resample_weekly(bars), "SIDEWAYS", directions=("long",))
                    if c["setup"] == "pullback")
        self.assertIn("not allowed", side["reject_reason"])

    def test_no_room_for_2r_rejects(self):
        from backend.signals.setups import _context, geometry
        bars = make_bars(uptrend(260, step=0.004))
        # A recent high just above the entry blocks the target.
        bars[-10]["high"] = bars[-1]["close"] * 1.01
        bars[-11]["high"] = bars[-12]["high"] = bars[-9]["high"] = bars[-8]["high"] = bars[-1]["close"] * 0.99
        geo = geometry(bars, _context(bars), 1)
        self.assertFalse(geo["ok"])
        self.assertIn("no room", geo["reason"])


class EventTests(IsolatedStateTestCase, unittest.TestCase):
    def test_market_events_and_fail_closed_past_coverage(self):
        from backend.signals import events
        got = events.market_events_between(date(2026, 11, 30), date(2026, 12, 8))
        self.assertEqual(got["events"], [{"date": "2026-12-04", "event": "RBI policy"}])
        self.assertFalse(events.market_events_between(date(2027, 3, 25), date(2027, 4, 5))["verified"])

    def test_results_unknown_is_excluded(self):
        from backend.signals import events
        today = date(2026, 11, 20)
        fetched = {"AAA": date(2027, 1, 20), "BBB": date(2026, 11, 3), "CCC": None, "DDD": date(2026, 11, 25)}
        events.refresh_results(list(fetched), fetch=fetched.get, today=today)
        first, last = date(2026, 11, 23), date(2026, 12, 2)
        self.assertEqual(events.results_clear("AAA", first, last, today), (True, ""))
        self.assertTrue(events.results_clear("BBB", first, last, today)[0])  # reported 17 days ago
        self.assertEqual(events.results_clear("CCC", first, last, today), (False, "results date unverified"))
        self.assertIn("inside the holding window", events.results_clear("DDD", first, last, today)[1])
        self.assertFalse(events.results_clear("ZZZ", first, last, today)[0])


class CashSwingScanTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany(
                "INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                 (501, "NSE", "NSE", "STOCK1", "STOCK1", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                 (502, "NSE", "NSE", "THIN", "THIN", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                 (601, "NFO", "NFO-FUT", "STOCK1DECFUT", "STOCK1", "FUT", "2026-12-29", 0, 500, 0.05, "2026-11-20"),
                 (602, "NFO", "NFO-FUT", "THINDECFUT", "THIN", "FUT", "2026-12-29", 0, 500, 0.05, "2026-11-20")],
            )

    def save(self, token, bars):
        from backend.marketdata.candles import save_candles
        save_candles(token, "day", [{**b, "date": b["ts"]} for b in bars])

    def test_scan_records_every_stock_and_blocks_on_events(self):
        from backend.signals.cash_swing import latest_run, run_scan
        self.save(256265, make_bars(uptrend(300, start=20000, step=0.006)))
        self.save(501, make_bars(uptrend(300, start=1000, step=0.005), volume=2_000_000))
        self.save(502, make_bars(uptrend(300, start=10, step=0.005), volume=1000))
        fetch = {"STOCK1": date(2027, 1, 25), "THIN": date(2027, 1, 25)}.get
        # 20 Nov: the 7-day window (23 Nov - 2 Dec) is clear of RBI (4 Dec).
        result = run_scan(now=datetime(2026, 11, 20, 15, 50), results_fetch=fetch)
        self.assertEqual(result["status"], "ok", result)
        run = latest_run("2026-11-20")
        by_symbol = {c["symbol"]: c for c in run["candidates"]}
        self.assertEqual(set(by_symbol), {"STOCK1", "THIN"})
        self.assertIn("turnover", by_symbol["THIN"]["reject_reason"])
        # 27 Nov: window reaches 4 Dec RBI policy -> whole scan blocked (fail closed).
        for token, start in ((256265, 20000), (501, 1000), (502, 10)):
            self.save(token, make_bars(uptrend(300, start=start, step=0.005), end=date(2026, 11, 27)))
        blocked = run_scan(now=datetime(2026, 11, 27, 15, 50), results_fetch=fetch)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("RBI policy", blocked["reason"])

    def test_stale_nifty_blocks_scan(self):
        from backend.signals.cash_swing import run_scan
        self.save(256265, make_bars(uptrend(300, start=20000), end=date(2026, 11, 18)))
        result = run_scan(now=datetime(2026, 11, 20, 15, 50), results_fetch=lambda s: None)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("stale", result["reason"])


class SchedulerScanHookTests(unittest.TestCase):
    def test_scan_due_after_daily_sync(self):
        from backend.scheduler import due_jobs
        close = datetime(2026, 11, 23, 15, 30)
        state = {"paused": False, "kite_connected": True, "instruments_fresh": True,
                 "done": {"candles:NSE:day": close, "candles:NSE:60minute": close, "candles:NSE:10minute": close,
                          "candles:MCX:60minute": datetime(2026, 11, 23, 15, 0)}, "scans_done": {}}
        self.assertIn("scan:cash_swing", due_jobs(datetime(2026, 11, 23, 15, 46), state))
        self.assertNotIn("scan:cash_swing", due_jobs(datetime(2026, 11, 23, 15, 40), state))
        state["scans_done"] = {"cash_swing": date(2026, 11, 23)}
        self.assertNotIn("scan:cash_swing", due_jobs(datetime(2026, 11, 23, 15, 46), state))


if __name__ == "__main__":
    unittest.main()
