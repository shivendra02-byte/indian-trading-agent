"""Per-book AI review shares, top-of-scan selection and shadow strategies (agreed 23 Nov 2026)."""

import json
import unittest
from datetime import datetime
from unittest import mock

from tests._support import IsolatedStateTestCase


def approve(prompt):
    return json.dumps({"decision": "approve", "confidence": 80, "reasons": ["ok"], "risks": []}), "m"


class QuotaTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        p = mock.patch("backend.notifications.telegram.send_html_message",
                       side_effect=lambda text, reply_markup=None: self.sent.append(text) or {"ok": True})
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch("backend.journal.panel.limit_block", return_value=None)
        p2.start()
        self.addCleanup(p2.stop)

    def add(self, symbol, ts, score=70, segment="stock_futures", setup="pullback"):
        from backend.journal.store import add_records
        add_records([{"segment": segment, "symbol": symbol, "instrument_token": 1, "exchange": "NFO",
                      "signal_date": ts[:10], "signal_ts": ts, "setup": setup, "direction": "long",
                      "regime": "STRONG_UP", "score": score, "decision": "shortlisted", "entry": 100.0,
                      "stop": 96.0, "target": 108.0, "rr": 2.0, "entry_date": ts[:10],
                      "fill_until": "2026-11-23T23:00:00", "time_stop_date": "2026-11-25",
                      "extra": {"tradingsymbol": symbol}, "stages": []}])

    def rec(self, symbol):
        from backend.journal.store import list_records
        return next(r for r in list_records() if r["symbol"] == symbol)

    def test_config_values(self):
        from backend.decision.config import get_config
        g = get_config().global_
        self.assertEqual(g.ai_panel.reviews_per_day, {"futures": 3, "mcx": 1, "cash": 2, "option_spreads": 1})
        self.assertEqual((g.ai_panel.reserved_after["futures"].after, g.ai_panel.reserved_after["futures"].reviews),
                         ("13:15", 1))
        self.assertEqual(g.limits.max_ai_evaluations_day, 7)
        self.assertEqual(sorted(g.shadow_strategies), ["cup_handle", "momentum", "nifty_options", "orb", "wolfe"])

    def test_only_top_score_of_a_scan_is_reviewed(self):
        from backend.journal.panel import review_pending
        self.add("LOW", "2026-11-23T09:15:00", score=65)
        self.add("TOP", "2026-11-23T09:15:00", score=82)
        self.add("IDX", "2026-11-23T09:15:00", score=70, segment="index_futures")   # same futures book
        self.add("GOLDM", "2026-11-23T09:15:00", score=60, segment="mcx")            # other book
        out = review_pending(datetime(2026, 11, 23, 10, 20), llm_call=approve)
        self.assertEqual(sorted(out["approved"]), ["GOLDM", "TOP"])
        self.assertIn("lower score than TOP", self.rec("LOW")["panel"]["skipped"])
        self.assertEqual(self.rec("IDX")["panel_decision"], "not_sent")
        self.assertEqual(self.rec("LOW")["decision"], "shortlisted")      # still tracked
        self.assertEqual(len(self.sent), 2)                                # no alert for the skipped ones

    def test_third_futures_review_kept_for_afternoon(self):
        from backend.journal.panel import review_pending
        for i, hh in enumerate(("09", "10", "11")):
            self.add(f"M{i}", f"2026-11-23T{hh}:15:00")
        out = review_pending(datetime(2026, 11, 23, 12, 20), llm_call=approve)
        self.assertEqual(out["approved"], ["M0", "M1"])
        self.assertIn("kept for later", self.rec("M2")["panel"]["skipped"])
        self.add("PM", "2026-11-23T13:15:00")
        out = review_pending(datetime(2026, 11, 23, 14, 20), llm_call=approve)
        self.assertEqual(out["approved"], ["PM"])
        self.add("PM2", "2026-11-23T14:15:00")
        out = review_pending(datetime(2026, 11, 23, 15, 20), llm_call=approve)
        self.assertEqual(out["approved"], [])
        self.assertIn("futures AI reviews used (3/day)", self.rec("PM2")["panel"]["skipped"])

    def test_books_have_own_shares(self):
        from backend.journal.panel import review_pending, usage_today
        self.add("G1", "2026-11-23T17:00:00", segment="mcx")
        self.add("G2", "2026-11-23T18:00:00", segment="mcx")
        self.add("C1", "2026-11-23", segment="cash_swing")
        out = review_pending(datetime(2026, 11, 23, 18, 5), llm_call=approve)
        self.assertEqual(sorted(out["approved"]), ["C1", "G1"])
        self.assertIn("mcx AI reviews used (1/day)", self.rec("G2")["panel"]["skipped"])
        u = usage_today(datetime(2026, 11, 23).date())
        self.assertEqual((u["ai_books"]["mcx"]["used"], u["ai_books"]["cash"]["used"]), (1, 1))

    def test_shadow_strategies_not_reviewed_or_alerted(self):
        from backend.journal.panel import review_pending
        self.add("CUP", "2026-11-23", segment="cup_handle")
        self.add("WOLF", "2026-11-23", segment="wolfe", score=90)
        self.add("ORB1", "2026-11-23T09:45:00", setup="orb", score=95)
        out = review_pending(datetime(2026, 11, 23, 16, 0), llm_call=approve)
        self.assertEqual(out["approved"], [])
        for s in ("CUP", "WOLF", "ORB1"):
            self.assertEqual(self.rec(s)["panel"]["skipped"], "shadow strategy: tracked, not reviewed")
        self.assertEqual(self.sent, [])

    def test_momentum_shadow_sends_nothing(self):
        from backend.momentum.book import _alert
        self.assertEqual(_alert("hello"), "off: shadow strategy")

    def test_reserve_cannot_exceed_share(self):
        from pydantic import ValidationError
        from backend.decision.config import AiPanelCfg
        with self.assertRaises(ValidationError):
            AiPanelCfg(reviews_per_day={"futures": 1}, reserved_after={"futures": {"after": "13:15", "reviews": 2}})


if __name__ == "__main__":
    unittest.main()
