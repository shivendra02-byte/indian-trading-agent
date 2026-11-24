"""Index credit spreads: construction, sizing, exits, EOD backtest and live run (synthetic chains, no network)."""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from backend.options import spreads
from tests._support import IsolatedStateTestCase

RULE = {"wing": 200, "lot_size": 65, "weekdays": [2, 3, 4], "min_days": 4, "max_days": 6}


def row(day, strike, t, price, delta, expiry="2025-11-25", oi=1000):
    return {"trade_date": day, "exchange": "NSE", "symbol": "NIFTY", "expiry": expiry, "strike": strike,
            "opt_type": t, "close": price, "settle": price, "delta": delta, "oi": oi, "iv": 0.12}


def chain(day, short_px=45.0, long_px=12.0):
    return [row(day, 25700, "PE", 90, -0.32), row(day, 25500, "PE", short_px, -0.17),
            row(day, 25300, "PE", long_px, -0.07), row(day, 26300, "CE", 40, 0.18), row(day, 26500, "CE", 10, 0.06)]


class PureTests(unittest.TestCase):
    def setUp(self):
        self.cfg = spreads.settings()

    def test_entry_window(self):
        exps = ["2025-11-25", "2025-12-02"]
        self.assertEqual(spreads.pick_expiry(date(2025, 11, 19), exps, RULE), "2025-11-25")   # Wed, 6 days
        self.assertIsNone(spreads.pick_expiry(date(2025, 11, 24), exps, RULE))               # Monday
        self.assertIsNone(spreads.pick_expiry(date(2025, 11, 18), exps, RULE))               # Tuesday (expiry day)

    def test_bull_put_and_condor(self):
        bp, why = spreads.build(chain("2025-11-19"), "bull_put", RULE, self.cfg)
        self.assertTrue(bp, why)
        self.assertEqual([(l["strike"], l["side"]) for l in bp["legs"]], [(25500, "short"), (25300, "long")])
        self.assertEqual(bp["credit"], 33.0)
        ic, why = spreads.build(chain("2025-11-19"), "iron_condor", RULE, self.cfg)
        self.assertTrue(ic, why)
        self.assertEqual(ic["credit"], 63.0)
        self.assertEqual(len(ic["legs"]), 4)

    def test_credit_too_small(self):
        built, why = spreads.build(chain("2025-11-19", 30, 10), "bull_put", RULE, self.cfg)
        self.assertIsNone(built)
        self.assertIn("credit", why)

    def test_size_and_exits(self):
        cfg = {**self.cfg, "capital": 1_000_000, "risk_per_trade_pct": 1.0, "max_loss_pct": 3.0}
        self.assertEqual(spreads.size(33, 200, 65, cfg), (2, ""))          # worst case caps it at 2 lots
        sp = {"credit": 33.0, "lot_size": 65, "lots": 2}
        self.assertEqual(spreads.exit_check(sp, 16.0, "2025-11-20", "2025-11-24", cfg), "take_profit")
        self.assertEqual(spreads.exit_check(sp, 66.0, "2025-11-20", "2025-11-24", cfg), "stop")
        self.assertEqual(spreads.exit_check(sp, 30.0, "2025-11-24", "2025-11-24", cfg), "expiry_exit")
        self.assertIsNone(spreads.exit_check(sp, 30.0, "2025-11-20", "2025-11-24", cfg))
        self.assertEqual(spreads.pnl(sp, 66.0, cfg)["pnl_r"], -1.0)

    def test_value(self):
        legs = [{"strike": 25500, "type": "PE", "side": "short"}, {"strike": 25300, "type": "PE", "side": "long"}]
        self.assertEqual(spreads.value(legs, {(25500, "PE"): 20, (25300, "PE"): 5}), 15)
        self.assertIsNone(spreads.value(legs, {(25500, "PE"): 20}))


def _store(rows):
    from backend.db import get_db
    from backend.options.store import ensure_option_tables
    ensure_option_tables()
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO option_eod (trade_date, exchange, symbol, expiry, strike, opt_type, close, settle, delta, oi, iv) "
            "VALUES (:trade_date, :exchange, :symbol, :expiry, :strike, :opt_type, :close, :settle, :delta, :oi, :iv)",
            rows)


class BookTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _store(chain("2025-11-19") + chain("2025-11-20", 20, 5))
        self.patches = [mock.patch.object(spreads, "_regime_on", return_value="STRONG_UP"),
                        mock.patch.object(spreads, "_blocked", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_backtest(self):
        res = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"])
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        self.assertEqual((t["strategy"], t["exit_reason"], t["exit_date"]), ("bull_put", "take_profit", "2025-11-20"))
        self.assertAlmostEqual(t["pnl_r"], 18 / 33, places=3)
        self.assertEqual(res["by_strategy"]["bull_put"]["trades"], 1)

    def test_live_run_reviews_and_follows(self):
        reply = '{"decision": "approve", "confidence": 75, "reasons": ["trend up"], "risks": ["gap"]}'
        with mock.patch.object(spreads, "_send", return_value="sent") as send:
            first = spreads.run_eod(date(2025, 11, 19), llm_call=lambda p: (reply, "test-model"))
            self.assertEqual(first["new"], ["NIFTY"])
            self.assertEqual(first["reviews"][0]["decision"], "approved")
            self.assertIn("Bull put spread", send.call_args[0][0])
            again = spreads.run_eod(date(2025, 11, 19), llm_call=lambda p: (reply, "m"))
            self.assertEqual(again["results"]["NIFTY"], "spread already open")
            second = spreads.run_eod(date(2025, 11, 20), llm_call=lambda p: (reply, "m"))
        self.assertEqual(second["closed"], ["NIFTY"])
        closed = spreads.spreads("closed")[0]
        self.assertEqual(closed["exit_reason"], "take_profit")
        from backend.journal.panel import evaluations_today
        self.assertEqual(evaluations_today(date.fromisoformat(closed["panel"]["evaluated_at"][:10])), 1)

    def test_run_now_on_a_holiday_uses_the_latest_stored_files(self):
        """Asked 24 Nov 2026: "Run now" said "no option file for today" on a holiday."""
        reply = '{"decision": "approve", "confidence": 75, "reasons": ["trend up"], "risks": ["gap"]}'
        with mock.patch.object(spreads, "_send", return_value="sent"):
            out = spreads.run_now(today=date(2025, 11, 22), llm_call=lambda p: (reply, "m"))   # Saturday
        self.assertEqual(out["date"], "2025-11-20")
        self.assertIn("weekend", out["note"])
        self.assertNotEqual(out["results"].get("NIFTY"), "no option file for today")

    def test_run_now_without_files(self):
        from backend.db import get_db
        with get_db() as conn:
            conn.execute("DELETE FROM option_eod")
        self.assertEqual(spreads.run_now(today=date(2025, 11, 22))["status"], "skipped")

    def test_paused(self):
        with mock.patch("backend.system_state.is_paused", return_value=True):
            self.assertEqual(spreads.run_eod(date(2025, 11, 19))["reason"], "paused")


if __name__ == "__main__":
    unittest.main()
