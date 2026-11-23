"""Deep Analysis runs the Market (technical) analyst only (agreed 21 Nov 2026)."""

import unittest

from pydantic import ValidationError

from backend.models import ALLOWED_ANALYSTS, AnalysisRequest


class PriceOnlyAnalystsTest(unittest.TestCase):
    def test_default_is_market_only(self):
        req = AnalysisRequest(ticker="RELIANCE", trade_date="2026-11-20")
        self.assertEqual(req.analysts, ["market"])
        self.assertEqual(ALLOWED_ANALYSTS, ["market"])

    def test_news_social_fundamentals_rejected(self):
        for name in ("news", "social", "fundamentals"):
            with self.assertRaises(ValidationError):
                AnalysisRequest(ticker="RELIANCE", trade_date="2026-11-20", analysts=["market", name])

    def test_empty_list_falls_back_to_market(self):
        req = AnalysisRequest(ticker="RELIANCE", trade_date="2026-11-20", analysts=[])
        self.assertEqual(req.analysts, ["market"])


if __name__ == "__main__":
    unittest.main()
