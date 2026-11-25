"""MCX one-lot risk limit (agreed 25 Nov 2026): sizing stays 1% of capital, but when even one lot
risks more, MCX still takes 1 lot if it risks ≤ 3.5% (₹35,000). Open risk counts the real risk."""

import unittest
from datetime import date

from tests._support import IsolatedStateTestCase


class LotsWithinRiskTests(unittest.TestCase):
    def test_rule(self):
        from backend.decision.config import lots_within_risk
        self.assertEqual(lots_within_risk("mcx", 2_000), (5, None))            # crude: 1% sizing, not sized up
        lots, note = lots_within_risk("mcx", 21_144)                          # silver 27 Oct breakdown
        self.assertEqual(lots, 1)
        self.assertIn("2.1% of capital", note)
        self.assertEqual(lots_within_risk("mcx", 35_000)[0], 1)
        self.assertEqual(lots_within_risk("mcx", 35_001), (0, None))
        self.assertEqual(lots_within_risk("stock_futures", 21_144), (0, None))  # NSE unchanged

    def test_tracker_sizes_like_the_scan(self):
        from backend.journal.tracker import _size
        rec = {"segment": "mcx", "extra": {"value_per_point": 5}}
        self.assertEqual(_size(rec, 200000, 4000), (1, 5.0))                  # 1 lot risks ₹20,000
        self.assertEqual(_size({**rec, "segment": "stock_futures"}, 200000, 4000), (0, 5.0))


class OpenRiskTests(IsolatedStateTestCase, unittest.TestCase):
    def add(self, symbol, segment="mcx", lots=1, vpp=5, entry=200000.0, stop=206000.0):
        from backend.journal.store import add_records
        add_records([{"segment": segment, "symbol": symbol, "instrument_token": 1, "exchange": "MCX",
                      "signal_date": "2026-11-20", "signal_ts": "2026-11-20T10:00:00", "setup": "breakout",
                      "direction": "short", "decision": "approved", "entry": entry, "stop": stop,
                      "target": 182000.0, "entry_date": "2026-11-20", "fill_until": "2026-11-20T12:00:00",
                      "time_stop_date": "2027-02-25", "extra": {"lots": lots, "value_per_point": vpp}}])

    def test_open_risk_uses_each_positions_real_risk(self):
        from backend.journal.panel import limit_block
        today = date(2026, 11, 25)
        self.add("SILVERM")                 # 1 lot × 6,000 × 5 = ₹30,000 = 3%
        self.add("GOLDM", vpp=10, entry=120000.0, stop=123000.0)   # ₹30,000 = 3%
        self.assertIsNone(limit_block(today, "mcx", 1.0))           # 6% + 1%
        self.add("CRUDEOILM", vpp=10, lots=10, entry=7500.0, stop=7650.0)   # ₹15,000 = 1.5% → 7.5% open
        self.assertIsNone(limit_block(today, "stock_futures", 1.0))
        self.assertIn("open risk would exceed 10.0% (7.5% open + 3.5% new)", limit_block(today, "mcx", 3.5))


if __name__ == "__main__":
    unittest.main()
