"""Hourly futures / MCX scan: hard filters, journal records, tracking and charges."""

import math
import unittest
from datetime import date, datetime, timedelta

from tests._support import IsolatedStateTestCase
from tests.test_signals import make_bars, uptrend
from tradingagents.utils.market_calendar import is_trading_day

NSE_STARTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]


def hourly_bars(closes, last_day=date(2026, 11, 23), last_start="11:15", volume=10_000, oi_start=1_000_000):
    """Hourly NSE bars ending with the bar that starts at ``last_start`` on ``last_day``."""
    slots, d = [], last_day
    while len(slots) < len(closes):
        if is_trading_day(d):
            starts = [s for s in NSE_STARTS if d != last_day or s <= last_start]
            slots = [f"{d.isoformat()}T{s}:00" for s in starts] + slots
        d -= timedelta(days=1)
    slots = slots[-len(closes):]
    bars, prev = [], closes[0]
    for i, (ts, c) in enumerate(zip(slots, closes)):
        hi, lo = max(prev, c) * 1.002, min(prev, c) * 0.998
        bars.append({"date": ts, "open": prev, "high": hi, "low": lo, "close": c, "volume": volume,
                     "oi": int(oi_start * (1 + 0.0005 * i))})
        prev = c
    return bars


def breakout_hourly():
    closes = [100 * 1.0008 ** i * (1 + 0.004 * math.sin(i / 2)) for i in range(380)]
    base = closes[-1]
    closes += [base * (1 + 0.002 * math.sin(i)) for i in range(20)]
    closes.append(base * 1.008)
    bars = hourly_bars(closes)
    bars[-1].update(open=base, high=base * 1.009, low=base * 0.999, volume=40_000, oi=int(bars[-2]["oi"] * 1.03))
    return bars


class FakeQuoteKite:
    def __init__(self, spread=0.05):
        self.spread = spread

    def quote(self, symbols):
        out = {}
        for s in symbols:
            ask = 100 * (1 + self.spread / 100)
            out[s] = {"depth": {"buy": [{"price": 100.0}], "sell": [{"price": ask}]}}
        return out


class HourlyScanTests(IsolatedStateTestCase, unittest.TestCase):
    NOW = datetime(2026, 11, 23, 12, 20)

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
                (501, "NSE", "NSE", "STOCK1", "STOCK1", "EQ", None, 0, 1, 0.05, "2026-11-23"),
                (601, "NFO", "NFO-FUT", "STOCK126DECFUT", "STOCK1", "FUT", "2026-12-29", 0, 250, 0.05, "2026-11-23")])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in
                                     make_bars(uptrend(300, start=20000, step=0.006), end=date(2026, 11, 20),
                                              volume=0)])  # Kite: index spot has no volume
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in
                                  make_bars(uptrend(300, start=50, step=0.004), end=date(2026, 11, 20))])
        save_candles(601, "60minute", breakout_hourly())
        refresh_ban_list(fetch=lambda: "Securities in Ban For Trade Date 23-NOV-2026:\n")
        events.refresh_results(["STOCK1"], fetch=lambda s: date(2027, 1, 20), today=date(2026, 11, 23))

    def scan(self, **kw):
        from backend.signals.fno import run_hourly_scan
        return run_hourly_scan("nse", now=self.NOW, client=kw.pop("client", FakeQuoteKite()), **kw)

    def test_breakout_recorded_with_lots_and_expiry_safe_time_stop(self):
        from backend.journal.store import list_records
        result = self.scan()
        self.assertEqual(result["status"], "ok", result)
        recs = list_records("segment = 'stock_futures'")
        brk = [r for r in recs if r["setup"] == "breakout" and r["direction"] == "long"]
        self.assertTrue(brk, [(r["setup"], r["reject_reason"]) for r in recs])
        rec = brk[0]
        self.assertEqual(rec["signal_ts"], "2026-11-23T11:15:00")
        self.assertEqual(rec["fill_until"], "2026-11-23T14:15:00")
        # No 3-day time stop for futures (agreed 23 Nov 2026): the latest exit is the session
        # before the contract's expiry; the trade otherwise ends at target / stop / trailing stop.
        expiry = date.fromisoformat(rec["extra"]["expiry"])
        self.assertLess(rec["time_stop_date"], expiry.isoformat())
        self.assertEqual(rec["time_stop_date"], "2026-12-28")   # contract expires 29 Dec
        self.assertGreaterEqual(rec["extra"]["lots"], 1)
        self.assertTrue(rec["soft_checks"]["oi_buildup"])

    def test_fail_closed_on_ban_spread_and_quote_errors(self):
        from backend.marketdata.instruments import refresh_ban_list
        from backend.signals.cash_swing import latest_run
        wide = self.scan(client=FakeQuoteKite(spread=0.5))
        self.assertEqual(wide["journal_records"], 0)
        reasons = {c["reject_reason"] for c in latest_run(segment="fno_1h")["candidates"]}
        self.assertTrue(any("spread" in (r or "") for r in reasons), reasons)

        class Broken:
            def quote(self, symbols):
                raise RuntimeError("down")
        self.assertEqual(self.scan(client=Broken())["journal_records"], 0)

        refresh_ban_list(fetch=lambda: "Securities in Ban For Trade Date 23-NOV-2026:\n1,STOCK1\n")
        self.scan()
        rows = latest_run(segment="fno_1h")["candidates"]
        self.assertEqual([r["reject_reason"] for r in rows], ["in F&O ban"])

    def test_no_results_check_for_futures_but_the_date_is_noted(self):
        """25 Nov 2026: ~44 stocks a scan were excluded as 'results date unverified'."""
        from backend.journal.panel import build_alert
        from backend.journal.store import list_records
        self.assertEqual(self.scan()["status"], "ok")
        brk = [r for r in list_records("segment = 'stock_futures'") if r["setup"] == "breakout"]
        self.assertEqual(brk[0]["extra"]["event_note"], "next results 2027-01-20")
        self.assertIn("next results 2027-01-20", build_alert(brk[0], {}, "approved"))

    def test_unknown_results_date_no_longer_excludes(self):
        from backend.journal.store import list_records
        with self.db.get_db() as conn:
            conn.execute("DELETE FROM results_dates")          # previously: "results date not checked" → rejected
        self.assertEqual(self.scan()["status"], "ok")
        brk = [r for r in list_records("segment = 'stock_futures'") if r["setup"] == "breakout"]
        self.assertTrue(brk)
        self.assertEqual(brk[0]["extra"]["event_note"], "next results date not known")

    def test_trend_pullback_is_a_shadow_setup_ranked_on_its_own(self):
        from backend.decision.config import get_config
        from backend.journal.store import list_records
        self.scan()
        recs = {(r["setup"], r["direction"]): r for r in list_records("segment = 'stock_futures'")}
        live = recs[("breakout", "long")]
        self.assertEqual(live["decision"], "shortlisted")                 # the live slot is not taken by the shadow
        tp = [r for (setup, _), r in recs.items() if setup == "trend_pullback"]
        self.assertTrue(tp)
        self.assertEqual(tp[0]["decision"], "shortlisted")                # tracked in its own (shadow) ranking
        self.assertTrue(get_config().global_.is_shadow("stock_futures", "trend_pullback"))

    def test_shadow_positions_do_not_block_live_setups(self):
        """An open shadow ORB / trend-pullback record on STOCK1 must not block a live STOCK1 setup."""
        from backend.journal.store import add_records, list_records
        add_records([{"segment": "stock_futures", "symbol": "STOCK1", "instrument_token": 601, "exchange": "NFO",
                      "signal_date": "2026-11-23", "signal_ts": "2026-11-23T09:55:00", "setup": "orb",
                      "direction": "long", "decision": "shortlisted", "entry": 1.0, "stop": 0.9, "target": 1.2,
                      "entry_date": "2026-11-23", "fill_until": "2026-11-23T15:15:00",
                      "time_stop_date": "2026-11-23", "extra": {}}])
        self.scan()
        brk = [r for r in list_records("segment = 'stock_futures'") if r["setup"] == "breakout"][0]
        self.assertEqual(brk["decision"], "shortlisted")

    def test_skips_last_30_minutes_but_still_tracks(self):
        from backend.signals.fno import run_hourly_scan
        late = run_hourly_scan("nse", now=datetime(2026, 11, 23, 15, 20), client=FakeQuoteKite())
        self.assertEqual(late["status"], "skipped")
        self.assertIn("tracking", late)

    def test_rbi_inside_hold_blocks_only_with_event_block_on(self):
        from unittest import mock
        from backend.signals import events, fno
        fake = {"verified": True, "events": [{"event": "RBI policy", "date": "2026-11-25"}]}
        real = fno._x
        with mock.patch.object(events, "market_events_between", return_value=fake), \
                mock.patch.object(fno, "_x", lambda seg, key, default=None:
                                  True if key == "event_block" else real(seg, key, default)):
            blocked = self.scan()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("RBI policy 2026-11-25", blocked["reason"])

    def test_rbi_note_on_candidates(self):
        from unittest import mock
        from backend.journal.store import list_records
        from backend.signals import events
        fake = {"verified": True, "events": [{"event": "RBI policy", "date": "2026-11-25"}]}
        with mock.patch.object(events, "market_events_between", return_value=fake):
            self.assertEqual(self.scan()["status"], "ok")
        brk = [r for r in list_records("segment = 'stock_futures'") if r["setup"] == "breakout"][0]
        self.assertIn("RBI policy 2026-11-25 inside the holding window", brk["extra"]["event_note"])


class FuturesTrackingTests(IsolatedStateTestCase, unittest.TestCase):
    def test_hourly_tracking_lots_and_futures_charges(self):
        from backend.journal.simulate import futures_charges
        from backend.journal.store import add_records, list_records
        from backend.journal.tracker import track_open_records
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        ensure_marketdata_tables()
        save_candles(601, "60minute", [
            {"date": "2026-11-23T12:15:00", "open": 100.5, "high": 100.6, "low": 99.9, "close": 100.2, "volume": 1, "oi": 1},
            {"date": "2026-11-23T13:15:00", "open": 100.2, "high": 104.5, "low": 100.1, "close": 104.2, "volume": 1, "oi": 1}])
        add_records([{"segment": "stock_futures", "symbol": "STOCK1", "instrument_token": 601, "exchange": "NFO",
                      "signal_date": "2026-11-23", "signal_ts": "2026-11-23T11:15:00", "setup": "breakout",
                      "direction": "long", "regime": "STRONG_UP", "score": 70, "decision": "shortlisted",
                      "entry": 100.0, "stop": 98.0, "target": 104.0, "rr": 2.0, "atr": 1.0,
                      "entry_date": "2026-11-23", "fill_until": "2026-11-23T14:15:00", "time_stop_date": "2026-11-26",
                      "extra": {"value_per_point": 250, "lots": 20}}])
        track_open_records()
        rec = list_records("symbol = 'STOCK1'")[0]
        self.assertEqual((rec["exit_reason"], rec["pnl_r"], rec["qty"]), ("target", 2.0, 20))
        gross = 20 * 250 * 4.0
        self.assertAlmostEqual(rec["pnl_rupees"], gross - rec["charges"], places=1)
        # STT 0.05% on the sell side dominates: ~₹260 on ₹5.2L sold.
        self.assertTrue(300 < futures_charges(500_000, 520_000) < 400, futures_charges(500_000, 520_000))

    def test_journal_v1_table_is_migrated(self):
        with self.db.get_db() as conn:
            conn.execute("""CREATE TABLE trade_journal (id INTEGER PRIMARY KEY, segment TEXT NOT NULL, symbol TEXT NOT NULL,
                instrument_token INTEGER NOT NULL, exchange TEXT NOT NULL, signal_date TEXT NOT NULL, setup TEXT NOT NULL,
                direction TEXT NOT NULL, decision TEXT NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL,
                target REAL NOT NULL, entry_date TEXT NOT NULL, fill_until TEXT NOT NULL, time_stop_date TEXT NOT NULL,
                track_status TEXT NOT NULL DEFAULT 'pending', UNIQUE (segment, symbol, signal_date, setup, direction))""")
            conn.execute("""INSERT INTO trade_journal (segment, symbol, instrument_token, exchange, signal_date, setup,
                direction, decision, entry, stop, target, entry_date, fill_until, time_stop_date)
                VALUES ('cash_swing', 'OLD', 1, 'NSE', '2026-11-20', 'pullback', 'long', 'shortlisted', 1, 0.9, 1.2,
                '2026-11-23', '2026-11-23T11:00', '2026-12-02')""")
        from backend.journal.store import list_records
        rows = list_records()
        self.assertEqual([(r["symbol"], r["signal_ts"]) for r in rows], [("OLD", "2026-11-20T15:30:00")])


class McxEventTests(unittest.TestCase):
    def test_mcx_windows(self):
        from backend.signals.events import mcx_event_block
        self.assertTrue(mcx_event_block("GOLDM", datetime(2026, 12, 10, 19, 0))[0])      # US CPI
        self.assertTrue(mcx_event_block("CRUDEOILM", datetime(2026, 11, 25, 20, 0))[0])  # Wednesday EIA
        self.assertFalse(mcx_event_block("CRUDEOILM", datetime(2026, 11, 25, 11, 0))[0])
        self.assertFalse(mcx_event_block("NATGASMINI", datetime(2026, 11, 25, 20, 0))[0])
        self.assertTrue(mcx_event_block("SILVERM", datetime(2027, 7, 1, 11, 0))[0])     # past verified dates


if __name__ == "__main__":
    unittest.main()
