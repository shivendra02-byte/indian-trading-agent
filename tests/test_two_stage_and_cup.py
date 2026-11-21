"""Two-stage (scored) selection, data without volume, and cup-and-handle detection."""

import unittest
from datetime import date, timedelta

from tests._support import IsolatedStateTestCase
from tests.test_signals import SetupTests, make_bars, uptrend
from tradingagents.utils.market_calendar import is_trading_day


def downtrend_weekly(n=80):
    closes = [200 * (1 - 0.01) ** i for i in range(n)]
    return [{"ts": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}T00:00:00", "open": c * 1.01, "high": c * 1.02,
             "low": c * 0.99, "close": c, "volume": 1} for i, c in enumerate(closes)]


class ScoredSelectionTests(unittest.TestCase):
    def _breakout(self, **kw):
        from backend.signals.setups import evaluate
        bars = SetupTests.breakout_bars(SetupTests())
        return next(c for c in evaluate(bars, downtrend_weekly(), "UNSTABLE", directions=("long",), **kw)
                    if c["setup"] == "breakout")

    def test_strict_rejects_but_scored_penalises(self):
        from backend.decision.config import available_rule_sets, use_config
        strict = self._breakout()
        self.assertFalse(strict["trackable"])
        self.assertIn("higher-timeframe", strict["reject_reason"])
        with use_config(available_rule_sets()["two_stage"]):
            scored = self._breakout()
        self.assertTrue(scored["valid"], scored.get("reject_reason"))   # regime not permitted: score only
        self.assertTrue(scored["trackable"])
        self.assertEqual(scored["penalties"], {"htf_against": 20})
        self.assertAlmostEqual(scored["score"], max(0, scored["score_before_penalties"] - 20), places=1)

    def test_context_against_is_a_penalty_in_scored_mode(self):
        from backend.decision.config import available_rule_sets, use_config
        from backend.marketdata.candles import resample_weekly
        from backend.signals.setups import evaluate
        bars = SetupTests.breakout_bars(SetupTests())
        ctx = {"day": downtrend_weekly()}
        strict = [c for c in evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",), context=ctx)
                  if c["setup"] == "breakout"][0]
        self.assertIn("day trend against", strict["reject_reason"])
        with use_config(available_rule_sets()["two_stage"]):
            scored = [c for c in evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",),
                                          context=ctx) if c["setup"] == "breakout"][0]
        self.assertTrue(scored["valid"])
        self.assertEqual(scored["penalties"], {"day_against": 10})

    def test_without_volume_is_not_tested(self):
        from backend.marketdata.candles import resample_weekly
        from backend.signals.setups import evaluate
        bars = SetupTests.breakout_bars(SetupTests(), volume_mult=0.5)
        for b in bars:
            b["volume"] = 0
        brk = next(c for c in evaluate(bars, resample_weekly(bars), "STRONG_UP", directions=("long",),
                                       is_stock=False, volume_available=False) if c["setup"] == "breakout")
        self.assertTrue(brk["valid"], brk.get("reject_reason"))
        self.assertIsNone(brk["soft_checks"]["volume"])
        self.assertIsNone(brk["factors"]["volume_oi"])


def _days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def cup_bars(v_shape=False, end=date(2026, 11, 20)):
    closes, vols = [], []
    closes += [70 + 0.2 * (i % 3) for i in range(150)]; vols += [1_000_000] * 150          # base
    closes += [70 + 30 * i / 129 for i in range(130)]; vols += [1_000_000] * 130           # prior uptrend to 100
    if v_shape:
        closes += [100 - 22 * i / 30 for i in range(1, 31)] + [78 + 21 * i / 30 for i in range(1, 31)]
    else:
        closes += [100 - 22 * i / 22 for i in range(1, 23)]                                # decline to 78
        closes += [78 + (0.3 if i % 2 else 0) for i in range(16)]                          # rounded bottom
        closes += [78 + 21.5 * i / 24 for i in range(1, 25)]                               # rise to 99.5
    vols += [1_000_000] * (len(closes) - len(vols))
    closes += [99.5 - 6.5 * i / 7 for i in range(1, 8)]; vols += [600_000] * 7             # handle to 93
    closes += [101.5]; vols += [2_500_000]                                                 # breakout
    days = _days(end, len(closes))
    bars, prev = [], closes[0]
    for d, c, v in zip(days, closes, vols):
        bars.append({"ts": f"{d.isoformat()}T00:00:00", "open": c * 0.999, "high": c * 1.003,
                     "low": c * 0.997, "close": c, "volume": v})
        prev = c
    return bars


def flat_nifty(bars):
    return [{**b, "open": 20000, "high": 20010, "low": 19990, "close": 20000} for b in bars]


class CupHandleTests(unittest.TestCase):
    def test_detects_cup_and_handle_breakout(self):
        from backend.signals.cup_handle import candidate
        bars = cup_bars()
        c = candidate(bars, flat_nifty(bars))
        self.assertTrue(c["trackable"], c.get("reject_reason"))
        p = c["pattern"]
        self.assertEqual(p["handle_days"], 8)   # right rim day + 7 handle days
        self.assertTrue(12 <= p["cup_depth_pct"] <= 33)
        self.assertAlmostEqual(c["entry"], round(p["pivot"] * 1.05, 2))   # top of the buy zone
        self.assertAlmostEqual(c["stop"], round(101.5 * 0.92, 2))
        self.assertAlmostEqual(c["target"], round(101.5 * 1.20, 2))
        self.assertTrue(p["handle_volume_dry_up"])
        self.assertGreater(c["score"], 0)

    def test_rejections(self):
        from backend.signals.cup_handle import detect
        v = cup_bars(v_shape=True)
        self.assertFalse(detect(v, flat_nifty(v))["found"])
        weak = cup_bars()
        weak[-1]["volume"] = 900_000
        self.assertIn("volume", detect(weak, flat_nifty(weak))["reason"])
        no_break = cup_bars()[:-1]
        self.assertFalse(detect(no_break, flat_nifty(no_break))["found"])
        strong_nifty = [{**b, "close": 20000 * (1 + 0.01 * i)} for i, b in enumerate(cup_bars())]
        self.assertIn("NIFTY", detect(cup_bars(), strong_nifty)["reason"])


class CupHandleScanTests(IsolatedStateTestCase, unittest.TestCase):
    def test_scan_journals_the_pattern(self):
        from datetime import datetime
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from backend.signals import cup_handle
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "CUPCO", "CUPCO", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "CUPCO26DECFUT", "CUPCO", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-20")])
        bars = [{**b, "volume": b["volume"] * 10} for b in cup_bars()]
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in bars])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in flat_nifty(bars)])
        from backend.signals import events
        events.refresh_results(["CUPCO"], fetch=lambda s: date(2027, 3, 1), today=date(2026, 11, 20))
        out = cup_handle.run_scan(now=datetime(2026, 11, 20, 16, 0), results_fetch=lambda s: date(2027, 3, 1))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["patterns"], 1, out)
        from backend.journal.store import list_records
        recs = list_records("segment = ?", ("cup_handle",))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["setup"], "cup_handle")


if __name__ == "__main__":
    unittest.main()


class LocalTimeTests(IsolatedStateTestCase, unittest.TestCase):
    def test_old_utc_defaults_store_indian_time(self):
        import os
        import time as _time
        from datetime import datetime, timezone
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Kolkata"
        _time.tzset()
        try:
            from backend.db import install_localtime_triggers
            with self.db.get_db() as conn:
                conn.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY, note TEXT, "
                             "created_at TEXT DEFAULT (datetime('now')))")
            self.assertIn("localtime_legacy_created_at", install_localtime_triggers())
            self.assertEqual(install_localtime_triggers(), [])          # idempotent
            with self.db.get_db() as conn:
                conn.execute("INSERT INTO legacy (note) VALUES ('default')")
                conn.execute("INSERT INTO legacy (note, created_at) VALUES ('explicit', '2026-01-01 10:00:00')")
                rows = dict(conn.execute("SELECT note, created_at FROM legacy").fetchall())
            utc = datetime.now(timezone.utc).replace(tzinfo=None)
            stored = datetime.fromisoformat(rows["default"])
            self.assertAlmostEqual((stored - utc).total_seconds(), 5.5 * 3600, delta=120)
            self.assertEqual(rows["explicit"], "2026-01-01 10:00:00")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            _time.tzset()
