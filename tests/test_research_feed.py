import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.research_feed import ResearchFeed


class ResearchFeedTests(unittest.TestCase):
    def make_feed(self):
        directory = tempfile.TemporaryDirectory()
        variables = {
            "SIGNAL_FEED_BEARER_TOKEN": "feed-secret",
            "EXECUTOR_BASE_URL": "https://executor.example",
            "REST_API_TOKEN": "executor-secret",
            "RESEARCH_FEED_STATE_PATH": directory.name + "/state.json",
        }
        env = patch.dict(os.environ, variables, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(directory.cleanup)
        return ResearchFeed()

    @staticmethod
    def market(**updates):
        value = {
            "id": "test-coin", "symbol": "test", "current_price": .1,
            "market_cap": 100_000_000, "fully_diluted_valuation": 105_000_000,
            "total_volume": 25_000_000,
            "price_change_percentage_1h_in_currency": 1,
            "price_change_percentage_24h_in_currency": 8,
            "price_change_percentage_7d_in_currency": 20,
        }
        value.update(updates)
        return value

    @staticmethod
    def evidence(**updates):
        at = datetime.now(timezone.utc)
        value = {
            "product_id": "TEST-USDC", "identity_verified": True,
            "no_safety_veto": True, "safety_score": 15,
            "news_score": 8, "social_score": 3,
            "source_urls": ["https://official.example/event"],
            "observed_at": (at - timedelta(minutes=2)).isoformat(),
            "expires_at": (at + timedelta(hours=2)).isoformat(),
        }
        value.update(updates)
        return value

    def test_complete_candidate_is_built(self):
        feed = self.make_feed()
        at = datetime.now(timezone.utc)
        candidate, failures = feed.build_candidate(
            self.market(), {"product_id": "TEST-USDC", "price": .1},
            self.evidence(), {"classification": "RISING"}, at,
        )
        self.assertEqual([], failures)
        self.assertEqual("TEST-USDC", candidate["product_id"])
        self.assertGreaterEqual(sum(candidate["component_scores"].values()), 85)
        self.assertLessEqual(candidate["max_loss_usdc"], 2.5)
        self.assertTrue(candidate["signal_id"])

    def test_missing_evidence_fails_closed(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(), {"product_id": "TEST-USDC", "price": .1}, {},
            {"classification": "RISING"}, datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("identity not attested", failures)
        self.assertIn("verified news score below 4", failures)

    def test_extension_and_dilution_are_rejected(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(price_change_percentage_24h_in_currency=18, fully_diluted_valuation=400_000_000),
            {"product_id": "TEST-USDC", "price": .1}, self.evidence(),
            {"classification": "RISING"}, datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("24h momentum outside (0%,15%]", failures)
        self.assertIn("severe dilution", failures)

    def test_duplicate_symbols_are_excluded(self):
        result = ResearchFeed.unique_by_symbol([self.market(id="one"), self.market(id="two")])
        self.assertNotIn("TEST", result)

    def test_regime_requires_broad_positive_returns(self):
        markets = [self.market(id=str(i)) for i in range(6)]
        self.assertEqual("RISING", ResearchFeed.regime(markets)["classification"])
        for item in markets[:4]: item["price_change_percentage_24h_in_currency"] = -1
        self.assertEqual("MIXED", ResearchFeed.regime(markets)["classification"])


if __name__ == "__main__":
    unittest.main()
