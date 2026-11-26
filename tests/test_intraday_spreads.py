"""Moderate regime tier, multi-timeframe spread entry (daily → 4H → 1H → 15m), the live
intraday scan / 1H follow and their scheduler jobs (agreed 24 Nov 2026). Synthetic bars, fake
Kite client, no network."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from backend.options import spread_mtf as mtf
from backend.options import spreads
from backend.signals import regime as rg
from tests._support import IsolatedStateTestCase

CFG = dict(mtf.DEFAULTS)


def bars(closes, start="2026-11-02T09:15:00", minutes=60, wick=5.0):
    t = datetime.fromisoformat(start)
    out, prev = [], closes[0]
    for c in closes:
        o = prev
        out.append({"ts": t.isoformat(timespec="seconds"), "open": o, "high": max(o, c) + wick,
                    "low": min(o, c) - wick, "close": c, "volume": 0})
        prev = c
        t += timedelta(minutes=minutes)
    return out


def daily(n=260, step=10.0):
    start = date(2025, 6, 2)
    out = []
    for i in range(n):
        c = 20000 + step * i + (15 if i % 2 else -15)
        out.append({"ts": (start + timedelta(days=i)).isoformat() + "T00:00:00", "open": c - 5,
                    "high": c + 40, "low": c - 40, "close": c, "volume": 0})
    return out


class ModerateRegimeTests(unittest.TestCase):
    def label(self, adx_value, moderate_min=20):
        cfg = rg._cfg().model_copy(update={"adx_moderate_min": moderate_min})
        with mock.patch.object(rg, "adx", lambda b, n: [adx_value] * len(b)), \
                mock.patch.object(rg, "_cfg", return_value=cfg):
            return rg.current_regime(daily())["regime"]

    def test_tiers(self):
        self.assertEqual(self.label(30), "STRONG_UP")
        self.assertEqual(self.label(22), "MODERATE_UP")
        self.assertEqual(self.label(22, None), "UNSTABLE")       # tier off: old behaviour
        self.assertEqual(self.label(15), "SIDEWAYS")

    def test_strong_moderate_flips_are_not_instability(self):
        self.assertEqual(rg._changes(["STRONG_UP", "MODERATE_UP", "STRONG_UP", "MODERATE_UP"]), 0)
        self.assertEqual(rg._changes(["STRONG_UP", "SIDEWAYS", "MODERATE_UP"]), 2)
        self.assertEqual((rg.direction("MODERATE_DOWN"), rg.direction("SIDEWAYS")), ("down", None))

    def test_live_permissions(self):
        from backend.decision.config import get_config
        g = get_config().global_
        self.assertEqual(g.regime.adx_moderate_min, 20)
        self.assertEqual(g.permissions["MODERATE_UP"], {})
        self.assertEqual(g.permissions["MODERATE_DOWN"]["pullback"].directions, ["short"])
        self.assertEqual(g.permissions["MODERATE_DOWN"]["breakout"].directions, ["short"])


class TimeframeTests(unittest.TestCase):
    def test_h4_confirm(self):
        up = bars([20000 + 20 * i + (30 if i % 6 < 3 else -30) for i in range(80)], minutes=240)
        ok, why = mtf.h4_confirm(up, "up", CFG)
        self.assertTrue(ok, why)
        self.assertFalse(mtf.h4_confirm(up, "down", CFG)[0])
        self.assertFalse(mtf.h4_confirm(up[:30], "up", CFG)[0])          # too few bars for EMA50

    def test_h1_pullback_gives_the_invalidation_level(self):
        closes = [20000 + 15 * i for i in range(70)]
        closes += [closes[-1] - d for d in (60, 120, 170, 150, 110, 80)]
        setup, why = mtf.h1_setup(bars(closes), "up", CFG)
        self.assertEqual(setup["kind"], "pullback", why)
        self.assertEqual(setup["invalid"], min(b["low"] for b in bars(closes)[-6:]))
        self.assertIsNone(mtf.h1_setup(bars(closes), "down", CFG)[0])

    def test_m15_trigger_fires_once(self):
        closes = [20000 + 5 * i for i in range(30)] + [20140, 20130, 20125, 20200]
        ok, why = mtf.m15_trigger(bars(closes, minutes=15, wick=2), "up", CFG)
        self.assertTrue(ok, why)
        ok, why = mtf.m15_trigger(bars(closes + [20260], minutes=15, wick=2), "up", CFG)
        self.assertFalse(ok)
        self.assertIn("previous bar", why)

    def test_range_rejection(self):
        closes = [20000 + (60 if i % 4 < 2 else -60) for i in range(31)]
        b = bars(closes, wick=10)
        b.append({"ts": "2026-11-04T10:15:00", "open": 20060, "high": 20075, "low": 19990, "close": 20000})
        setup, why = mtf.range_rejection(b, CFG)
        self.assertEqual(setup["side"], "top", why)
        self.assertTrue(mtf.invalidated(setup["invalid"], "range", setup["invalid"]["above"] + 1))
        self.assertFalse(mtf.invalidated(setup["invalid"], "range", 20000))

    def test_evaluate_needs_every_timeframe(self):
        h4 = bars([20000 + 20 * i + (30 if i % 6 < 3 else -30) for i in range(80)], minutes=240)
        h1c = [20000 + 15 * i for i in range(70)]
        h1c += [h1c[-1] - d for d in (60, 120, 170, 150, 110, 80)]
        m15c = [20000 + 5 * i for i in range(30)] + [20140, 20130, 20125, 20200]
        m15 = bars(m15c, start="2026-11-25T02:15:00", minutes=15, wick=2)   # last bar 10:30–10:45 today
        now = datetime(2026, 11, 25, 11, 0)
        ev = mtf.evaluate("MODERATE_UP", bars(h1c), h4, m15, now, CFG)
        self.assertEqual(ev["signal"]["direction"], "up", ev)
        self.assertEqual(ev["signal"]["kind"], "pullback")
        self.assertIsNone(mtf.evaluate("STRONG_DOWN", bars(h1c), h4, m15, now, CFG)["signal"])
        self.assertIsNone(mtf.evaluate("UNSTABLE", bars(h1c), h4, m15, now, CFG)["signal"])
        # 26 Nov 2026: the window applies to the trigger bar's end, not the clock
        m15_1445 = bars(m15c, start="2026-11-25T06:15:00", minutes=15, wick=2)   # last bar 14:30–14:45
        on_time = mtf.evaluate("MODERATE_UP", bars(h1c), h4, m15_1445, datetime(2026, 11, 25, 14, 47), CFG)
        self.assertEqual(on_time["signal"]["direction"], "up", on_time)
        m15_1500 = bars(m15c, start="2026-11-25T06:30:00", minutes=15, wick=2)   # last bar 14:45–15:00
        late = mtf.evaluate("MODERATE_UP", bars(h1c), h4, m15_1500, datetime(2026, 11, 25, 15, 2), CFG)
        self.assertTrue(late["reason"].startswith("outside the entry window"), late)
        stale = mtf.evaluate("MODERATE_UP", bars(h1c), h4, m15, datetime(2026, 11, 26, 10, 0), CFG)
        self.assertIsNone(stale["signal"])                                       # yesterday's bar never triggers
        self.assertTrue(mtf.invalidated(ev["signal"]["invalid"], "up", ev["signal"]["invalid"] - 1))

    def test_completed_bars(self):
        h1 = bars([1, 2, 3], start="2026-11-25T13:15:00")
        self.assertEqual(len(mtf.completed(h1, 60, datetime(2026, 11, 25, 15, 15))), 2)
        self.assertEqual(mtf.bar_end(h1[2], 60), datetime(2026, 11, 25, 15, 30))   # cut at the close


class StrategyChoiceTests(unittest.TestCase):
    def test_iv_switch_and_moderate(self):
        cfg = {**spreads.settings(), "debit_below_iv_pct": 30, "debit_below_vix": 11}
        self.assertEqual(spreads.base_strategy("MODERATE_DOWN", cfg), "bear_call")
        self.assertEqual(spreads.by_iv("bull_put", 12.0, 20.0, cfg), "bull_call")
        self.assertEqual(spreads.by_iv("bull_put", 55.0, 9.0, cfg), "bull_put")
        self.assertEqual(spreads.by_iv("bull_put", None, 9.0, cfg), "bull_call")       # no IV history → VIX
        self.assertEqual(spreads.by_iv("iron_condor", 5.0, 9.0, cfg), "iron_condor")


def live_rows(spot=26000.0):
    """A small live chain: bull put legs 25500/25300 PE, bull call legs, a few calls."""
    def r(strike, t, mid, delta, sym):
        return {"symbol": "NIFTY", "expiry": "2026-12-08", "strike": strike, "opt_type": t, "close": mid,
                "settle": None, "bid": mid - 0.5, "ask": mid + 0.5, "oi": 5000, "delta": delta, "iv": 0.12,
                "forward": spot, "tradingsymbol": sym, "exchange": "NFO"}
    return [r(25500, "PE", 45.0, -0.17, "NIFTY26DEC25500PE"), r(25300, "PE", 12.0, -0.07, "NIFTY26DEC25300PE"),
            r(26000, "CE", 160.0, 0.5, "NIFTY26DEC26000CE"), r(26200, "CE", 80.0, 0.33, "NIFTY26DEC26200CE"),
            r(26000, "PE", 150.0, -0.5, "NIFTY26DEC26000PE")]


class FakeKite:
    def __init__(self, prices):
        self.prices = prices

    def quote(self, keys):
        return {k: {"last_price": self.prices.get(k), "depth": {"buy": [{"price": self.prices[k] - 0.5}],
                                                               "sell": [{"price": self.prices[k] + 0.5}]}}
                for k in keys if k in self.prices}


class LiveBookTests(IsolatedStateTestCase, unittest.TestCase):
    NOW = datetime(2026, 11, 25, 11, 0)

    def setUp(self):
        super().setUp()
        sig = {"regime": "STRONG_UP", "bias": "up", "h4": {"ok": True}, "h1": {"ok": True}, "m15": {"ok": True},
               "signal": {"direction": "up", "kind": "pullback", "invalid": 25800.0,
                          "signal_ts": "2026-11-25T10:45:00", "trigger_close": 26010.0}}
        none = {"regime": "STRONG_UP", "signal": None, "reason": "waiting for the 15m trigger"}
        self.patches = [
            mock.patch.object(spreads, "_regime_on", return_value="STRONG_UP"),
            mock.patch.object(spreads, "_blocked", return_value=None),
            mock.patch.object(spreads, "trend_bars", return_value={"h1": [], "h4": [], "m15": []}),
            mock.patch("backend.options.spread_mtf.evaluate",
                       side_effect=lambda regime, *a, **k: dict(sig) if self.fire else dict(none)),
            mock.patch("backend.marketdata.instruments.option_expiries", return_value=["2026-12-01", "2026-12-08"]),
            mock.patch.object(spreads, "live_chain", return_value=(live_rows(), 26000.0, 13.0)),
            mock.patch.object(spreads, "iv_percentile", return_value=45.0),
            mock.patch.object(spreads, "_send", return_value="sent"),
        ]
        for p in self.patches:
            p.start()
        self.fire = True

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def llm(self, prompt):
        self.prompt = prompt
        return json.dumps({"decision": "approve", "confidence": 80, "reasons": ["trend"], "risks": []}), "m"

    def test_scan_then_follow_on_1h_closes(self):
        cfg = spreads.settings()
        self.assertEqual((cfg["mode"], cfg["symbols"]["NIFTY"]["min_days"]), ("intraday", 7))
        res = spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertIn("NIFTY", res["new"], res)
        sp = [s for s in spreads.spreads("open") if s["symbol"] == "NIFTY"][0]
        self.assertEqual((sp["strategy"], sp["expiry"], sp["setup"], sp["invalid"]),
                         ("bull_put", "2026-12-08", "pullback", 25800.0))     # next weekly, 13 days out
        self.assertEqual(sp["legs"][0]["tradingsymbol"], "NIFTY26DEC25500PE")
        self.assertIn("15-minute trigger", self.prompt)
        again = spreads.scan_intraday(self.NOW + timedelta(minutes=15), client=object(), llm_call=self.llm)
        self.assertEqual(again["results"]["NIFTY"], "spread already open")

        h1 = {"h1": [{"ts": "2026-11-25T11:15:00", "close": 25950.0}], "h4": [], "m15": []}
        kite = FakeKite({"NFO:NIFTY26DEC25500PE": 40.0, "NFO:NIFTY26DEC25300PE": 11.0})
        with mock.patch.object(spreads, "trend_bars", return_value=h1):
            out = spreads.follow_intraday(datetime(2026, 11, 25, 12, 17), client=kite)
        self.assertEqual(out["closed"], [])
        self.assertEqual(spreads.spreads("open")[0]["last_value"], 29.0)
        h1["h1"][0]["close"] = 25790.0                       # 1H close below the pullback low
        with mock.patch.object(spreads, "trend_bars", return_value=h1):
            out = spreads.follow_intraday(datetime(2026, 11, 25, 13, 17), client=kite)
        self.assertEqual(out["closed"], [{"symbol": "NIFTY", "reason": "invalidated"}])
        done = spreads.spreads("closed")[0]
        self.assertEqual(done["exit_reason"], "invalidated")
        self.assertAlmostEqual(done["pnl_points"], 33.0 - 29.0)

    def test_low_iv_buys_a_debit_spread(self):
        with mock.patch.object(spreads, "iv_percentile", return_value=10.0):
            spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertEqual(spreads.spreads("open")[0]["strategy"], "bull_call")

    def test_wide_legs_are_refused(self):
        rows = live_rows()
        for r in rows:
            r["ask"] = r["close"] + 20
        with mock.patch.object(spreads, "live_chain", return_value=(rows, 26000.0, 13.0)):
            res = spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertIn("bid-ask", res["results"]["NIFTY"])
        self.assertEqual(spreads.spreads("open"), [])

    def test_rbi_before_expiry_opens_at_half_size(self):
        """25 Nov 2026: NIFTY signals were all dropped as 'event before expiry: RBI policy 2026-12-04'."""
        cfg = spreads.settings()
        self.assertEqual(cfg["event_size_mult"], 0.5)
        sent = []
        with mock.patch.object(spreads, "_blocked", return_value="event before expiry: RBI policy 2026-12-04"), \
                mock.patch.object(spreads, "_send", side_effect=lambda text: sent.append(text) or "sent"):
            res = spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertIn("NIFTY", res["new"], res)
        self.assertIn("RBI policy 2026-12-04 before expiry — 0.5× size", res["results"]["NIFTY"])
        sp = [x for x in spreads.spreads("open") if x["symbol"] == "NIFTY"][0]
        self.assertIn("RBI policy 2026-12-04", sp["event_note"])
        full, _ = spreads.size(sp["credit"], sp["width"], sp["lot_size"], cfg, sp["strategy"])
        half, _ = spreads.size(sp["credit"], sp["width"], sp["lot_size"],
                               {**cfg, "risk_per_trade_pct": cfg["risk_per_trade_pct"] / 2,
                                "max_loss_pct": cfg["max_loss_pct"] / 2}, sp["strategy"])
        self.assertEqual(sp["lots"], half)
        self.assertLess(sp["lots"], full)
        self.assertIn("Event risk: RBI policy 2026-12-04", self.prompt)
        self.assertTrue(any("RBI policy 2026-12-04" in t for t in sent))

    def test_unverified_calendar_still_blocks(self):
        with mock.patch.object(spreads, "_blocked", return_value="event calendar not verified to expiry"):
            res = spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertEqual(res["results"]["NIFTY"], "event calendar not verified to expiry")
        self.assertEqual(spreads.spreads("open"), [])

    def test_every_scan_is_logged(self):
        self.fire = False
        spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.fire = True
        spreads.scan_intraday(self.NOW + timedelta(minutes=15), client=object(), llm_call=self.llm, manual=True)
        log = spreads.scan_log(self.NOW.date())
        nifty = [r for r in log if r["symbol"] == "NIFTY"]
        self.assertEqual(len(nifty), 2)
        self.assertEqual(nifty[0]["signal"], 1)                       # newest first
        self.assertIn("shortlisted", nifty[0]["result"])
        self.assertEqual(nifty[1]["result"], "waiting for the 15m trigger")
        self.assertEqual(spreads.scan_log(date(2026, 11, 24)), [])

    def test_no_signal_records_nothing(self):
        self.fire = False
        res = spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        self.assertEqual(res["new"], [])
        self.assertEqual(res["results"]["NIFTY"], "waiting for the 15m trigger")

    def test_evening_run_only_backs_up_the_1h_follow(self):
        spreads.scan_intraday(self.NOW, client=object(), llm_call=self.llm)
        out = spreads.run_eod(self.NOW.date())
        self.assertEqual(out["new"], [])
        self.assertIn("intraday mode", out["note"])


class SchedulerTests(unittest.TestCase):
    def test_15m_sync_then_scan(self):
        from backend import scheduler
        state = {"paused": False, "kite_connected": True, "instruments_fresh": True, "done": {}, "scans_done": {},
                 "spreads_intraday": {"first": datetime.strptime("09:30", "%H:%M").time(),
                                      "last": datetime.strptime("14:45", "%H:%M").time()}}
        now = datetime(2026, 11, 25, 10, 47)
        self.assertIn("candles:NSE:15minute", scheduler.due_jobs(now, state))
        bar = datetime(2026, 11, 25, 10, 45)
        state["done"] = {"candles:NSE:15minute": bar, "candles:NSE:10minute": datetime(2026, 11, 25, 10, 45),
                         "candles:NSE:60minute": datetime(2026, 11, 25, 10, 15),
                         "candles:NSE:day": datetime(2026, 11, 24, 15, 30), "candles:MCX:60minute": datetime(2026, 11, 25, 10, 0),
                         "candles:MCX:day": datetime(2026, 11, 24, 23, 30)}
        jobs = scheduler.due_jobs(now, state)
        self.assertIn("scan:spreads", jobs)
        state["scans_done"] = {"spreads": bar}
        self.assertNotIn("scan:spreads", scheduler.due_jobs(now, state))
        late = datetime(2026, 11, 25, 15, 2)
        state["done"]["candles:NSE:15minute"] = datetime(2026, 11, 25, 15, 0)
        self.assertNotIn("scan:spreads", scheduler.due_jobs(late, state))   # after the last entry bar


if __name__ == "__main__":
    unittest.main()
