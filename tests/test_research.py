"""Research section (27 Nov 2026): trend books, pairs, cash momentum, risk switch, journal,
stats, post-mortem, win model, regime hysteresis, API. Synthetic bars, no network."""

from __future__ import annotations

import json
import math
import random
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from backend.research import config as rcfg
from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login


def weekdays(n: int, start: date = date(2019, 1, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_from(closes: list[float], start: date = date(2019, 1, 1), spread: float = 0.004, volume: int = 2_000_000):
    out = []
    prev = closes[0]
    for d, c in zip(weekdays(len(closes), start), closes):
        out.append({"ts": d.isoformat() + "T00:00:00", "open": prev, "high": max(prev, c) * (1 + spread),
                    "low": min(prev, c) * (1 - spread), "close": c, "volume": volume})
        prev = c
    return out


CTX = {"kind": "mcx", "pv": 10.0, "contract": "TEST", "cost_bps": 3.0, "roll_bps": 4.0, "one_r": 10_000.0}


class TrendTests(unittest.TestCase):
    def setUp(self):
        rcfg.set_override(None)

    def test_ewma_long_trend_is_traded_long_and_fills_next_open(self):
        from backend.research.trend import simulate_ewma
        closes = [100 * math.exp(0.002 * i) for i in range(700)]
        bars = bars_from(closes)
        p = {"fast": 20, "slow": 250, "atr_period": 33, "entry_buffer_atr": 1.0, "stop_atr": 5.0,
             "exit_rule": "stop_and_signal_off", "warmup_bars": 300}
        trades = simulate_ewma(bars, p, "GOLDM", CTX)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "long")
        self.assertEqual(t["status"], "open")
        i = next(k for k, b in enumerate(bars) if b["ts"][:10] == t["entry_date"])
        self.assertEqual(t["entry_price"], round(bars[i]["open"], 2))       # next day's open, not the signal close
        self.assertGreater(t["pnl_r"], 0)

    def test_ewma_exit_needs_stop_and_signal_off(self):
        from backend.research.trend import simulate_ewma
        up = [100 * math.exp(0.002 * i) for i in range(500)]
        down = [up[-1] * math.exp(-0.004 * i) for i in range(1, 300)]
        bars = bars_from(up + down)
        p = {"fast": 20, "slow": 250, "atr_period": 33, "entry_buffer_atr": 0.5, "stop_atr": 3.0,
             "exit_rule": "stop_and_signal_off", "warmup_bars": 300, "short_enabled": False}
        trades = simulate_ewma(bars, p, "GOLDM", CTX)
        closed = [t for t in trades if t["status"] == "closed"]
        self.assertTrue(closed)
        self.assertEqual(closed[0]["exit_reason"], "trailing_stop")
        self.assertIn("cost_r", closed[0]["extra"])

    def test_is_rebalance_first_session_on_or_after_weekday(self):
        from backend.research.trend import is_rebalance
        days = [d.isoformat() for d in weekdays(10, date(2026, 11, 23))]     # Mon 23 Nov ...
        flags = [is_rebalance(days, i, 2) for i in range(len(days))]
        self.assertEqual([days[i] for i, f in enumerate(flags) if f], ["2026-11-25", "2026-12-02"])
        # Wednesday missing (holiday): Thursday takes it
        gap = [d for d in days if d != "2026-11-25"]
        self.assertTrue(is_rebalance(gap, gap.index("2026-11-26"), 2))

    def test_tsmom_signal_sign(self):
        from backend.research.trend import tsmom_signals
        random.seed(1)
        up = [100.0]
        for _ in range(200):
            up.append(up[-1] * (1 + 0.004 + random.gauss(0, 0.01)))
        sig = tsmom_signals(up, 10, 10, 33)
        self.assertGreater(sig[-1], 0.5)
        sig_down = tsmom_signals([1 / c for c in up], 10, 10, 33)
        self.assertLess(sig_down[-1], -0.5)


class StatsTests(unittest.TestCase):
    def test_worst_quarter_and_bear(self):
        from backend.research.stats import bear_quarters, bear_result, stats
        trades = [{"entry_date": "2025-01-05", "exit_date": "2025-02-01", "pnl_r": 2.0, "status": "closed"},
                  {"entry_date": "2025-04-05", "exit_date": "2025-05-01", "pnl_r": -1.0, "status": "closed"},
                  {"entry_date": "2025-04-06", "exit_date": "2025-06-01", "pnl_r": -0.5, "status": "closed"},
                  {"entry_date": "2025-07-01", "exit_date": "2025-08-01", "pnl_r": 1.0, "status": "closed"}]
        s = stats(trades)
        self.assertEqual(s["worst_quarter"], "2025-Q2")
        self.assertEqual(s["worst_quarter_r"], -1.5)
        self.assertEqual(s["quarters_positive_pct"], 67.0)
        nifty = []
        for d, c in (("2024-12-31", 100), ("2025-03-31", 110), ("2025-06-30", 90), ("2025-09-30", 95)):
            nifty.append({"ts": d + "T00:00:00", "close": c})
        bq = bear_quarters(nifty, share=0.34)
        self.assertEqual(bq["bear"], ["2025-Q2"])
        br = bear_result(trades, bq["bear"])
        self.assertEqual(br["avg_r_per_bear_quarter"], -1.5)


class PairsTests(unittest.TestCase):
    def setUp(self):
        rcfg.set_override(None)

    def _series(self, n=900):
        random.seed(7)
        days = [d.isoformat() for d in weekdays(n, date(2021, 1, 1))]
        f = sp = 0.0
        a, b, c = {}, {}, {}
        for d in days:
            f += random.gauss(0, 0.01)
            sp = 0.88 * sp + random.gauss(0, 0.012)
            a[d] = 100 * math.exp(f)
            b[d] = 50 * math.exp(f + sp)
            c[d] = 70 * math.exp(random.gauss(0, 0.01) + 0.5 * f + 0.001 * len(c))
        return {"AAA": a, "BBB": b, "CCC": c}

    def test_df_test_tells_mean_reversion_from_random_walk(self):
        import numpy as np
        from backend.research.pairs import df_test
        random.seed(3)
        ar, rw = [0.0], [0.0]
        for _ in range(500):
            ar.append(0.8 * ar[-1] + random.gauss(0, 1))
            rw.append(rw[-1] + random.gauss(0, 1))
        t_ar, hl = df_test(np.array(ar))
        t_rw, _ = df_test(np.array(rw))
        self.assertLess(t_ar, -3.34)
        self.assertGreater(t_rw, -3.34)
        self.assertAlmostEqual(hl, -math.log(2) / math.log(0.8), delta=1.0)

    def test_matching_uses_each_stock_once(self):
        from backend.research.pairs import match_pairs
        cands = [{"x": "A", "y": "B", "t": -6.0}, {"x": "A", "y": "C", "t": -5.0}, {"x": "C", "y": "D", "t": -4.0},
                 {"x": "B", "y": "D", "t": -3.5}]
        chosen = match_pairs(cands, 10)
        names = [s for c in chosen for s in (c["x"], c["y"])]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({(c["x"], c["y"]) for c in chosen}, {("A", "B"), ("C", "D")})

    def test_simulate_trades_the_cointegrated_pair(self):
        from backend.research.pairs import simulate
        p = {**rcfg.strategy("pairs_stocks"), "formation_days": 400}
        trades, extra = simulate(self._series(), p, "2023-01-01", None, unit=lambda s: 100.0)
        self.assertTrue(trades)
        self.assertTrue(all(t["symbol"] == "BBB/AAA" for t in trades))
        closed = [t for t in trades if t["status"] == "closed"]
        self.assertGreater(sum(t["pnl_r"] for t in closed), 0)
        self.assertEqual(len(closed[0]["legs"]), 2)
        self.assertGreater(extra["funnel"]["formations"], 10)

    def test_metals_pair_records_cointegration_flag(self):
        from backend.research.pairs import simulate
        p = {**rcfg.strategy("pairs_metals"), "formation_days": 400}
        s = self._series()
        trades, _ = simulate({"GOLDM": s["AAA"], "SILVERM": s["BBB"]}, p, "2023-01-01", None,
                             force_pairs=[("GOLDM", "SILVERM")], unit=lambda s: 10.0, cost_kind="mcx")
        self.assertTrue(trades)
        self.assertIn("cointegrated", trades[0]["extra"])


class CashMomentumTests(unittest.TestCase):
    def setUp(self):
        rcfg.set_override(None)

    def test_regression_score_prefers_smooth_uptrends(self):
        from backend.research.cash_momentum import regression_score
        smooth = [100 * math.exp(0.002 * i) for i in range(90)]
        noisy = [c * (1 + 0.05 * math.sin(i)) for i, c in enumerate(smooth)]
        self.assertGreater(regression_score(smooth), regression_score(noisy))
        self.assertLess(regression_score([1 / c for c in smooth]), 0)

    def test_no_buys_while_nifty_below_its_200_day_sma(self):
        from backend.research.cash_momentum import simulate
        down = bars_from([20000 * math.exp(-0.001 * i) for i in range(500)])
        stocks = {f"S{i}": bars_from([100 * math.exp((0.001 + i * 0.0002) * k) for k in range(500)])
                  for i in range(10)}
        trades, extra = simulate(stocks, down, rcfg.strategy("cash_momentum"), start=down[300]["ts"][:10])
        self.assertEqual(trades, [])
        self.assertGreater(extra["funnel"]["index_filter_off"], 0)
        up = bars_from([20000 * math.exp(0.001 * i) for i in range(500)])
        trades, extra = simulate(stocks, up, rcfg.strategy("cash_momentum"), start=up[300]["ts"][:10])
        self.assertTrue(trades)
        self.assertTrue(all(t["direction"] == "long" for t in trades))


class RiskSwitchTests(unittest.TestCase):
    def test_hysteresis_keeps_defensive_until_on_above(self):
        from backend.research.risk_switch import compute
        closes = [100 * math.exp(0.001 * i) for i in range(260)] + \
                 [100 * math.exp(0.26) * math.exp(-0.004 * i) for i in range(1, 60)] + \
                 [100 * math.exp(0.26 - 0.236) * math.exp(0.0005 * i) for i in range(1, 40)]
        nifty = bars_from(closes)
        p = {"off_below": 0.0, "on_above": 0.2, "drawdown_pct": 5, "horizon_days": 20,
             "weights": {"trend": 0.5, "momentum": 0.5}}
        rows = compute(nifty, {}, {}, [], p)
        states = [r["state"] for r in rows]
        self.assertIn("DEFENSIVE", states)
        for prev, cur in zip(rows, rows[1:]):
            if prev["state"] == "DEFENSIVE" and cur["state"] == "NORMAL":
                self.assertGreater(cur["score"], 0.2)
            if prev["state"] == "NORMAL" and cur["state"] == "DEFENSIVE":
                self.assertLess(cur["score"], 0.0)

    def test_study_counts_falls(self):
        from backend.research.risk_switch import study
        rows = [{"date": f"2025-01-{i + 1:02d}", "score": -0.5 if i < 5 else 0.5,
                 "state": "DEFENSIVE" if i < 5 else "NORMAL", "close": 100 - (10 if 2 <= i < 6 else 0)}
                for i in range(25)]
        out = study(rows, {"horizon_days": 3, "drawdown_pct": 5})
        self.assertGreater(out["by_state"]["DEFENSIVE"]["fall_rate_pct"], out["by_state"]["NORMAL"]["fall_rate_pct"])


class RegimeHysteresisTests(unittest.TestCase):
    def test_confirm_needs_consecutive_days(self):
        from backend.signals.regime import _confirm
        raw = ["A", "A", "B", "A", "B", "B", "B", None, "C"]
        self.assertEqual(_confirm(raw, 2), ["A", "A", "A", "A", "A", "B", "B", None, "B"])
        self.assertEqual(_confirm(raw, 1), raw)

    def test_experiment_rule_set_loads_with_hysteresis(self):
        from backend.decision.config import get_config, use_config
        self.assertEqual(get_config().global_.regime.confirm_days, 1)       # live: off
        self.assertIsNone(get_config().global_.regime.adx_trend_exit)
        with use_config("config/experiments/regime_hysteresis.yaml"):
            self.assertEqual(get_config().global_.regime.confirm_days, 2)
            self.assertEqual(get_config().global_.regime.adx_trend_exit, 22)

    def test_spread_research_preset(self):
        from backend.options.mtf_backtest import PRESETS
        names = [v["name"] for v in PRESETS["research"]]
        self.assertEqual(names[0], "live")
        self.assertTrue(any(v.get("use_invalidation") is False for v in PRESETS["research"]))
        self.assertTrue(any(v.get("slow_trend_filter") for v in PRESETS["research"]))


def hourly_nse(n_days: int, drift: float = 0.0004, seed: int = 3, start: date = date(2025, 1, 1)):
    random.seed(seed)
    out, p = [], 100.0
    for d in weekdays(n_days, start):
        for h, m in ((9, 15), (10, 15), (11, 15), (12, 15), (13, 15), (14, 15), (15, 15)):
            o = p
            p = p * (1 + drift + random.gauss(0, 0.004))
            out.append({"ts": datetime(d.year, d.month, d.day, h, m).isoformat(), "open": o,
                        "high": max(o, p) * 1.002, "low": min(o, p) * 0.998, "close": p, "volume": 1})
    return out


def daily_from(hourly):
    days = {}
    for b in hourly:
        days.setdefault(b["ts"][:10], []).append(b)
    return [{"ts": k + "T00:00:00", "open": v[0]["open"], "high": max(x["high"] for x in v),
             "low": min(x["low"] for x in v), "close": v[-1]["close"], "volume": 1} for k, v in sorted(days.items())]


class LiveExitTests(unittest.TestCase):
    """Asked 27 Nov 2026: research trades carry a stop and a target, hit during the session."""

    def setUp(self):
        rcfg.set_override(None)

    def test_intrabar_exit_gap_stop_first_and_target(self):
        from backend.research.trend import intrabar_exit
        t = {"direction": "long", "stop": 95.0, "target": 110.0}
        self.assertEqual(intrabar_exit(t, {"open": 94, "high": 96, "low": 93, "close": 95}, 95.0), (94, "stop"))
        self.assertEqual(intrabar_exit(t, {"open": 100, "high": 111, "low": 94, "close": 100}, 95.0), (95.0, "stop"))
        self.assertEqual(intrabar_exit(t, {"open": 100, "high": 111, "low": 99, "close": 110}, 95.0), (110.0, "target"))
        self.assertEqual(intrabar_exit(t, {"open": 100, "high": 101, "low": 97, "close": 100}, 98.0), (98.0, "trailing_stop"))
        self.assertIsNone(intrabar_exit(t, {"open": 100, "high": 101, "low": 99, "close": 100}, 95.0))

    def test_daily_trend_target_and_partial_bar(self):
        from backend.research.trend import simulate_ewma
        closes = [100 * math.exp(0.002 * i) for i in range(560)]
        bars = bars_from(closes)
        p = {"fast": 20, "slow": 250, "atr_period": 33, "entry_buffer_atr": 1.0, "stop_atr": 5.0, "target_r": 0.5,
             "exit_check": "intrabar", "warmup_bars": 300}
        trades = simulate_ewma(bars, p, "GOLDM", CTX)
        self.assertTrue(any(t["exit_reason"] == "target" for t in trades))
        first = trades[0]
        self.assertAlmostEqual(first["target"], first["entry_price"] + 0.5 * (first["entry_price"] - first["stop"]), delta=0.02)
        # a partial bar never starts a trade but can hit the stop
        flat = bars_from([100.0] * 400)
        sig_bars = flat[:-1] + [{**flat[-1], "partial": True, "close": 400.0, "high": 400.0}]
        self.assertEqual(simulate_ewma(sig_bars, {**p, "warmup_bars": 300}, "GOLDM", CTX), [])
        open_t = next(t for t in simulate_ewma(bars[:400], {**p, "target_r": 50}, "GOLDM", CTX) if t["status"] == "open")
        crash = bars[:400] + [{"ts": "2030-01-01T00:00:00", "open": open_t["entry_price"], "high": open_t["entry_price"],
                                 "low": 1.0, "close": 1.0, "volume": 0, "partial": True}]
        last = simulate_ewma(crash, {**p, "target_r": 50}, "GOLDM", CTX)[-1]
        self.assertEqual(last["status"], "closed")
        self.assertIn(last["exit_reason"], ("stop", "trailing_stop"))

    def test_hourly_pullback_and_4h_trades_have_stop_and_target(self):
        from backend.research import intraday
        from tradingagents.utils.market_calendar import NSE
        bars = hourly_nse(300)
        prior = bars_from([50 * math.exp(0.002 * i) for i in range(300)], date(2023, 1, 2))
        daily = prior + daily_from(bars)
        ctx = {"kind": "stock_futures", "pv": 500.0, "contract": "X", "cost_bps": 4.0, "roll_bps": 4.0, "one_r": 10000.0}
        p1 = rcfg.strategy("htf_pullback")
        t1 = intraday.run_1h(bars, intraday.pullback_signals(bars, daily, p1), p1, "TEST", ctx, "1h")
        p2 = rcfg.strategy("trend_4h")
        t2 = intraday.run_1h(bars, intraday.trend4h_signals(bars, NSE, p2), p2, "TEST", ctx, "4h")
        self.assertTrue(t1)
        self.assertTrue(t2)
        for t in t1 + t2:
            self.assertIsNotNone(t["stop"])
            self.assertIsNotNone(t["target"])
            sign = 1 if t["direction"] == "long" else -1
            self.assertGreater((t["target"] - t["entry_price"]) * sign, 0)
            self.assertGreater((t["entry_price"] - t["stop"]) * sign, 0)
        keys = [t["key"] for t in t1]
        self.assertEqual(len(keys), len(set(keys)))
        reasons = {t["exit_reason"] for t in t1 + t2}
        self.assertTrue(reasons & {"target", "stop", "breakeven_stop", "trailing_stop"})
        # entries fill at the bar after the signal
        sig_ts = t1[0]["extra"]["signal_ts"]
        k = next(i for i, b in enumerate(bars) if b["ts"] == sig_ts)
        self.assertEqual(t1[0]["entry_ts"], bars[k + 1]["ts"])

    def test_4h_bucket_completion(self):
        from backend.research.intraday import _bucket_index
        from tradingagents.utils.market_calendar import NSE
        bars = hourly_nse(2)
        four, idx, done = _bucket_index(bars, NSE)
        self.assertEqual(len(four), 4)                   # 09:15 and 13:15 buckets, two days
        self.assertEqual(done[:7], [False, False, False, True, False, False, True])


class StoredResearchTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        rcfg.set_override(None)

    def tearDown(self):
        rcfg.set_override(None)
        super().tearDown()

    def test_journal_sync_replaces_and_keeps_first_seen(self):
        from backend.research import journal
        t1 = {"symbol": "GOLDM", "direction": "long", "entry_date": "2026-12-01", "entry_price": 1.0,
              "status": "open", "pnl_r": 0.5, "extra": {}}
        out = journal.sync("trend_ewma", [t1, {**t1, "entry_date": "2026-11-01"}], "2026-11-27")
        self.assertEqual((out["trades"], out["open"], out["new"]), (1, 1, 1))
        first = journal.list_trades("trend_ewma")[0]["first_seen"]
        closed = {**t1, "status": "closed", "exit_date": "2026-12-20", "pnl_r": 2.0}
        out = journal.sync("trend_ewma", [closed], "2026-11-27")
        self.assertEqual(out["new"], 0)
        rows = journal.list_trades("trend_ewma")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[0]["first_seen"], first)
        self.assertEqual(len(journal.closed_between("2026-12-14", "2026-12-20")), 1)

    def test_run_live_skips_when_paused_and_isolates_errors(self):
        from backend.research import registry
        with mock.patch("backend.system_state.is_paused", return_value=True):
            self.assertEqual(registry.run_live("nse")["status"], "skipped")

        def runner(entry):
            if entry["key"] == "tsmom":
                raise RuntimeError("boom")
            return {"status": "ok"}
        out = registry.run_live("mcx", runner=runner)
        self.assertEqual(out["strategies"]["tsmom"]["status"], "error")
        self.assertEqual(out["strategies"]["trend_ewma"]["status"], "ok")
        self.assertIn("pairs_metals", out["strategies"])          # MCX group
        self.assertNotIn("cash_momentum", out["strategies"])       # NSE only

    def test_postmortem_due_and_run_with_fake_ai(self):
        from backend.research import journal, postmortem
        self.assertIsNone(postmortem.due(datetime(2026, 11, 27, 10, 0), None))        # Friday
        self.assertIsNone(postmortem.due(datetime(2026, 11, 28, 8, 0), None))         # Saturday before 08:30
        self.assertEqual(postmortem.due(datetime(2026, 11, 28, 9, 0), None), date(2026, 11, 23))
        self.assertIsNone(postmortem.due(datetime(2026, 11, 29, 9, 0), date(2026, 11, 23)))
        journal.sync("tsmom", [{"symbol": "NIFTY", "direction": "long", "entry_date": "2026-11-27",
                                "exit_date": "2026-11-27", "status": "closed", "pnl_r": 1.2, "extra": {}}], "2026-11-27")
        prompts = []

        def fake(prompt):
            prompts.append(prompt)
            return "## This week\nok", "fake-model"
        with mock.patch("backend.research.risk_switch.history", return_value=[]):
            out = postmortem.run(date(2026, 11, 23), llm_call=fake)
        self.assertEqual(out["ai_status"], "ok")
        self.assertEqual(out["closed"], 1)
        self.assertIn("too early to judge", prompts[0])
        report = postmortem.get_report("2026-11-23")
        self.assertEqual(report["ai_model"], "fake-model")
        self.assertEqual(report["summary"]["strategies"]["tsmom"]["this_week"]["n"], 1)

    def test_backtest_job_stores_summary(self):
        from backend.research import backtests
        fake_trades = [{"symbol": "X", "direction": "long", "entry_date": f"2024-0{m}-01", "exit_date": f"2024-0{m}-20",
                        "status": "closed", "pnl_r": (1.0 if m % 2 else -0.5)} for m in range(1, 9)]
        entry = {"backtest": lambda params, progress=None: {"trades": fake_trades, "grid": []}, "limitations": ["x"]}
        with mock.patch("backend.research.registry.get", return_value=entry), \
                mock.patch("backend.research.data.daily_bars", return_value=[]):
            out = backtests.run_sync("tsmom", {"split_date": "2024-05-01"})
        self.assertEqual(out["summary"]["all"]["n"], 8)
        self.assertEqual(out["summary"]["by_period"]["train"]["n"], 4)
        self.assertEqual(out["summary"]["limitations"], ["x"])

    def test_win_model_on_a_stored_backtest(self):
        from backend.backtest.engine import ensure_tables
        from backend.db import get_db
        from backend.research.win_model import run, sources
        ensure_tables()
        random.seed(4)
        trades = []
        d = datetime(2024, 1, 1, 10, 15)
        for i in range(900):
            score = random.uniform(30, 90)
            win = random.random() < (0.25 + 0.5 * (score - 30) / 60)     # higher score → more wins
            trades.append({"symbol": f"S{i % 20}", "signal_ts": (d + timedelta(hours=7 * i)).isoformat(),
                           "setup": random.choice(["pullback", "breakout"]), "direction": "long", "regime": "STRONG_UP",
                           "score": score, "rr": 2.0, "vol_ratio": 1.2, "entry": 100.0, "stop": 97.0,
                           "soft_checks": {}, "pnl_r": 1.5 if win else -1.0})
        with get_db() as conn:
            cur = conn.execute("INSERT INTO rule_backtests (segment, rule_set, start_date, end_date, status, trades) "
                               "VALUES ('cash_swing', 'current', '2024-01-01', '2024-12-31', 'done', ?)",
                               (json.dumps(trades),))
            run_id = cur.lastrowid
        self.assertEqual(sources()["backtests"][0]["id"], run_id)
        out = run("backtest", run_id, min_train=300)
        self.assertEqual(out["status"], "ok")
        self.assertGreater(out["auc"], 0.6)
        self.assertGreater(out["by_model_third"]["high"]["avg_r"], out["by_model_third"]["low"]["avg_r"])
        self.assertEqual(run("journal")["status"], "too few")

    def test_sync_by_symbol_and_hourly_run(self):
        from backend.research import journal, registry
        a = {"symbol": "GOLDM", "direction": "long", "entry_date": "2026-12-01", "entry_price": 1.0, "stop": 0.9,
             "target": 1.3, "status": "open", "pnl_r": 0.1, "extra": {}, "entry_ts": "2026-12-01T10:00:00",
             "timeframe": "1h"}
        b = {**a, "symbol": "NIFTY"}
        journal.sync("htf_pullback", [a, b], "2026-11-27")
        journal.sync("htf_pullback", [], "2026-11-27", symbols=["GOLDM"])        # MCX run with no GOLDM trade
        rows = journal.list_trades("htf_pullback")
        self.assertEqual([r["symbol"] for r in rows], ["NIFTY"])
        self.assertEqual(rows[0]["target"], 1.3)
        self.assertEqual(rows[0]["timeframe"], "1h")
        with mock.patch("backend.system_state.is_paused", return_value=True):
            self.assertEqual(registry.run_hourly("mcx")["status"], "skipped")
        seen = []
        out = registry.run_hourly("mcx", runner=lambda e: seen.append(e["key"]) or {"status": "ok"})
        self.assertEqual(set(seen), {"trend_ewma", "tsmom", "htf_pullback", "trend_4h"})
        self.assertEqual(out["strategies"]["trend_4h"]["status"], "ok")

    def test_partial_today_from_stitched_mcx_hours(self):
        from backend.db import get_db
        from backend.research import intraday
        intraday.ensure_table()
        with get_db() as conn:
            for h, (o, hi, lo, c) in enumerate(((10, 12, 9, 11), (11, 15, 10, 14), (14, 14, 8, 9))):
                conn.execute("INSERT INTO research_hourly_bars VALUES ('GOLDM', ?, ?, ?, ?, ?, 0, 'X')",
                             (f"2026-12-02T{9 + h:02d}:00:00", o, hi, lo, c))
        part = intraday.partial_today("GOLDM", "2026-12-01")
        self.assertEqual((part["open"], part["high"], part["low"], part["close"]), (10, 15, 8, 9))
        self.assertTrue(part["partial"])
        self.assertIsNone(intraday.partial_today("GOLDM", "2026-12-02"))

    def test_api_overview_and_journal(self):
        from backend.research import journal
        journal.sync("trend_ewma", [{"symbol": "GOLDM", "direction": "long", "entry_date": "2026-12-01",
                                     "status": "open", "pnl_r": 0.4, "extra": {}}], "2026-11-27")
        with fresh_test_client() as client:
            login(client)
            res = client.get("/api/research/")
            self.assertEqual(res.status_code, 200)
            keys = {s["key"] for s in res.json()["strategies"]}
            self.assertTrue({"trend_ewma", "tsmom", "pairs_stocks", "pairs_metals", "cash_momentum",
                             "risk_switch"} <= keys)
            res = client.get("/api/research/journal?strategy=trend_ewma")
            self.assertEqual(res.json()["summary"]["open"], 1)
            res = client.post("/api/research/backtests", json={"strategy": "trend_ewma", "start_date": "2024-01-01",
                                                               "end_date": "2023-01-01"}, headers=csrf_headers(client))
            self.assertEqual(res.status_code, 400)
            self.assertEqual(client.get("/api/research/strategy/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
