"""GET /api/journal/ai-today: only today's AI-approved trades, with BUY/SELL and confidence."""

import json
import unittest
from datetime import date, timedelta

from tests._support import IsolatedStateTestCase, fresh_test_client, login


class AiTodayTests(IsolatedStateTestCase, unittest.TestCase):
    def test_lists_only_todays_approved(self):
        from backend.journal.store import add_records, list_records, update_record
        today, old = date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()
        base = {"segment": "stock_futures", "instrument_token": 1, "exchange": "NFO", "setup": "pullback",
                "regime": "STRONG_UP", "score": 70, "entry": 100, "stop": 95, "target": 110,
                "signal_date": today, "entry_date": today, "fill_until": f"{today}T15:00", "time_stop_date": today}
        add_records([{**base, "symbol": "AAA", "direction": "long", "signal_ts": f"{today}T10:15", "decision": "shortlisted",
                      "extra": {"tradingsymbol": "AAA26NOVFUT"}},
                     {**base, "symbol": "BBB", "direction": "short", "signal_ts": f"{today}T11:15", "decision": "shortlisted"},
                     {**base, "symbol": "CCC", "direction": "long", "signal_ts": f"{old}T11:15", "decision": "shortlisted"}])
        recs = {r["symbol"]: r for r in list_records()}
        update_record(recs["AAA"]["id"], {"decision": "approved", "panel_decision": "approve",
                                          "panel": {"evaluated_at": f"{today}T10:20:00", "confidence": 72}})
        update_record(recs["BBB"]["id"], {"decision": "rejected_panel", "panel_decision": "reject",
                                          "panel": {"evaluated_at": f"{today}T11:20:00", "confidence": 80}})
        update_record(recs["CCC"]["id"], {"decision": "approved", "panel_decision": "approve",
                                          "panel": {"evaluated_at": f"{old}T11:20:00", "confidence": 65}})
        with fresh_test_client() as client:
            login(client)
            body = client.get("/api/journal/ai-today").json()
        self.assertEqual([(p["name"], p["action"], p["confidence"]) for p in body["picks"]],
                         [("AAA26NOVFUT", "BUY", 72)])
        self.assertEqual(body["reviewed"], 2)


if __name__ == "__main__":
    unittest.main()
