"""News feed keeps market/economy articles and drops sport, travel and politics (asked 23 Nov 2026)."""

import unittest

from backend.news_sources import is_market_relevant


class NewsFilterTest(unittest.TestCase):
    def test_off_topic_dropped(self):
        for title in ("India beat Australia by 6 wickets in 2nd T20",
                      "IndiGo cancels 200 flights amid fog",
                      "BJP wins Bihar assembly election, Congress concedes",
                      "PM addresses rally in Patna"):
            self.assertFalse(is_market_relevant(title), title)

    def test_market_kept(self):
        for title in ("Sensex rallies 600 points as FIIs return",
                      "RBI keeps repo rate unchanged, inflation forecast lowered",
                      "Reliance Q2 results: profit up 9%",
                      "Gold prices hit record as dollar weakens"):
            self.assertTrue(is_market_relevant(title), title)

    def test_off_topic_word_with_strong_market_context_kept(self):
        self.assertTrue(is_market_relevant("IndiGo shares fall 4% after flight cancellations, brokerages cut target price"))
        self.assertTrue(is_market_relevant("Election results: what it means for markets and stocks"))

    def test_summary_html_is_read(self):
        self.assertTrue(is_market_relevant("Big move today", "<p>The <b>Nifty</b> closed higher</p>"))


if __name__ == "__main__":
    unittest.main()
