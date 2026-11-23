"""Deep Analysis survives a lost browser connection: the server keeps a snapshot of
everything streamed, reports failures and lists recent runs, so the page can catch up
after the iPad screen was off or the tab was reloaded."""

import unittest
from unittest import mock

from tests._support import IsolatedStateTestCase


class _FakeGraph:
    """Stands in for TradingAgentsGraph: streams two report chunks, optionally fails."""

    fail_after_first = False

    def __init__(self, *args, **kwargs):
        self.graph = self

    def stream(self, init_state, **kwargs):
        yield {"market_report": "Market looks firm."}
        if _FakeGraph.fail_after_first:
            raise RuntimeError("LLM timed out")
        yield {"market_report": "Market looks firm.", "final_trade_decision": "FINAL: BUY",
               "investment_debate_state": {"bull_history": "Bull case", "bear_history": "Bear case"}}

    def process_signal(self, text):
        return "BUY"


class AnalysisResumeTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.routers import analysis
        self.analysis = analysis
        analysis._tasks.clear()
        patcher = mock.patch("tradingagents.graph.trading_graph.TradingAgentsGraph", _FakeGraph)
        patcher.start()
        self.addCleanup(patcher.stop)
        _FakeGraph.fail_after_first = False

    def _start(self, task_id="t1"):
        a = self.analysis
        a._tasks[task_id] = {"status": "pending", "ticker": "TRENT.NS", "trade_date": "2026-11-23",
                             "analysts": ["market"], "started_at": 1e12, "progress": a._new_progress()}
        a._run_analysis_sync(task_id, "TRENT.NS", "2026-11-23", {}, ["market"])

    def test_snapshot_of_a_running_analysis(self):
        a = self.analysis
        a._tasks["r1"] = {"status": "running", "ticker": "TRENT.NS", "trade_date": "2026-11-23",
                          "started_at": 1e12, "progress": a._new_progress()}
        a._track("r1", {"type": "report", "section": "market_report", "content": "Firm"})
        a._track("r1", {"type": "debate", "side": "bull", "content": "Bull case"})
        a._track("r1", {"type": "heartbeat", "chunk": 3, "last_activity": "AIMessage: thinking"})
        got = a.get_analysis_result("r1")
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["progress"]["reports"], {"market_report": "Firm"})
        self.assertEqual(got["progress"]["debates"], {"bull": "Bull case"})
        self.assertEqual(got["progress"]["heartbeat"], "AIMessage: thinking")
        self.assertEqual([t["task_id"] for t in a.recent_tasks()], ["r1"])

    def test_completed_run_without_any_browser_is_saved(self):
        self._start("t1")
        got = self.analysis.get_analysis_result("t1")
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["signal"], "BUY")
        self.assertEqual([r["task_id"] for r in self.analysis.get_analysis_history()], ["t1"])
        # After a backend restart the in-memory task is gone; the saved result remains.
        self.analysis._tasks.clear()
        self.assertEqual(self.analysis.get_analysis_result("t1")["signal"], "BUY")

    def test_failure_is_kept_for_the_page(self):
        _FakeGraph.fail_after_first = True
        self._start("t2")
        got = self.analysis.get_analysis_result("t2")
        self.assertEqual(got["status"], "error")
        self.assertIn("LLM timed out", got["error"])
        self.assertEqual(got["progress"]["reports"], {"market_report": "Market looks firm."})
        self.assertEqual(self.analysis.recent_tasks()[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
