"""RSI divergence: detection, outcome tracking, live scan + alert, history study, API."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from tests._support import IsolatedStateTestCase, fresh_test_client, login


def bars_from(closes, start=date(2025, 1, 1)):
    out, d = [], start
    for c in closes:
        out.append({"ts": d.isoformat() + "T00:00:00", "open": c, "high": c + 0.5, "low": c - 0.5,
                    "close": c, "volume": 1000, "oi": 0})
        d += timedelta(days=1)
    return out


def bullish_closes(tail=29, tail_step=1.2):
    c = [100 + (i % 2) * 0.3 for i in range(80)]       # flat
    c += [100 - 2.5 * i for i in range(1, 9)]           # sharp fall to 80: first low, RSI deeply oversold
    c += [80 + 1.5 * i for i in range(1, 7)]            # bounce
    c += [89 - 0.6 * i for i in range(1, 18)]           # slow grind to a lower low: RSI higher low
    c += [78.8 + tail_step * i for i in range(1, tail + 1)]
    return c


class DetectionTests(unittest.TestCase):
    def test_bullish_found_at_confirmation_bar(self):
        from backend.signals.divergence import find_divergences
        bars = bars_from(bullish_closes())
        found = find_divergences(bars)
        self.assertEqual(len(found), 1)
        d = found[0]
        self.assertEqual(d["kind"], "bullish")
        self.assertLess(d["pivot2_price"], d["pivot1_price"])
        self.assertGreater(d["pivot2_rsi"], d["pivot1_rsi"])
        self.assertEqual(d["s"], d["i2"] + 2)                 # known 2 bars after the swing
        self.assertTrue(d["trend_ok"])
        self.assertLess(d["stop"], d["pivot2_price"])
        # nothing after the signal bar is needed to find it
        self.assertEqual(find_divergences(bars[:d["s"] + 1])[0]["i2"], d["i2"])
        self.assertEqual(find_divergences(bars[:d["s"]]), [])

    def test_bearish_mirror(self):
        from backend.signals.divergence import find_divergences
        bars = bars_from([200 - c for c in bullish_closes()])
        found = find_divergences(bars)
        self.assertEqual([d["kind"] for d in found], ["bearish"])
        self.assertGreater(found[0]["stop"], found[0]["pivot2_price"])

    def test_trend_filter(self):
        from backend.signals.divergence import find_divergences
        # same shape but far above a long rising base: close stays above the 50 EMA
        closes = [60 + i * 0.5 for i in range(80)] + [c + 40 for c in bullish_closes()[80:]]
        bars = bars_from(closes)
        with_filter = find_divergences(bars)
        without = find_divergences(bars, require_trend=False)
        self.assertTrue(all(d["trend_ok"] for d in with_filter))
        self.assertGreaterEqual(len(without), len(with_filter))

    def test_outcomes(self):
        from backend.signals.divergence import find_divergences, outcome
        bars = bars_from(bullish_closes())
        d = find_divergences(bars)[0]
        win = outcome(bars, d, 20)
        self.assertEqual((win["status"], win["exit_reason"], win["result_r"]), ("closed", "target", 2.0))
        self.assertGreater(win["ret_5"], 0)
        # price breaks the second low after the signal: stopped at -1R
        base = bullish_closes(tail=3, tail_step=0.4)
        d = find_divergences(bars_from(base))[0]
        crash = bars_from(base + [78.0, 76.0, 74.0])
        lost = outcome(crash, d, 20)
        self.assertEqual((lost["exit_reason"], lost["result_r"]), ("stop", -1.0))
        # too few bars after the signal: still open
        short = bars_from(bullish_closes(tail=5, tail_step=0.1))
        d = find_divergences(short)[0]
        self.assertEqual(outcome(short, d, 20)["status"], "open")


class ScanTests(IsolatedStateTestCase, unittest.TestCase):
    def _patches(self, bars, instruments):
        return (mock.patch("backend.signals.divergence.instruments", return_value=instruments),
                mock.patch("backend.marketdata.candles.get_candles", return_value=bars))

    def test_scan_alerts_once_and_tracks(self):
        from backend.signals import divergence
        closes = bullish_closes(tail=2, tail_step=0.4)            # signal bar is the last bar
        bars = bars_from(closes, start=date(2026, 11, 22) - timedelta(days=len(closes) - 1))
        inst = [{"symbol": "NIFTY", "segment": "index", "token": 256265}]
        sent = []
        p1, p2 = self._patches(bars, inst)
        with p1, p2:
            first = divergence.run_scan("nse", now=datetime(2026, 11, 22, 15, 50),
                                        notify=lambda new, group: sent.append(new) or "sent")
            second = divergence.run_scan("nse", now=datetime(2026, 11, 22, 16, 30),
                                         notify=lambda new, group: sent.append(new) or "sent")
        self.assertEqual(first["new"], 1)
        self.assertEqual(first["bullish"], 1)
        self.assertEqual(second["new"], 0)                    # never alerted twice
        self.assertEqual(len(sent), 1)
        row = divergence.list_signals()[0]
        self.assertEqual((row["symbol"], row["kind"], row["status"], row["alert_status"]),
                         ("NIFTY", "bullish", "open", "sent"))

    def test_scan_skips_stale_data_and_pause(self):
        from backend.signals import divergence
        from backend.system_state import set_paused
        bars = bars_from(bullish_closes(tail=2, tail_step=0.4), start=date(2025, 1, 1))   # old data
        p1, p2 = self._patches(bars, [{"symbol": "NIFTY", "segment": "index", "token": 1}])
        with p1, p2:
            result = divergence.run_scan("nse", now=datetime(2026, 11, 22, 15, 50), notify=lambda n, g: "sent")
        self.assertEqual((result["instruments"], result["new"]), (0, 0))
        set_paused(True)
        self.assertEqual(divergence.run_scan("nse")["reason"], "paused")

    def test_alert_text(self):
        from backend.signals import divergence
        bars = bars_from(bullish_closes())
        d = divergence.find_divergences(bars)[0]
        text = divergence.build_alert([{**d, "symbol": "M&M", "segment": "stock"}], "nse", history="History: x")
        self.assertIn("Bullish", text)
        self.assertIn("M&amp;M", text)
        self.assertIn("Information only", text)
        self.assertIn("invalid below", text)

    def test_study(self):
        from backend.signals import divergence
        series = [({"symbol": "A", "segment": "stock"}, bars_from(bullish_closes())),
                  ({"symbol": "B", "segment": "stock"}, bars_from([200 - c for c in bullish_closes()]))]
        result = divergence.study(series)
        self.assertEqual(result["signals"], 2)
        self.assertEqual(result["groups"]["bullish"]["n"], 1)
        self.assertTrue(result["groups"]["bullish"]["small_sample"])
        self.assertIn("bullish | with trend filter", result["groups"])
        self.assertIsNotNone(result["baseline_10d"]["bullish"])

    def test_api(self):
        with fresh_test_client() as client:
            login(client)
            body = client.get("/api/divergence").json()
            self.assertEqual(body["signals"], [])
            self.assertIn("bullish", body["live_record"])
            self.assertEqual(client.get("/api/divergence/study").json(), {})


if __name__ == "__main__":
    unittest.main()


class ExitWarningAndUptrendTests(IsolatedStateTestCase, unittest.TestCase):
    def _bearish_bars(self):
        closes = [200 - c for c in bullish_closes(tail=2, tail_step=0.4)]
        return bars_from(closes, start=date(2026, 11, 22) - timedelta(days=len(closes) - 1))

    def _scan(self, bars, symbol="RELIANCE"):
        from backend.signals import divergence
        sent = []
        inst = [{"symbol": symbol, "segment": "index", "token": 7}]
        with mock.patch("backend.signals.divergence.instruments", return_value=inst), \
                mock.patch("backend.marketdata.candles.get_candles", return_value=bars):
            result = divergence.run_scan("nse", now=datetime(2026, 11, 22, 15, 50),
                                         notify=lambda new, group: sent.append(new) or "sent")
        return result, sent

    def test_bearish_only_alerted_when_held_long(self):
        from backend.signals import divergence
        result, sent = self._scan(self._bearish_bars())
        self.assertEqual((result["bearish"], result["alerted"], sent), (1, 0, []))
        row = divergence.list_signals()[0]
        self.assertEqual(row["alert_status"], "not sent: bearish_alert=open_longs")

    def test_bearish_alerted_as_exit_warning_for_open_long(self):
        from backend import ideas
        from backend.signals import divergence
        ideas.ensure_table()
        with mock.patch("backend.ideas.daily_bars", return_value=[]):
            ideas.add_idea({"symbol": "HDFCBANK", "direction": "long", "entry_type": "market", "invalid": 1500,
                            "idea_date": date.today().isoformat()})
        self.assertIn("HDFCBANK", divergence.open_long_symbols())
        result, sent = self._scan(self._bearish_bars(), symbol="HDFCBANK")
        self.assertEqual((result["alerted"], len(sent)), (1, 1))
        text = divergence.build_alert(sent[0], "nse", history="")
        self.assertIn("Exit warning", text)

    def test_pullback_in_uptrend_context(self):
        from backend.signals import divergence
        prefix = [40 + i * 0.4 for i in range(250)]               # long uptrend 40 -> 140
        shape = [c + 40 for c in bullish_closes()[80:]]           # then the pullback with a divergence
        bars = bars_from(prefix + [140 + (i % 2) * 0.3 for i in range(20)] + shape)
        found = divergence.find_divergences(bars)
        self.assertTrue(found)
        self.assertTrue(found[-1]["uptrend"])
        below = bars_from(bullish_closes())                       # too short for a 200 SMA: not an uptrend
        cfg = divergence.settings().model_copy(update={"bullish_context": "pullback_in_uptrend"})
        self.assertEqual(divergence.find_divergences(below, cfg), [])
        self.assertTrue(divergence.find_divergences(below, cfg, apply_context=False))
        self.assertTrue(divergence.find_divergences(bars, cfg))

    def test_study_reports_uptrend_and_train_test(self):
        from backend.signals import divergence
        old = bars_from(bullish_closes(), start=date(2024, 1, 1))
        new = bars_from(bullish_closes(), start=date(2026, 1, 1))
        result = divergence.study([({"symbol": "A", "segment": "stock"}, old), ({"symbol": "B", "segment": "stock"}, new)])
        self.assertEqual(result["split_date"], "2025-06-01")
        self.assertEqual(result["groups"]["bullish | train"]["n"], 1)
        self.assertEqual(result["groups"]["bullish | test"]["n"], 1)
        self.assertIn("bullish | below 200 SMA | test", result["groups"])
