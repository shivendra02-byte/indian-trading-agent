"""Trade journal: outcome simulation, tracking against stored candles, learning report."""

import unittest
from datetime import date, datetime

from tests._support import IsolatedStateTestCase, fresh_test_client, login


def bar(ts, o, h, l, c, eod=True):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "eod": eod}


PLAN = dict(direction="long", entry=100.0, stop=96.0, target=108.0, atr=2.0,
            fill_until="2026-11-23T11:00", time_stop_date="2026-12-02")


class SimulateTests(unittest.TestCase):
    def sim(self, bars, **kw):
        from backend.journal.simulate import simulate
        return simulate(bars=bars, **{**PLAN, **kw})

    def test_target_hit_with_mfe_mae(self):
        r = self.sim([bar("2026-11-23T09:15", 101, 101.5, 99.5, 100.5, False),
                      bar("2026-11-24T00:00", 100.5, 103, 98, 102),
                      bar("2026-11-25T00:00", 102, 108.5, 101, 108)])
        self.assertEqual((r["status"], r["exit_reason"], r["pnl_r"]), ("closed", "target", 2.0))
        self.assertEqual(r["fill_price"], 100.0)
        self.assertEqual(r["mae_r"], 0.5)   # low 98 = 2 below fill, R = 4
        self.assertEqual(r["mfe_r"], 2.0)

    def test_stop_first_when_both_touched_and_gap_stop(self):
        both = self.sim([bar("2026-11-23T09:15", 100, 100.5, 99, 100, False),
                         bar("2026-11-24T00:00", 100, 109, 95, 104)])
        self.assertEqual((both["exit_reason"], both["pnl_r"]), ("stop", -1.0))
        gap = self.sim([bar("2026-11-23T09:15", 100, 100.5, 99, 100, False),
                        bar("2026-11-24T00:00", 94, 95, 93, 94.5)])
        self.assertEqual((gap["exit_reason"], gap["exit_price"], gap["pnl_r"]), ("gap_stop", 94, -1.5))

    def test_no_fill_cases_and_pending(self):
        unfilled = self.sim([bar("2026-11-23T09:15", 102, 103, 101, 102, False),
                             bar("2026-11-23T10:15", 102, 103, 101, 102, False),
                             bar("2026-11-23T11:15", 102, 103, 100.5, 101, False)])
        self.assertEqual(unfilled["status"], "no_fill")
        gapped = self.sim([bar("2026-11-23T09:15", 95, 96, 94, 95, False)])
        self.assertEqual(gapped["reason"], "opened beyond the stop")
        self.assertEqual(self.sim([])["status"], "pending")

    def test_time_stop_and_trailing_shadow(self):
        bars = [bar("2026-11-23T09:15", 100, 100.2, 99.8, 100, False)]
        closes = [101, 103, 106.5, 106, 104, 103, 102.5]   # +1.625R peak, then fades
        for i, c in enumerate(closes):
            day = ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02"][i]
            bars.append(bar(f"{day}T00:00:00", c - 0.5, c + 0.3, c - 0.8, c))
        r = self.sim(bars)
        self.assertEqual(r["exit_reason"], "time_stop")
        self.assertAlmostEqual(r["pnl_r"], 0.625)
        self.assertEqual(r["trail"]["exit_reason"], "trail_stop")   # trailed 2 ATR below the 106.5 close
        self.assertAlmostEqual(r["trail"]["pnl_r"], 0.625)

    def test_delivery_charges(self):
        from backend.journal.simulate import delivery_charges
        charges = delivery_charges(100_000, 102_000)
        self.assertTrue(215 < charges < 245, charges)


class TrackerAndLearningTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from backend.journal.store import add_records
        ensure_marketdata_tables()
        save_candles(501, "60minute", [
            {"date": "2026-11-23T09:15:00", "open": 101, "high": 101.5, "low": 99.8, "close": 100.6, "volume": 1},
            {"date": "2026-11-23T15:15:00", "open": 100.6, "high": 101, "low": 100.2, "close": 100.8, "volume": 1}])
        save_candles(501, "day", [
            {"date": "2026-11-25T00:00:00", "open": 101, "high": 108.4, "low": 100.5, "close": 108, "volume": 1}])
        base = dict(segment="cash_swing", instrument_token=501, exchange="NSE", signal_date="2026-11-20",
                    direction="long", regime="STRONG_UP", score=70, entry=100.0, stop=96.0, target=108.0, rr=2.0,
                    atr=2.0, entry_date="2026-11-23", fill_until="2026-11-23T11:00", time_stop_date="2026-12-02")
        add_records([
            {**base, "symbol": "AAA", "setup": "breakout", "decision": "shortlisted",
             "soft_checks": {"regime": True, "volume": True}, "extra": {"vol_ratio": 1.8}},
            {**base, "symbol": "BBB", "setup": "breakout", "decision": "soft_rejected",
             "reject_reason": "volume 1.2x < 1.5x", "soft_checks": {"regime": True, "volume": False},
             "extra": {"vol_ratio": 1.2}},
        ])

    def test_tracking_closes_records_with_rupee_pnl(self):
        from backend.journal.store import list_records
        from backend.journal.tracker import track_open_records
        counts = track_open_records()
        self.assertEqual(counts["closed"], 2)
        rec = list_records("symbol = 'AAA'")[0]
        self.assertEqual((rec["track_status"], rec["exit_reason"], rec["pnl_r"]), ("closed", "target", 2.0))
        self.assertEqual(rec["qty"], 2500)          # ₹10k risk / ₹4 = 2500; value ₹2.5L cap
        self.assertGreater(rec["pnl_rupees"], 19_000)
        self.assertLess(rec["pnl_rupees"], 20_000)  # ₹20k gross minus charges

    def test_learning_report_answers_questions_and_flags_small_samples(self):
        from backend.journal.learning import report
        from backend.journal.tracker import track_open_records
        track_open_records()
        rep = report("cash_swing")
        self.assertEqual(rep["closed"], 2)
        self.assertEqual(rep["q3_volume_confirmation"]["failed_1_5x"]["n"], 1)
        self.assertFalse(rep["q3_volume_confirmation"]["passed_1_5x"]["sufficient"])
        self.assertEqual(rep["q6_rejected_but_profitable"]["profitable_count"], 2)
        self.assertEqual(rep["q1_best_setup_trending"]["best"]["group"], "breakout")

    def test_journal_routes(self):
        with fresh_test_client() as client:
            login(client)
            listed = client.get("/api/journal?segment=cash_swing&decision=soft_rejected")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual([r["symbol"] for r in listed.json()["records"]], ["BBB"])
            self.assertEqual(client.get("/api/journal/learning").status_code, 200)


class ScanFeedsJournalTests(IsolatedStateTestCase, unittest.TestCase):
    def test_scan_writes_trackable_candidates_to_journal(self):
        from backend.journal.store import list_records
        from backend.signals.cash_swing import run_scan
        from tests.test_signals import make_bars, uptrend
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "STOCK1", "STOCK1", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "STOCK1DECFUT", "STOCK1", "FUT", "2026-12-29", 0, 500, 0.05, "2026-11-20")])
        import math
        nifty = make_bars(uptrend(300, start=20000, step=0.006), volume=3_000_000)
        # A stock with a textbook pullback on the last bar (same shape as the setup test).
        closes = [100 * 1.004 ** i * (1 + 0.03 * math.sin((i + 18) / 3)) for i in range(280)]
        base = closes[-1]
        closes += [base * (1 - 0.03 * (i + 1) / 3) for i in range(3)]
        closes.append(closes[-1] * 1.03)
        stock = make_bars(closes, wiggle=0.005, volume=3_000_000)
        stock[-1].update(open=closes[-2], low=closes[-2] * 0.997, high=closes[-1] * 1.001)
        for token, bars in ((256265, nifty), (501, stock)):
            save_candles(token, "day", [{**b, "date": b["ts"]} for b in bars])
        result = run_scan(now=datetime(2026, 11, 20, 15, 50), results_fetch=lambda s: date(2027, 1, 25))
        self.assertEqual(result["status"], "ok", result)
        records = list_records("segment = 'cash_swing'")
        self.assertEqual(result["journal_records"], len(records))
        self.assertGreater(len(records), 0)
        for rec in records:
            self.assertIn(rec["decision"], ("soft_rejected", "below_score", "not_selected", "shortlisted"))
            self.assertEqual(rec["entry_date"], "2026-11-23")
            self.assertEqual(rec["time_stop_date"], "2026-12-02")


if __name__ == "__main__":
    unittest.main()
