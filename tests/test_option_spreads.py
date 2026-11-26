"""Index credit spreads: construction, sizing, exits, EOD backtest and live run (synthetic chains, no network)."""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from backend.options import spreads
from tests._support import IsolatedStateTestCase


# These tests cover the end-of-day rules (mode "eod", backtests and research): pin the
# NIFTY entry window they were written for (Wed–Fri, 4–6 days; live moved to 7–13 days on 24 Nov 2026).
EOD_NIFTY = {"weekdays": [2, 3, 4], "min_days": 4, "max_days": 6}
_eod_patch = None


def _eod_settings(real=spreads.settings):
    cfg = real()
    return {**cfg, "mode": "eod", "symbols": {**cfg["symbols"], "NIFTY": {**cfg["symbols"]["NIFTY"], **EOD_NIFTY}}}


def setUpModule():
    global _eod_patch
    _eod_patch = mock.patch.object(spreads, "settings", _eod_settings)
    _eod_patch.start()


def tearDownModule():
    _eod_patch.stop()

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


def debit_chain(day, near_px=120.0, far_px=50.0):
    """A chain with a near-the-money call (delta 0.50) for bull call spreads."""
    return chain(day) + [row(day, 25900, "CE", near_px, 0.50), row(day, 26100, "CE", far_px, 0.30)]


class DebitSpreadTests(unittest.TestCase):
    """Bull call / bear put debit spreads (asked 24 Nov 2026)."""
    def setUp(self):
        self.cfg = spreads.settings()

    def test_bull_call_build_size_exits(self):
        bc, why = spreads.build(debit_chain("2025-11-19"), "bull_call", RULE, self.cfg)
        self.assertTrue(bc, why)
        self.assertEqual([(l["strike"], l["side"]) for l in bc["legs"]], [(25900, "long"), (26100, "short")])
        self.assertEqual(bc["credit"], 70.0)                     # the debit paid
        sp = {**bc, "lot_size": 65, "lots": 1}
        self.assertEqual(spreads.value(bc["legs"], {(25900, "CE"): 200.0, (26100, "CE"): 60.0}, debit=True), 140.0)
        self.assertEqual(spreads.exit_check(sp, 140.0, "2025-11-20", "2025-11-24", self.cfg), "take_profit")  # +100%
        self.assertEqual(spreads.exit_check(sp, 35.0, "2025-11-20", "2025-11-24", self.cfg), "stop")         # −50%
        self.assertIsNone(spreads.exit_check(sp, 80.0, "2025-11-20", "2025-11-24", self.cfg))
        self.assertEqual(spreads.pnl(sp, 35.0, self.cfg)["pnl_r"], -1.0)     # 1R = half the debit
        self.assertEqual(spreads.pnl(sp, 140.0, self.cfg)["pnl_r"], 2.0)
        cfg = {**self.cfg, "capital": 1_000_000, "risk_per_trade_pct": 1.0, "max_loss_pct": 3.0}
        self.assertEqual(spreads.size(70, 200, 65, cfg, "bull_call"), (4, ""))   # 1R ₹2,275/lot; worst ₹4,550/lot

    def test_debit_too_expensive(self):
        built, why = spreads.build(debit_chain("2025-11-19", 160, 40), "bull_call", RULE, self.cfg)
        self.assertIsNone(built)
        self.assertIn("debit", why)

    def test_one_sided_credit_minimum(self):
        cfg = {**self.cfg, "min_credit_ratio_single": 0.10}
        built, why = spreads.build(chain("2025-11-19", 30, 10), "bull_put", RULE, cfg)   # credit 20 = 10% of 200
        self.assertTrue(built, why)


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

    def test_compare_min_credit(self):
        # asked 26 Nov 2026: the same rules at several minimum credits, with costs
        from backend.options.spread_research import compare_min_credit
        res = compare_min_credit(date(2025, 11, 17), date(2025, 11, 21), [0.20, 0.15], symbols=["NIFTY"])
        by = {r["ratio"]: r for r in res["ratios"]}
        self.assertEqual(by[0.15]["singles"]["trades"], 1)                    # credit 33 on 200 = 16.5%
        self.assertEqual(by[0.2]["singles"]["trades"], 0)
        self.assertEqual(by[0.15]["added_vs"], 0.2)
        self.assertEqual(by[0.15]["added"]["trades"], 1)
        t = by[0.15]["added_trades"][0]
        self.assertEqual((t["strategy"], t["credit_pct"]), ("bull_put", 16.5))
        self.assertLess(t["pnl_r"], 18 / 33)                                   # costs come off
        self.assertGreater(by[0.15]["singles"]["avg_cost_points"], 0)

    def test_compare_strategies_keeps_a_book_each(self):
        from backend.db import get_db
        with get_db() as conn:
            conn.execute("DELETE FROM option_eod")
        day2 = chain("2025-11-20", 20, 5) + [row("2025-11-20", 25900, "CE", 260, 0.80),
                                             row("2025-11-20", 26100, "CE", 120, 0.55)]
        _store(debit_chain("2025-11-19") + day2)
        res = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"],
                               strategies=["bull_put", "bull_call", "iron_condor"])
        got = sorted((t["strategy"], t["exit_reason"]) for t in res["trades"])
        self.assertEqual(got, [("bull_call", "take_profit"), ("bull_put", "take_profit")])   # no condor in STRONG_UP
        self.assertEqual(set(res["by_strategy"]), {"bull_put", "iron_condor", "bull_call"})
        self.assertEqual(res["funnel"]["NIFTY"]["opened"], 3)            # a new bull call the day the first closed
        self.assertEqual(res["skipped"].get("still open at the end"), 1)
        self.assertEqual(res["funnel"]["NIFTY"]["entry_days"], 2)

    def test_low_vix_buys_a_debit_spread(self):
        """Asked 24 Nov 2026: VIX below 11 → bull call / bear put; otherwise bull put / bear call."""
        from backend.db import get_db
        from backend.options import store
        with get_db() as conn:
            conn.execute("DELETE FROM option_eod")
        _store(debit_chain("2025-11-19"))
        cfg = spreads.settings()
        get = (lambda day, e: store.chain("NIFTY", day, e)), (lambda day: ["2025-11-25"])
        low = spreads.candidate("NIFTY", date(2025, 11, 19), cfg, *get, "STRONG_UP", 10.2)
        high = spreads.candidate("NIFTY", date(2025, 11, 19), cfg, *get, "STRONG_UP", 14.0)
        side = spreads.candidate("NIFTY", date(2025, 11, 19), cfg, *get, "SIDEWAYS", 10.2)
        self.assertEqual((low["strategy"], high["strategy"], side["strategy"]), ("bull_call", "bull_put", "iron_condor"))
        self.assertTrue(low["ok"], low["reason"])
        self.assertEqual(spreads.by_vix("bear_call", 9.0, cfg), "bear_put")
        self.assertEqual(spreads.by_vix("bear_call", None, cfg), "bear_call")          # unknown VIX → credit
        self.assertEqual(spreads.by_vix("bear_call", 9.0, {**cfg, "debit_below_vix": None}), "bear_call")
        alert = spreads.build_alert({**low, "vix": 10.2}, None, "approved")
        self.assertIn("Bull call spread", alert)
        self.assertIn("Debit 70.0 pts (you pay)", alert)
        prompt = spreads.build_prompt({**low, "vix": 10.2})
        self.assertIn("option debit spread", prompt)
        self.assertIn('"debit": 70.0', prompt)
        with mock.patch("backend.options.store.vix_on", return_value=10.0):
            res = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"])
            off = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"], debit_below_vix=0)
        self.assertEqual(res["funnel"]["NIFTY"]["opened"], 1)
        self.assertIn("bull_call", res["by_strategy"])
        self.assertNotIn("bull call: credit", " ".join(res["skipped"]))
        self.assertIsNone(off["options"]["debit_below_vix"])

    def test_condor_in_unstable(self):
        with mock.patch.object(spreads, "_regime_on", return_value="UNSTABLE"):
            off = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"])
            on = spreads.backtest(date(2025, 11, 17), date(2025, 11, 21), ["NIFTY"], condor_in_unstable=True)
        self.assertEqual(off["trades"], [])
        self.assertIn("no spread in a UNSTABLE regime", off["skipped"])
        self.assertEqual(on["funnel"]["NIFTY"]["opened"], 1)          # a condor opened (still open at the end)
        self.assertEqual(on["skipped"].get("still open at the end"), 1)

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
