"""Kite open positions (positions()["net"]) are normalised separately from holdings (asked 23 Nov 2026)."""

import unittest

from backend.brokers.kite import KiteConfigError, normalize_positions


class KiteOpenPositionsTest(unittest.TestCase):
    def test_open_rows_only_with_side_and_exposure(self):
        rows = normalize_positions({"net": [
            {"tradingsymbol": "NIFTY25DECFUT", "exchange": "NFO", "product": "NRML", "quantity": -75,
             "multiplier": 1, "average_price": 24000, "last_price": 23900, "pnl": 7500, "unrealised": 7500,
             "realised": 0, "m2m": 7500},
            {"tradingsymbol": "GOLDM25DECFUT", "exchange": "MCX", "product": "NRML", "quantity": 1,
             "multiplier": 10, "average_price": 76000, "last_price": 76100, "pnl": 1000},
            {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 0, "realised": 120},
        ], "day": []})
        self.assertEqual([r["tradingsymbol"] for r in rows], ["GOLDM25DECFUT", "NIFTY25DECFUT"])
        gold, nifty = rows
        self.assertEqual(nifty["side"], "short")
        self.assertEqual(nifty["exposure"], 75 * 23900)
        self.assertEqual(gold["unrealised"], 1000.0)   # computed when Kite leaves it out
        self.assertEqual(gold["exposure"], 761000.0)

    def test_invalid_snapshot_rejected(self):
        with self.assertRaises(KiteConfigError):
            normalize_positions([])


if __name__ == "__main__":
    unittest.main()
