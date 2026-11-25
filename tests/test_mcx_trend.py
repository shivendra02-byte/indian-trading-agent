"""MCX trend pullback (4H trend → 2H pullback → 1H trigger), the HIGH_VOL trend regime fix and
the read-only replay tool (agreed 25 Nov 2026). Synthetic silver-like bars: a fall in legs of
-7% with +3.8% bounces that fail at lower highs."""

import math
import unittest
from datetime import date, timedelta

from tests._support import IsolatedStateTestCase
from tests.test_signals import make_bars
from tradingagents.utils.market_calendar import MCX, session_windows

DOWN = {"regime": "STRONG_DOWN"}


def mcx_slots(end: date, n: int) -> list[str]:
    out, d = [], end
    while len(out) < n:
        w = session_windows(MCX, d)
        if w:
            last = w[-1][1].hour + (1 if w[-1][1].minute else 0)
            out = [f"{d.isoformat()}T{h:02d}:00:00" for h in range(w[0][0].hour, last)] + out
        d -= timedelta(days=1)
    return out[-n:]


def falling_legs(n=700, start=120000.0, drop=0.07, bounce=0.038):
    closes, p = [], start
    while len(closes) < n:
        for _ in range(45):
            p *= 1 - drop / 45
            closes.append(p * (1 + 0.0015 * math.sin(len(closes))))
        for _ in range(22):
            p *= 1 + bounce / 22
            closes.append(p * (1 + 0.0015 * math.sin(len(closes))))
    return closes[:n]


def hourly(closes, end=date(2026, 11, 24)):
    out, prev = [], closes[0]
    for ts, c in zip(mcx_slots(end, len(closes)), closes):
        out.append({"ts": ts, "open": prev, "high": max(prev, c) * 1.0015, "low": min(prev, c) * 0.9985,
                    "close": c, "volume": 1000, "oi": 100000})
        prev = c
    return out


def views(h):
    from backend.marketdata.candles import resample_hours
    return resample_hours(h, MCX, 2), resample_hours(h, MCX, 4)


class TrendPullbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = hourly(falling_legs())

    def signals(self, regime=DOWN, daily_trend=-1.0, d="short", h=None):
        from backend.signals import mcx_trend
        h = h or self.h
        out = []
        for j in range(400, len(h)):
            w = h[:j + 1]
            h2, h4 = views(w)
            r = mcx_trend.check(d, regime, daily_trend, h4, h2, w[-400:])
            if r["ok"]:
                out.append(j)
        return out

    def test_one_short_per_failed_bounce(self):
        hits = self.signals()
        self.assertEqual(len(hits), 5)                       # five bounces after bar 400
        tops = [67 * k - 1 for k in range(6, 11)]
        for j in hits[:4]:
            self.assertTrue(any(0 < j - t <= 10 for t in tops), j)   # within ~10 hours after a bounce top

    def test_regime_gate(self):
        self.assertEqual(self.signals(regime={"regime": "SIDEWAYS"}), [])
        self.assertEqual(self.signals(regime={"regime": "HIGH_VOL", "trend": None}), [])
        self.assertEqual(len(self.signals(regime={"regime": "HIGH_VOL", "trend": "down"})), 5)
        self.assertEqual(self.signals(daily_trend=1.0), [])
        self.assertEqual(self.signals(d="long", regime={"regime": "MODERATE_UP"}), [])   # agreed 24 Nov 2026

    def test_higher_high_is_not_a_pullback(self):
        from backend.signals import mcx_trend
        h = hourly(falling_legs(bounce=0.09))                 # bounces bigger than the legs → no lower highs
        self.assertEqual(self.signals(h=h), [])
        w = h[:470]
        h2, h4 = views(w)
        self.assertFalse(mcx_trend.check("short", DOWN, -1.0, h4, h2, w)["ok"])

    def test_candidate_row(self):
        from backend.signals import mcx_trend
        w = self.h[:408]
        h2, h4 = views(w)
        c = mcx_trend.candidates(w[-400:], h2, h4, DOWN, -1.0, directions=("short",))[0]
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertEqual(c["setup"], "mcx_trend")
        self.assertGreater(c["stop"], c["entry"])
        self.assertGreaterEqual(c["rr"], 2.0)
        self.assertLessEqual(c["stop_atr"], 3.0)
        self.assertGreaterEqual(c["score"], 60)
        self.assertEqual([s["stage"] for s in c["stages"]][:4], ["regime", "trend_4h", "pullback_2h", "trigger_1h"])

    def test_live_config(self):
        from backend.decision.config import get_config
        from backend.signals import mcx_trend
        extra = get_config().instrument("mcx").model_extra
        self.assertTrue(mcx_trend.config_for(extra)["enabled"])
        self.assertTrue(extra["regime_high_vol_trend"])
        nse = mcx_trend.config_for(get_config().instrument("stock_futures").model_extra)   # shadow since 25 Nov 2026
        self.assertEqual(nse["setup_name"], "trend_pullback")
        self.assertTrue(get_config().global_.is_shadow("stock_futures", "trend_pullback"))
        self.assertFalse(get_config().global_.is_shadow("mcx", "mcx_trend"))
        self.assertIsNone(mcx_trend.config_for(get_config().instrument("cash_swing").model_extra))


def silver_nov_2026():
    """Hourly path shaped like MCX silver in Nov 2026 (×1000): fall from 58 to 48, leg down to 41,
    small bounce to 42.8, low 40.8, bounce to 44.2 (above the small 2H swing, below the 48 leg
    start), then roll over to 40.2."""
    v = [(0, 58), (150, 53.5), (200, 54.5), (300, 48), (345, 41), (352, 42.8), (372, 40.8), (400, 44.2), (430, 40.2)]
    closes = []
    for (i0, c0), (i1, c1) in zip(v, v[1:]):
        closes += [c0 + (c1 - c0) * (i - i0) / (i1 - i0) for i in range(i0, i1)]
    closes.append(v[-1][1])
    return hourly([x * 1000 * (1 + 0.002 * math.sin(i)) for i, x in enumerate(closes)])


class SilverNovemberTests(unittest.TestCase):
    """25 Nov 2026: the 24 Nov bounce to ~44.2 was rejected as 'no lower high' because it topped the
    small 2H swing at ~42.8. The lower high is now judged on the 4H scale (vs the ~48 leg start)."""

    def test_short_after_the_bounce_to_44(self):
        from backend.signals import mcx_trend
        h = silver_nov_2026()
        hv = {"regime": "HIGH_VOL", "trend": "down"}
        hits = []
        for j in range(395, len(h)):
            w = h[:j + 1]
            h2, h4 = views(w)
            if mcx_trend.check("short", hv, -1.0, h4, h2, w[-400:])["ok"]:
                hits.append(j)
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(401 <= hits[0] <= 412, hits)            # soon after the 44.2 top at bar 400
        w = h[:hits[0] + 1]
        h2, h4 = views(w)
        c = mcx_trend.candidates(w[-400:], h2, h4, hv, -1.0, directions=("short",))[0]
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertGreater(c["stop"], c["entry"])
        self.assertLessEqual(c["stop_atr"], 3.0)                 # stop above the high is 3.6 ATR → capped at 3
        self.assertIn("capped at 3 ATR", c["stop_type"])
        self.assertGreaterEqual(c["rr"], 2.0)


class HighVolTrendRegimeTests(unittest.TestCase):
    def test_high_vol_days_in_a_fall_are_not_flips(self):
        from backend.signals import regime as rg
        closes = [100 * 0.997 ** i for i in range(300)]
        bars = make_bars(closes, end=date(2026, 11, 20))
        labels = ["STRONG_DOWN", "HIGH_VOL", "STRONG_DOWN", "HIGH_VOL", "STRONG_DOWN",
                  "HIGH_VOL", "HIGH_VOL", "STRONG_DOWN", "HIGH_VOL", "HIGH_VOL"]
        orig = rg.label_series
        rg.label_series = lambda b: [None] * (len(b) - 10) + labels
        try:
            self.assertEqual(rg.current_regime(bars)["regime"], "UNSTABLE")
            r = rg.current_regime(bars, high_vol_trend=True)
        finally:
            rg.label_series = orig
        self.assertEqual(r["regime"], "HIGH_VOL")
        self.assertEqual(r["trend"], "down")


def store_silverm(db, h, daily_end, scale=1.0):
    from backend.marketdata import ensure_marketdata_tables
    from backend.marketdata.candles import save_candles
    ensure_marketdata_tables()
    with db.get_db() as conn:
        conn.execute("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (701, "MCX", "MCX-FUT", "SILVERM27FEBFUT", "SILVERM", "FUT", "2027-02-26", 0, 5, 1.0,
                      "2026-11-24"))
    save_candles(701, "60minute", [{**b, "date": b["ts"]} for b in h])
    daily = make_bars([150000 * scale * 0.998 ** i for i in range(300)], end=date(2026, 10, 1))
    daily += make_bars([daily[-1]["close"] * 0.997 ** i for i in range(1, 45)], end=date(2026, 11, 24))
    save_candles(701, "day", [{**b, "date": b["ts"]} for b in daily if b["ts"][:10] <= daily_end])


class LiveScanTests(IsolatedStateTestCase, unittest.TestCase):
    def test_hourly_mcx_scan_journals_the_trend_short(self):
        from datetime import datetime
        from backend.journal.store import ensure_journal_table
        from backend.signals.fno import run_hourly_scan
        from tests.test_fno_scan import FakeQuoteKite
        h = hourly(falling_legs())[:408]                       # last bar = the 27 Oct 15:00 trigger
        self.assertEqual(h[-1]["ts"], "2026-10-27T15:00:00")
        store_silverm(self.db, h, "2026-10-26")
        res = run_hourly_scan("mcx", now=datetime(2026, 10, 27, 16, 5), client=FakeQuoteKite())
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["shortlisted"], ["SILVERM"])
        ensure_journal_table()
        with self.db.get_db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM trade_journal WHERE setup = 'mcx_trend'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["direction"], rows[0]["decision"]), ("short", "shortlisted"))


class ReplayTests(IsolatedStateTestCase, unittest.TestCase):
    def test_replay_finds_the_trend_shorts_only_with_the_new_rules(self):
        from backend.mcx_replay import replay
        store_silverm(self.db, hourly(falling_legs()), "2026-11-24")
        res = replay(date(2026, 10, 20), ["SILVERM"], today=date(2026, 11, 24))
        new = [r for r in res["signals"] if r["rule_set"] == "new" and r["setup"] == "mcx_trend"]
        self.assertTrue(new, res["rejects"]["new"].most_common(5))
        self.assertTrue(all(r["direction"] == "short" for r in new))
        self.assertFalse([r for r in res["signals"] if r["rule_set"] == "before" and r["setup"] == "mcx_trend"])
        self.assertTrue(any("result" in r for r in new))

    def test_bars_without_open_interest_are_skipped_like_the_live_scan(self):
        from backend.mcx_replay import replay
        from backend.signals.fno import oi_signal
        h = hourly(falling_legs())
        for b in h[500:520]:
            b["oi"] = None
        self.assertIsNone(oi_signal(h[:510])["change_pct"])
        store_silverm(self.db, h, "2026-11-24")
        res = replay(date(2026, 10, 20), ["SILVERM"], today=date(2026, 11, 24))
        self.assertGreaterEqual(res["rejects"]["new"]["data: no open interest on the last bar"], 18)   # minus timing-excluded bars


    def test_one_lot_rule_and_blocked_book(self):
        """Prices ×3: a lot risks ~₹18k — blocked at 1% ("before"), 1 lot at the MCX 3.5% rule ("new").
        Prices ×7: ~₹42k — blocked under both, followed in the blocked book with its result in R."""
        from backend.mcx_replay import replay
        store_silverm(self.db, hourly(falling_legs(start=360000.0)), "2026-11-24", scale=3)
        res = replay(date(2026, 10, 20), ["SILVERM"], today=date(2026, 11, 24))
        new = [r for r in res["signals"] if r["rule_set"] == "new" and r["setup"] == "mcx_trend"]
        self.assertTrue(new)
        taken = [r for r in new if r.get("taken")]
        self.assertTrue(taken, [r["result"] for r in new])
        self.assertTrue(all(15_000 < r["lot_risk"] <= 35_000 for r in taken))

    def test_too_big_even_for_the_mcx_limit_is_followed_as_blocked(self):
        from backend.mcx_replay import replay
        store_silverm(self.db, hourly(falling_legs(start=840000.0)), "2026-11-24", scale=7)
        res = replay(date(2026, 10, 20), ["SILVERM"], today=date(2026, 11, 24))
        new = [r for r in res["signals"] if r["rule_set"] == "new" and r["setup"] == "mcx_trend"]
        self.assertTrue(new)
        self.assertFalse([r for r in new if r.get("taken")])
        self.assertTrue(all(r["result"].startswith("BLOCKED: 1 lot risks") for r in new))
        self.assertTrue([r for r in new if r.get("blocked_taken") and r.get("blocked_r") is not None])


if __name__ == "__main__":
    unittest.main()
