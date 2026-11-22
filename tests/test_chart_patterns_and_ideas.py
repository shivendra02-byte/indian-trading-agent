"""Chart patterns (trendline bounce/break, channel, triangle) and the ideas tracker."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login


def bars_from(closes, vols=None, start=date(2025, 1, 1)):
    out, d, prev = [], start, closes[0]
    for i, c in enumerate(closes):
        o = (prev + c) / 2
        out.append({"ts": d.isoformat() + "T00:00:00", "open": o, "high": max(o, c) + 0.3,
                    "low": min(o, c) - 0.3, "close": c, "volume": vols[i] if vols else 1000, "oi": 0})
        prev, d = c, d + timedelta(days=1)
    return out


def zigzag(n, base=100.0, slope=0.3, amp=8.0, period=16):
    out = []
    for i in range(n):
        ph, half = i % period, period / 2
        out.append(base + slope * i + amp * (ph / half if ph < half else (period - ph) / half))
    return out


def triangle(n=70, top=120.0, low0=100.0, rise=0.25, period=12):
    out = []
    for i in range(n):
        lo, ph, half = low0 + rise * i, i % period, period / 2
        out.append(lo + (top - lo) * (ph / half if ph < half else (period - ph) / half))
    return out


def loose():
    from backend.signals.chart_patterns import settings
    return settings().model_copy(update={"max_stop_atr": 10})


class PatternTests(unittest.TestCase):
    def test_rising_channel_bounce(self):
        from backend.signals.chart_patterns import find_patterns
        sigs = find_patterns(bars_from(zigzag(90)))
        self.assertTrue(sigs)
        s = sigs[0]
        self.assertEqual((s["pattern"], s["direction"]), ("trendline_bounce", "long"))
        self.assertGreaterEqual(s["detail"]["touches"], 3)
        self.assertTrue(s["detail"]["channel"])
        self.assertGreater(s["target_r"], 2)                     # aims at the upper channel line
        self.assertLess(s["stop"], s["detail"]["line"])
        # cooldown: never two of the same pattern+direction within 10 bars
        longs = [x["s"] for x in sigs if x["pattern"] == "trendline_bounce" and x["direction"] == "long"]
        self.assertTrue(all(b - a >= 10 for a, b in zip(longs, longs[1:])))

    def test_point_in_time(self):
        from backend.signals.chart_patterns import find_patterns
        bars = bars_from(zigzag(90))
        first = find_patterns(bars)[0]
        again = find_patterns(bars[:first["s"] + 1])
        self.assertEqual(again[-1]["s"], first["s"])
        self.assertFalse([x for x in find_patterns(bars[:first["s"]]) if x["s"] == first["s"]])

    def test_falling_channel_short(self):
        from backend.signals.chart_patterns import find_patterns
        sigs = find_patterns(bars_from([300 - x for x in zigzag(90)]))
        self.assertEqual({(s["pattern"], s["direction"]) for s in sigs}, {("trendline_bounce", "short")})
        self.assertGreater(sigs[0]["stop"], sigs[0]["close"])

    def test_ascending_triangle_breakout_needs_volume(self):
        from backend.signals.chart_patterns import find_patterns
        t = triangle()
        closes = t + [t[-1] + 1, 123.5]
        with_volume = find_patterns(bars_from(closes, [1000] * (len(closes) - 1) + [3000]), loose())
        hits = [s for s in with_volume if s["pattern"] == "triangle_breakout"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["direction"], "long")
        self.assertEqual(hits[0]["s"], len(closes) - 1)
        self.assertGreaterEqual(hits[0]["detail"]["flat_touches"], 2)
        no_volume = find_patterns(bars_from(closes), loose())
        self.assertFalse([s for s in no_volume if s["pattern"] == "triangle_breakout"])

    def test_falling_trendline_breakout(self):
        from backend.signals.chart_patterns import find_patterns
        d = [300 - x for x in zigzag(80)]
        closes = d + [d[-1] + 0.3, d[-1] + 5]
        sigs = find_patterns(bars_from(closes, [1000] * (len(closes) - 1) + [3000]), loose())
        hits = [s for s in sigs if s["pattern"] == "trendline_break"]
        self.assertEqual([(s["direction"], s["s"]) for s in hits], [("long", len(closes) - 1)])
        self.assertGreaterEqual(hits[0]["detail"]["touches"], 3)

    def test_stop_too_wide_is_skipped(self):
        from backend.signals.chart_patterns import find_patterns
        t = triangle()
        closes = t + [t[-1] + 1, 123.5]
        sigs = find_patterns(bars_from(closes, [1000] * (len(closes) - 1) + [3000]))   # default 3 ATR limit
        self.assertFalse([s for s in sigs if s["pattern"] == "triangle_breakout"])


class PatternScanTests(IsolatedStateTestCase, unittest.TestCase):
    def test_scan_alerts_new_once_and_study(self):
        from backend.signals import chart_patterns
        closes = zigzag(90)
        bars = bars_from(closes)
        first = chart_patterns.find_patterns(bars)[-1]["s"]
        live = bars[:first + 1]
        start = date(2026, 11, 22) - timedelta(days=len(live) - 1)
        live = bars_from(closes[:first + 1], start=start)
        inst = [{"symbol": "NIFTY", "segment": "index", "token": 1}]
        sent = []
        with mock.patch("backend.signals.divergence.instruments", return_value=inst), \
                mock.patch("backend.marketdata.candles.get_candles", return_value=live):
            r1 = chart_patterns.run_scan("nse", now=datetime(2026, 11, 22, 16), notify=lambda n, g: sent.append(n) or "sent")
            r2 = chart_patterns.run_scan("nse", now=datetime(2026, 11, 22, 17), notify=lambda n, g: sent.append(n) or "sent")
        self.assertEqual((r1["new"], r2["new"], len(sent)), (1, 0, 1))
        row = chart_patterns.list_signals()[0]
        self.assertEqual((row["pattern"], row["direction"], row["alert_status"]), ("trendline_bounce", "long", "sent"))
        self.assertTrue(row["detail"]["channel"])
        text = chart_patterns.build_alert([{**sent[0][0]}], "nse", history="")
        self.assertIn("Trendline bounce long", text)
        self.assertIn("Information only", text)
        result = chart_patterns.study([({"symbol": "A", "segment": "stock"}, bars)])
        self.assertGreater(result["signals"], 0)
        self.assertIn("trendline_bounce long", result["groups"])
        self.assertIn("trendline_bounce long | channel", result["groups"])

    def test_api(self):
        with fresh_test_client() as client:
            login(client)
            body = client.get("/api/chart-patterns").json()
            self.assertEqual(body["signals"], [])
            self.assertEqual(client.get("/api/chart-patterns/study").json()["latest"], {})


def daily(prices, start=date(2026, 10, 1)):
    """prices: list of (open, high, low, close)."""
    out, d = [], start
    for o, h, l, c in prices:
        out.append({"ts": d.isoformat(), "open": o, "high": h, "low": l, "close": c})
        d += timedelta(days=1)
    return out


class IdeaEvaluateTests(unittest.TestCase):
    def idea(self, **kw):
        base = {"direction": "long", "entry_type": "limit", "entry": 100.0, "invalid": 95.0, "target": 110.0,
                "horizon_days": 10}
        return {**base, **kw}

    def test_limit_fill_then_target(self):
        from backend.ideas import evaluate
        bars = daily([(103, 104, 101, 102), (101, 102, 99, 100.5), (101, 106, 100, 105), (105, 111, 104, 110)])
        r = evaluate(self.idea(), bars)
        self.assertEqual((r["status"], r["fill_price"], r["exit_reason"], r["result_r"]), ("closed", 100.0, "target", 2.0))

    def test_stop_first_and_gap(self):
        from backend.ideas import evaluate
        r = evaluate(self.idea(), daily([(100, 101, 94, 96)]))                 # fill and stop on the same day
        self.assertEqual((r["exit_reason"], r["result_r"]), ("invalid", -1.0))
        r = evaluate(self.idea(), daily([(100, 101, 99, 100.5), (90, 91, 89, 90)]))   # gap through the level
        self.assertEqual((r["exit_reason"], r["exit_price"], r["result_r"]), ("invalid", 90, -2.0))

    def test_breakout_short_not_filled_and_open(self):
        from backend.ideas import evaluate
        short = self.idea(direction="short", entry_type="breakout", entry=100.0, invalid=105.0, target=90.0)
        never = daily([(102, 103, 101, 102)] * 5)
        self.assertEqual(evaluate(short, never)["status"], "not_filled")
        self.assertEqual(evaluate(short, never[:2])["status"], "waiting")
        r = evaluate(short, daily([(101, 101.5, 99, 99.5), (99, 100, 97, 98)]))
        self.assertEqual((r["status"], r["fill_price"]), ("open", 100.0))
        self.assertGreater(r["mfe_r"], 0)

    def test_market_time_exit(self):
        from backend.ideas import evaluate
        idea = self.idea(entry_type="market", entry=None, target=None, horizon_days=3)
        r = evaluate(idea, daily([(100, 101, 99, 100)] + [(100, 103, 99.5, 102)] * 4))
        self.assertEqual((r["status"], r["exit_reason"], r["result_r"]), ("closed", "time", 0.4))

    def test_validation(self):
        from backend.ideas import validate
        with self.assertRaises(ValueError):
            validate({"symbol": "../etc", "direction": "long", "invalid": 1})
        with self.assertRaises(ValueError):
            validate({"symbol": "UNOMINDA", "direction": "long", "entry": 100, "invalid": 105})
        ok = validate({"symbol": "m&m.ns", "direction": "short", "entry": 100, "invalid": 105})
        self.assertEqual((ok["symbol"], ok["entry_type"], ok["pattern"]), ("M&M", "limit", "other"))


class IdeaStoreTests(IsolatedStateTestCase, unittest.TestCase):
    def test_add_refresh_record_and_week_summary(self):
        from backend import ideas
        bars = daily([(103, 104, 101, 102), (101, 102, 99, 100.5), (101, 106, 100, 105), (105, 111, 104, 110)],
                      start=date(2026, 11, 2))
        fetch = lambda symbol, since: bars   # noqa: E731
        a = ideas.add_idea({"symbol": "UNOMINDA", "direction": "long", "entry": 100, "invalid": 95, "target": 110,
                            "source": "@payal_trustable", "pattern": "trendline_bounce", "idea_date": "2026-11-01"},
                           fetch=fetch)
        self.assertEqual((a["status"], a["result_r"]), ("closed", 2.0))
        b = ideas.add_idea({"symbol": "POLYCAB", "direction": "short", "entry_type": "market", "invalid": 120,
                            "source": "@payal_trustable", "idea_date": "2026-11-01"}, fetch=lambda s, d: [])
        self.assertEqual(b["status"], "no_data")             # a week old and no prices
        rec = ideas.record()
        self.assertEqual(rec["by_source"]["@payal_trustable"]["n"], 1)
        self.assertEqual(rec["counts"]["no_data"], 1)
        week = ideas.week_summary(date(2026, 11, 2), date(2026, 11, 8))
        self.assertEqual([r["symbol"] for r in week["closed_this_week"]], ["UNOMINDA"])
        self.assertTrue(ideas.delete_idea(b["id"]))

    def test_api(self):
        with fresh_test_client() as client:
            login(client)
            with mock.patch("backend.ideas.daily_bars", return_value=[]):
                r = client.post("/api/ideas", json={"symbol": "ASTERDM", "direction": "long", "entry": 700,
                                                    "invalid": 650, "source": "@Thekalal", "pattern": "trendline_bounce"},
                                headers=csrf_headers(client))
            self.assertEqual(r.status_code, 200, r.text)
            bad = client.post("/api/ideas", json={"symbol": "ASTERDM", "direction": "long", "entry": 700, "invalid": 750},
                              headers=csrf_headers(client))
            self.assertEqual(bad.status_code, 400)
            body = client.get("/api/ideas").json()
            self.assertEqual(body["ideas"][0]["symbol"], "ASTERDM")
            self.assertIn("trendline_bounce", body["patterns"])
            self.assertEqual(client.delete(f"/api/ideas/{r.json()['id']}", headers=csrf_headers(client)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
