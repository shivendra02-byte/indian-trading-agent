"""Stage analysis, the trend reversal setup (replaces Wolfe), VCP and top-signal exit warnings
(synthetic bars, no network)."""

import unittest
from datetime import date, datetime, timedelta

from backend.decision.config import (available_rule_sets, exit_rule_for, get_config, instrument_or_live,
                                     use_config)
from backend.signals import reversal, stage, vcp
from backend.signals.daily_cash_scan import journal_row


# --- bar builders ------------------------------------------------------------------------------

def _hour(i, o, h, l, c, v=1000):
    return {"ts": (datetime(2026, 1, 1) + timedelta(hours=i)).isoformat(), "open": o, "high": h, "low": l,
            "close": c, "volume": v}


def reversal_bars():
    """A 1H uptrend, a decline below the 200 EMA, a swing low (~111.3), a stretched fall, a hammer
    that sweeps the swing low and closes back above it, then the trigger above the hammer's high."""
    bars = []

    def add(c, wig=0.3, o=None, h=None, l=None, v=1000):
        prev = bars[-1]["close"] if bars else c
        o = prev if o is None else o
        bars.append(_hour(len(bars), o, h if h is not None else max(o, c) + wig,
                          l if l is not None else min(o, c) - wig, c, v))

    for k in range(250):
        add(100 + 30 * k / 249 + (0.4 if k % 4 < 2 else -0.4))
    for k in range(30):
        add(130 - 18 * k / 29 + (0.4 if k % 4 < 2 else -0.4))
    add(112.0); add(111.6); add(112.1, l=111.6)
    for c in (112.6, 113.4, 114.0):
        add(c)
    add(113.6, h=113.9)                               # 114.3 stays a clean swing high
    for c in (112.5, 110.8, 109.2, 107.8, 106.6):
        add(c)
    add(111.6, o=106.6, h=111.9, l=105.8, v=3000)    # the extreme: swing failure, heavy volume
    add(111.2); add(111.7)
    add(112.6)                                        # trigger
    return bars


def mirror(bars, k=250.0):
    return [{**b, "open": k - b["open"], "close": k - b["close"], "high": k - b["low"], "low": k - b["high"]}
            for b in bars]


STAGE2 = {"stage": 2, "label": "Stage 2 (advancing)"}
STAGE4 = {"stage": 4, "label": "Stage 4 (declining)"}


def daily(closes, volumes=None, start=date(2023, 1, 2)):
    return [{"ts": (start + timedelta(days=i)).isoformat() + "T00:00:00", "open": c, "high": c + 0.5,
             "low": c - 0.5, "close": c, "volume": (volumes[i] if volumes else 1000)} for i, c in enumerate(closes)]


def path(vertices):
    out = []
    for (i0, c0), (i1, c1) in zip(vertices, vertices[1:]):
        for i in range(i0, i1):
            out.append(c0 + (c1 - c0) * (i - i0) / (i1 - i0))
    out.append(vertices[-1][1])
    return out


VCP_PATH = [(0, 100), (200, 100.0001), (330, 150), (340, 130), (350, 147), (357, 139), (363, 146), (367, 143),
            (370, 145.5)]


def vcp_bars(vertices=VCP_PATH, breakout=147.5, bvol=3000, base_vol=500):
    closes = path(vertices)
    bars = daily(closes, [1000 if i < 330 else base_vol for i in range(len(closes))], start=date(2024, 1, 1))
    if breakout:
        i = len(bars)
        bars.append({"ts": (date(2024, 1, 1) + timedelta(days=i)).isoformat() + "T00:00:00", "open": 145.5,
                     "high": breakout + 0.3, "low": 145.3, "close": breakout, "volume": bvol})
    return bars


# --- stage analysis ----------------------------------------------------------------------------

class StageTests(unittest.TestCase):
    def test_uptrend_is_stage_2_with_trend_template(self):
        bars = daily(path([(0, 100), (299, 180)]))
        info = stage.classify(bars)
        self.assertEqual(info["stage"], 2)
        self.assertTrue(info["trend_template"]["passed"], info["trend_template"])

    def test_downtrend_is_stage_4(self):
        info = stage.classify(daily(path([(0, 180), (299, 100)])))
        self.assertEqual(info["stage"], 4)
        self.assertFalse(info["trend_template"]["passed"])

    def test_rising_200_but_close_below_is_stage_3(self):
        info = stage.classify(daily(path([(0, 100), (280, 180), (299, 120)])))
        self.assertEqual(info["stage"], 3)

    def test_not_enough_history(self):
        info = stage.classify(daily([100.0] * 150))
        self.assertIsNone(info["stage"])
        ok, why = stage.allows(info, "long", [1, 2], [3, 4])
        self.assertFalse(ok)
        self.assertIn("unknown", why)

    def test_allows(self):
        self.assertTrue(stage.allows(STAGE2, "long", [1, 2], [3, 4])[0])
        self.assertFalse(stage.allows(STAGE4, "long", [1, 2], [3, 4])[0])
        self.assertTrue(stage.allows(STAGE4, "short", [1, 2], [3, 4])[0])

    def test_series_is_point_in_time(self):
        bars = daily(path([(0, 100), (299, 180)]))
        series = stage.stages_series(bars)
        self.assertEqual(series[-1], stage.classify(bars)["stage"])
        self.assertEqual(series[250], stage.classify(bars[:251])["stage"])

    def test_config_validates_sma_order(self):
        from backend.decision.config import StageCfg
        with self.assertRaises(Exception):
            StageCfg(sma_fast=200, sma_mid=150, sma_slow=50)


class TopWarningTests(unittest.TestCase):
    def test_heavy_volume_break_of_the_50_day(self):
        closes = path([(0, 100), (299, 180)])
        vols = [1000] * 300
        closes.append(168.0)            # slices through the 50-day SMA
        vols.append(3000)
        kinds = {w["kind"] for w in stage.top_warnings(daily(closes, vols))}
        self.assertIn("break_50dma", kinds)

    def test_climax_run(self):
        closes = path([(0, 100), (299, 180)])
        for c in (190, 205, 260):       # parabolic: the largest gain of the move, far above the 50-day
            closes.append(c)
        kinds = {w["kind"] for w in stage.top_warnings(daily(closes))}
        self.assertIn("climax_run", kinds)

    def test_sma200_rollover(self):
        bars = daily(path([(0, 100), (200, 180), (420, 90)]))
        days = [t for t in range(260, len(bars))
                if "sma200_rollover" in {w["kind"] for w in stage.top_warnings(bars[:t + 1])}]
        self.assertTrue(days)                         # fires while the 200-day turns down ...
        self.assertLess(days[-1], len(bars) - 1)      # ... not forever after

    def test_quiet_uptrend_has_no_warning(self):
        self.assertEqual(stage.top_warnings(daily(path([(0, 100), (299, 180)]))), [])


# --- reversal ---------------------------------------------------------------------------------

class ReversalTests(unittest.TestCase):
    def setUp(self):
        self.p = reversal.params({"max_stop_atr": 6})
        self.bars = reversal_bars()

    def test_bottom_reversal_passes_every_step(self):
        r = reversal.check(self.bars, "long", self.p, STAGE2)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual([s["stage"] for s in r["steps"]],
                         ["context", "stretch", "exhaustion", "stage", "adx", "trigger"])
        self.assertIsNotNone(r["sfp"])
        self.assertIsNotNone(r["divergence"])
        self.assertLessEqual(r["z"], -2.5)
        self.assertEqual(r["extreme"], 105.8)

    def test_trade_levels(self):
        row = reversal.build(self.bars, "long", self.p, STAGE2)
        self.assertTrue(row["valid"], row.get("reject_reason"))
        self.assertLess(row["stop"], 105.8)
        self.assertAlmostEqual((row["target"] - row["entry"]) / (row["entry"] - row["stop"]), 2.0, places=1)
        self.assertEqual(row["setup"], "reversal")
        self.assertGreater(row["factors"]["effort"], 0)          # 3x volume on the extreme bar
        self.assertEqual(row["score"], sum(row["factors"].values()))

    def test_default_stop_cap_rejects_a_wide_stop(self):
        row = reversal.build(self.bars, "long", reversal.params({}), STAGE2)
        self.assertFalse(row["valid"])
        self.assertIn("stop too wide", row["reject_reason"])

    def test_no_falling_knife_in_stage_4(self):
        r = reversal.check(self.bars, "long", self.p, STAGE4)
        self.assertFalse(r["ok"])
        self.assertEqual(r["steps"][-1]["stage"], "stage")

    def test_unknown_stage_blocks_unless_switched_off(self):
        self.assertFalse(reversal.check(self.bars, "long", self.p, None)["ok"])
        p = {**self.p, "require_stage": False}
        self.assertTrue(reversal.check(self.bars, "long", p, None)["ok"])

    def test_runaway_adx_blocks(self):
        sr = reversal.Series(self.bars, self.p)
        sr.adx[-1], sr.adx[-2] = 40.0, 35.0
        r = reversal.check(self.bars, "long", self.p, STAGE2, series=sr)
        self.assertFalse(r["ok"])
        self.assertIn("runaway", r["reason"])

    def test_one_signal_per_extreme(self):
        bars = self.bars + [_hour(len(self.bars), 112.6, 113.6, 112.3, 113.3)]
        r = reversal.check(bars, "long", self.p, STAGE2)
        self.assertFalse(r["ok"])
        self.assertIn("already triggered", r["reason"])

    def test_waits_before_the_trigger(self):
        r = reversal.check(self.bars[:-1], "long", self.p, STAGE2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["steps"][-1]["stage"], "trigger")

    def test_top_is_the_mirror_image(self):
        bars = mirror(self.bars)
        r = reversal.check(bars, "short", self.p, STAGE4)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertFalse(reversal.check(bars, "short", self.p, STAGE2)["ok"])

    def test_find_all_matches_the_live_check(self):
        found = reversal.find_all(self.bars, self.p, lambda t: STAGE2, directions=("long",))
        self.assertEqual([c["s"] for c in found], [len(self.bars) - 1])
        live = reversal.build(self.bars, "long", self.p, STAGE2)
        self.assertEqual((found[0]["entry"], found[0]["stop"]), (live["entry"], live["stop"]))

    def test_find_all_reports_drop_outs(self):
        # 26 Nov 2026: the backtest funnel gets the reason of every checked bar that failed
        reasons = []
        found = reversal.find_all(self.bars, self.p, lambda t: STAGE2, directions=("long",), on_reject=reasons.append)
        self.assertEqual(len(found), 1)
        self.assertTrue(reasons)
        self.assertTrue(all(isinstance(r, str) and r for r in reasons))

    def test_nifty_regime_filter(self):
        cfg = {**reversal.cash_settings(), "params": self.p, "nifty_regimes": ["STRONG_UP", "SIDEWAYS"]}
        row = reversal.cash_candidate(self.bars, cfg, "STRONG_DOWN")
        self.assertFalse(row["trackable"])
        self.assertIn("NIFTY regime STRONG_DOWN", row["reject_reason"])
        self.assertIsNone(reversal.cash_settings()["nifty_regimes"])           # live: no filter

    def test_reversal_experiments_load(self):
        from backend.decision.config import use_config
        with use_config("config/experiments/reversal_loose_regime.yaml"):
            c = reversal.cash_settings()
        self.assertEqual(c["params"]["z_stretch"], 2.0)
        self.assertIn("STRONG_UP", c["nifty_regimes"])
        with use_config("config/experiments/reversal_nifty_regime.yaml"):
            c = reversal.cash_settings()
        self.assertEqual(c["params"]["z_stretch"], 2.5)

    def test_swing_high_trigger(self):
        p = {**self.p, "trigger": "swing_high"}
        r = reversal.check(self.bars, "long", p, STAGE2)
        self.assertFalse(r["ok"])                   # the last swing high (~114.3) is not broken yet
        self.assertIn("swing high", r["reason"])

    def test_bad_trigger_name(self):
        with self.assertRaises(ValueError):
            reversal.params({"trigger": "close"})


# --- VCP ----------------------------------------------------------------------------------------

class VcpTests(unittest.TestCase):
    def setUp(self):
        self.cfg = vcp.settings()

    def test_detects_three_shrinking_contractions(self):
        p = vcp.detect(vcp_bars(), self.cfg)
        self.assertTrue(p["found"], p.get("reason"))
        self.assertEqual(len(p["contractions"]), 3)
        depths = [c["depth_pct"] for c in p["contractions"]]
        self.assertEqual(depths, sorted(depths, reverse=True))
        self.assertLessEqual(p["dryup_ratio"], 0.8)

    def test_candidate(self):
        c = vcp.candidate(vcp_bars(), self.cfg)
        self.assertTrue(c["valid"], c.get("reject_reason"))
        self.assertLess(c["stop"], c["pattern"]["last_low"])
        self.assertGreaterEqual(c["rr"], self.cfg["min_rr"])
        self.assertEqual(c["score"], sum(c["factors"].values()))

    def test_needs_volume_dry_up(self):
        p = vcp.detect(vcp_bars(base_vol=1000), self.cfg)
        self.assertFalse(p["found"])
        self.assertIn("dry-up", p["reason"])

    def test_needs_breakout_volume(self):
        p = vcp.detect(vcp_bars(bvol=600), self.cfg)
        self.assertFalse(p["found"])
        self.assertIn("breakout volume", p["reason"])

    def test_extended_breakout_rejected(self):
        p = vcp.detect(vcp_bars(breakout=156), self.cfg)
        self.assertFalse(p["found"])

    def test_contractions_must_shrink(self):
        bad = [(0, 100), (200, 100.0001), (330, 150), (340, 140), (350, 147), (357, 130), (363, 146), (367, 143),
               (370, 145.5)]
        self.assertFalse(vcp.detect(vcp_bars(bad), self.cfg)["found"])

    def test_trend_template_required(self):
        down = daily(path([(0, 180), (370, 100)]))
        p = vcp.detect(down, self.cfg)
        self.assertFalse(p["found"])
        self.assertIn("trend template", p["reason"])


# --- config + wiring -----------------------------------------------------------------------------

class WiringTests(unittest.TestCase):
    def test_shadow_and_wolfe_retired(self):
        g = get_config().global_
        self.assertFalse(get_config().instrument("wolfe").enabled)
        for seg in ("stock_futures", "index_futures", "mcx"):
            self.assertTrue(g.is_shadow(seg, "reversal"))
            self.assertIsNotNone(reversal.config_for(get_config().instrument(seg).model_extra))
        self.assertTrue(g.is_shadow("cash_reversal", "reversal"))
        self.assertTrue(g.is_shadow("vcp", "vcp"))
        self.assertFalse(g.is_shadow("mcx", "mcx_trend"))      # live MCX setups unchanged
        self.assertFalse(get_config().global_.stage_analysis.top_warnings.alert)

    def test_cash_books_follow_the_cash_path(self):
        from backend.journal import panel, postmortem, tracker
        for mod in (panel, postmortem, tracker):
            self.assertIn("cash_reversal", mod.CASH_SEGMENTS)
            self.assertIn("vcp", mod.CASH_SEGMENTS)
        self.assertEqual(exit_rule_for("cash_reversal")["mode"], "breakeven_trail")

    def test_older_rule_sets_fall_back_to_the_live_blocks(self):
        path = available_rule_sets()["v1_baseline"]
        with use_config(path):
            self.assertNotIn("vcp", get_config().instruments)
            self.assertTrue(instrument_or_live("vcp").enabled)
            self.assertEqual(exit_rule_for("vcp")["mode"], "breakeven_trail")
            self.assertTrue(reversal.cash_settings()["enabled"])

    def test_cash_context_uses_the_daily_50(self):
        # on daily bars a 200 EMA context would contradict the Stage 1/2 filter for longs
        self.assertEqual(reversal.cash_settings()["params"]["context_ema"], 50)

    def test_journal_row(self):
        row = reversal.build(reversal_bars(), "long", reversal.params({"max_stop_atr": 6}), STAGE2)
        row.update(symbol="ABC", token=1, shortlisted=False, daily_stage=2)
        rec = journal_row(row, "cash_reversal", "reversal", date(2026, 11, 25), date(2026, 11, 26),
                          date(2027, 5, 1), "15:15")
        self.assertEqual((rec["segment"], rec["setup"], rec["direction"]), ("cash_reversal", "reversal", "long"))
        self.assertEqual(rec["fill_until"], "2026-11-26T15:15")
        self.assertEqual(rec["extra"]["daily_stage"], 2)
        self.assertEqual(rec["extra"]["pattern"]["kind"], "reversal")


class HourlyScanReversalTests(unittest.TestCase):
    """The hourly futures scan journals a reversal as a shadow setup next to the live ones."""

    def test_reversal_rows_are_shadow_tracked(self):
        from unittest import mock
        from tests.test_fno_scan import HourlyScanTests

        captured = {}

        def fake(bars, daily_bars, p, *, directions, regime):
            captured["args"] = (len(bars), len(daily_bars), directions)
            row = reversal.build(reversal_bars(), "long", reversal.params({"max_stop_atr": 6}), STAGE2, regime)
            k = bars[-1]["close"] / row["entry"]          # scale onto the contract's price
            row.update({key: round(row[key] * k, 2) for key in ("entry", "stop", "target", "risk_per_unit")})
            return [row]

        case = HourlyScanTests("test_breakout_recorded_with_lots_and_expiry_safe_time_stop")
        case.setUp()
        try:
            with mock.patch("backend.signals.reversal.candidates", side_effect=fake):
                case.scan()
            from backend.journal.store import list_records
            recs = {r["setup"]: r for r in list_records("segment = 'stock_futures'")}
            self.assertIn("reversal", recs)
            self.assertEqual(recs["breakout"]["decision"], "shortlisted")     # the live slot is untouched
            self.assertTrue(get_config().global_.is_shadow("stock_futures", "reversal"))
            self.assertEqual(recs["reversal"]["extra"]["pattern"]["kind"], "reversal")
            self.assertEqual(captured["args"][2], ("long", "short"))
        finally:
            case.tearDown()


from tests._support import IsolatedStateTestCase  # noqa: E402


class CashScanTests(IsolatedStateTestCase, unittest.TestCase):
    """The daily VCP scan through the shared cash scan: journaled, shadow (never sent to the AI)."""

    def test_vcp_scan_journals_a_shadow_record(self):
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from backend.signals import events
        from tests.test_two_stage_and_cup import _days, flat_nifty
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (256265, "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, 0, 0, 0, "2026-11-20"),
                (501, "NSE", "NSE", "VCPCO", "VCPCO", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "VCPCO26DECFUT", "VCPCO", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-20")])
        raw = vcp_bars()
        days = _days(date(2026, 11, 20), len(raw))
        bars = [{**b, "ts": f"{d.isoformat()}T00:00:00", "volume": b["volume"] * 10_000} for b, d in zip(raw, days)]
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in bars])
        save_candles(256265, "day", [{**b, "date": b["ts"]} for b in flat_nifty(bars)])
        events.refresh_results(["VCPCO"], fetch=lambda s: date(2027, 3, 1), today=date(2026, 11, 20))
        out = vcp.run_scan(now=datetime(2026, 11, 20, 16, 0), results_fetch=lambda s: date(2027, 3, 1))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["patterns"], 1, out)
        self.assertTrue(out["shadow"])
        from backend.journal.store import list_records
        recs = list_records("segment = ?", ("vcp",))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["setup"], "vcp")
        self.assertNotIn(recs[0].get("panel_decision"), ("approve", "reject"))

    def test_vcp_backtest_runner(self):
        from backend.backtest import engine
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from tests.test_two_stage_and_cup import _days, flat_nifty
        ensure_marketdata_tables()
        raw = vcp_bars()
        for i in range(1, 15):        # the breakout follows through
            c = 147.5 + 0.8 * i
            raw.append({"ts": "", "open": c - 0.4, "high": c + 0.5, "low": c - 0.6, "close": c, "volume": 2000})
        days = _days(date(2026, 11, 20), len(raw))
        bars = [{**b, "ts": f"{d.isoformat()}T00:00:00", "volume": b["volume"] * 10_000} for b, d in zip(raw, days)]
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in bars])
        trades = engine.run_vcp(date(2026, 1, 1), date(2026, 11, 20), [{"symbol": "VCPCO", "spot_token": 501}],
                                flat_nifty(bars), daily_only=True)
        self.assertEqual(len(trades), 1, trades)
        self.assertEqual(trades[0]["data"], "daily_only")
        self.assertIn("pivot", trades[0]["pattern"])
        self.assertIn("vcp", engine.SEGMENTS)
        self.assertIsInstance(engine.run_cash_reversal(date(2026, 1, 1), date(2026, 11, 20),
                                                       [{"symbol": "VCPCO", "spot_token": 501}], flat_nifty(bars),
                                                       daily_only=True), list)
        self.assertIn("cash_reversal", engine.SEGMENTS)

    def test_exit_warning_stored_for_a_held_long_without_alert(self):
        from backend.journal.store import add_records
        from backend.marketdata import ensure_marketdata_tables
        from backend.marketdata.candles import save_candles
        from backend.signals import exit_warnings
        from tests.test_two_stage_and_cup import _days
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (501, "NSE", "NSE", "HELD", "HELD", "EQ", None, 0, 1, 0.05, "2026-11-20"),
                (601, "NFO", "NFO-FUT", "HELD26DECFUT", "HELD", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-20")])
        closes = path([(0, 100), (299, 180)]) + [168.0]
        vols = [1000] * 300 + [3000]
        days = _days(date(2026, 11, 20), len(closes))
        bars = [{**b, "ts": f"{d.isoformat()}T00:00:00"} for b, d in zip(daily(closes, vols), days)]
        save_candles(501, "day", [{**b, "date": b["ts"]} for b in bars])
        add_records([{"segment": "cash_swing", "symbol": "HELD", "instrument_token": 501, "exchange": "NSE",
                      "signal_date": "2026-11-10", "setup": "pullback", "direction": "long", "decision": "approved",
                      "entry": 170.0, "stop": 160.0, "target": 190.0, "entry_date": "2026-11-11",
                      "fill_until": "2026-11-11T15:15", "time_stop_date": "2027-02-01", "extra": {}}])
        sent = []
        out = exit_warnings.run(now=datetime(2026, 11, 20, 16, 0), notify=lambda new: sent.append(new) or "sent")
        self.assertEqual(out["status"], "ok", out)
        self.assertGreaterEqual(out["new"], 1)
        self.assertEqual(sent, [])                                    # alert off (shadow)
        kinds = {w["kind"] for w in exit_warnings.list_warnings()}
        self.assertIn("break_50dma", kinds)
        again = exit_warnings.run(now=datetime(2026, 11, 20, 16, 5))
        self.assertEqual(again["new"], 0)                             # once per symbol, kind and day

    def test_reversal_scan_runs(self):
        from backend.marketdata import ensure_marketdata_tables
        ensure_marketdata_tables()
        out = reversal.run_scan(now=datetime(2026, 11, 20, 16, 0), results_fetch=lambda s: None)
        self.assertEqual(out["status"], "blocked")          # no NIFTY data in an empty store
        self.assertIn("NIFTY", out["reason"])


if __name__ == "__main__":
    unittest.main()
