"""AI panel over shortlisted journal records and the Telegram alerts."""

import json
import unittest
from datetime import datetime
from unittest import mock

from tests._support import IsolatedStateTestCase

NOW = datetime(2026, 11, 23, 10, 20)


def approve(conf=80):
    return lambda prompt: (json.dumps({"decision": "approve", "confidence": conf,
                                       "reasons": ["trend intact", "clean pullback"], "risks": ["near resistance"]}),
                           "fake-model")


class PanelTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = mock.patch("backend.notifications.telegram.send_html_message",
                             side_effect=lambda text, reply_markup=None: self.sent.append(text) or {"ok": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, symbol, score=70, decision="shortlisted", fill_until="2026-11-23T12:15:00", **kw):
        from backend.journal.store import add_records, list_records
        add_records([{"segment": "stock_futures", "symbol": symbol, "instrument_token": 1, "exchange": "NFO",
                      "signal_date": "2026-11-23", "signal_ts": "2026-11-23T09:15:00", "setup": "pullback",
                      "direction": "long", "regime": "STRONG_UP", "score": score, "decision": decision,
                      "entry": 100.0, "stop": 96.0, "target": 108.0, "rr": 2.0, "entry_date": "2026-11-23",
                      "fill_until": fill_until, "time_stop_date": "2026-11-25",
                      "extra": {"tradingsymbol": f"{symbol}26DECFUT", "lots": 2},
                      "stages": [{"stage": "ai_panel", "decision": "pending"},
                                 {"stage": "final_approval", "decision": "pending"}], **kw}])
        return next(r for r in list_records() if r["symbol"] == symbol)

    def rec(self, symbol):
        from backend.journal.store import list_records
        return next(r for r in list_records() if r["symbol"] == symbol)

    def test_approve_marks_paper_and_alerts(self):
        from backend.journal.panel import review_pending
        self.add("AAA")
        out = review_pending(NOW, llm_call=approve())
        self.assertEqual(out["approved"], ["AAA"])
        r = self.rec("AAA")
        self.assertEqual((r["decision"], r["panel_decision"], r["is_paper"]), ("approved", "approve", 1))
        self.assertEqual(r["panel"]["confidence"], 80)
        self.assertEqual([s["decision"] for s in r["stages"]], ["pass", "pass"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("AI-approved trade", self.sent[0])
        self.assertIn("BUY AAA26DECFUT", self.sent[0])
        self.assertIn("2 lots", self.sent[0])
        self.assertIn("trend intact", self.sent[0])

    def test_reject_and_low_confidence(self):
        from backend.journal.panel import review_pending
        self.add("AAA", score=80)
        self.add("BBB", score=70)
        replies = iter([
            json.dumps({"decision": "reject", "confidence": 70, "reasons": ["extended"], "risks": []}),
            json.dumps({"decision": "approve", "confidence": 40, "reasons": ["ok"], "risks": []}),
        ])
        out = review_pending(NOW, llm_call=lambda p: (next(replies), "m"))
        self.assertEqual(sorted(out["rejected"]), ["AAA", "BBB"])
        self.assertEqual(self.rec("AAA")["decision"], "rejected_panel")
        self.assertIn("confidence 40 < 60", self.rec("BBB")["reject_reason"])
        self.assertEqual(self.sent, [])

    def test_daily_ai_limit_then_rules_only_alert(self):
        from backend.journal.panel import review_pending
        for i in range(6):
            self.add(f"S{i}", score=90 - i)
        with mock.patch("backend.journal.panel.limit_block", return_value=None):
            out = review_pending(NOW, llm_call=approve())
        self.assertEqual(len(out["approved"]), 5)
        self.assertEqual(out["not_sent"], [{"symbol": "S5", "reason": "daily AI limit reached (5)"}])
        self.assertEqual(self.rec("S5")["decision"], "shortlisted")
        self.assertIn("rules only, AI limit reached", self.sent[-1])

    def test_portfolio_limits_stop_new_approvals(self):
        from backend.journal.panel import review_pending
        for i in range(7):
            self.add(f"S{i}", score=90 - i)
        out = review_pending(NOW, llm_call=approve())
        # 5 new trades a day is the first limit hit (10 positions / 10% open risk allowed).
        self.assertEqual(len(out["approved"]), 5)
        self.assertIn("daily trade limit", out["not_sent"][0]["reason"])
        self.assertEqual(self.rec("S6")["panel_decision"], "not_sent")

    def test_open_position_limit_per_book(self):
        from backend.journal.panel import limit_block
        from backend.journal.store import update_record
        for i in range(10):
            update_record(self.add(f"F{i}", signal_date="2026-11-16")["id"], {"decision": "approved"})
        self.assertEqual(limit_block(NOW.date(), "stock_futures"), "open position limit reached (10/10)")
        self.assertEqual(limit_block(NOW.date(), "mcx"), "open position limit reached (10/10)")   # same F&O book
        self.assertIsNone(limit_block(NOW.date(), "cash_swing"))                                # own book (2)

    def test_live_risk_limits(self):
        from backend.decision.config import get_config
        g = get_config().global_
        self.assertEqual((g.risk.max_open_positions, g.risk.max_open_risk_pct), (10, 10.0))

    def test_ai_error_keeps_shortlisted_and_says_so(self):
        from backend.journal.panel import review_pending
        self.add("AAA")
        out = review_pending(NOW, llm_call=lambda p: ("I think it's fine", "m"))
        self.assertEqual(out["errors"][0]["symbol"], "AAA")
        r = self.rec("AAA")
        self.assertEqual((r["decision"], r["panel_decision"]), ("shortlisted", "error"))
        self.assertIn("AI review failed", self.sent[0])

    def test_expired_window_paused_and_disabled(self):
        from backend.journal.panel import review_pending
        from backend.system_state import set_paused
        self.add("OLD", fill_until="2026-11-23T10:00:00")
        self.assertEqual(review_pending(NOW, llm_call=lambda p: self.fail("expired window reviewed"))["approved"], [])
        self.add("AAA")
        set_paused(True, "test")
        self.assertEqual(review_pending(NOW, llm_call=lambda p: self.fail("AI while paused"))["reason"], "paused")
        set_paused(False)
        with mock.patch("backend.journal.panel.get_config") as cfg:
            cfg.return_value.global_.ai_panel.enabled = False
            self.assertEqual(review_pending(NOW)["status"], "skipped")

    def test_telegram_not_configured_does_not_break(self):
        from backend.journal.panel import review_pending
        from backend.notifications.telegram import TelegramConfigError
        self.add("AAA")
        with mock.patch("backend.notifications.telegram.send_html_message",
                        side_effect=TelegramConfigError("Telegram bot token or chat ID is not configured")):
            out = review_pending(NOW, llm_call=approve())
        self.assertEqual(out["approved"], ["AAA"])
        self.assertTrue(out["alerts"][0]["status"].startswith("not sent"))

    def test_approved_symbol_is_busy_for_the_next_scan(self):
        from backend.journal.panel import review_pending
        from backend.signals.selection import open_positions
        self.add("AAA")
        review_pending(NOW, llm_call=approve())
        self.assertEqual(open_positions(["stock_futures"]), {("stock_futures", "AAA")})

    def test_parse_reply(self):
        from backend.journal.panel import parse_reply
        self.assertEqual(parse_reply('Sure:\n{"decision":"APPROVE","confidence":72.4,"reasons":["a"]}')["confidence"], 72)
        for bad in ("no json", '{"decision":"maybe","confidence":50}', '{"decision":"approve","confidence":150}'):
            with self.assertRaises(ValueError):
                parse_reply(bad)

    def test_after_scan_hook_respects_switch(self):
        from backend.journal.panel import review_after_scan
        self.assertEqual(review_after_scan(NOW)["reason"], "TRADINGAGENTS_AI_PANEL=off")


if __name__ == "__main__":
    unittest.main()
