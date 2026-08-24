import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
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
        self.assertIn("regime", candidate["component_scores"])
        self.assertIn("liquidity", candidate["component_scores"])
        self.assertEqual(1, candidate["change_1h_pct"])
        self.assertEqual(.25, candidate["turnover"])
        self.assertAlmostEqual(.106, candidate["target_1_price"])
        self.assertAlmostEqual(.12, candidate["target_price"])
        self.assertEqual(5, candidate["trail_activation_pct"])
        self.assertEqual(3, candidate["trail_pct"])
        self.assertEqual("ESTABLISHED", candidate["opportunity_tier"])

    def test_emerging_candidate_uses_smaller_capital_and_higher_score(self):
        feed = self.make_feed()
        at = datetime.now(timezone.utc)
        market = self.market(market_cap=15_000_000, fully_diluted_valuation=16_000_000,
                             total_volume=2_000_000)
        candidate, failures = feed.build_candidate(
            market, {"product_id": "TEST-USDC", "price": .1},
            self.evidence(news_score=10, social_score=5), {"classification": "RISING"}, at,
        )
        self.assertEqual([], failures)
        self.assertEqual("EMERGING", candidate["opportunity_tier"])
        self.assertEqual(5.0, candidate["notional_usdc"])
        self.assertEqual(.25, candidate["max_loss_usdc"])
        self.assertAlmostEqual(.095, candidate["stop_price"])

    def test_too_thin_coin_remains_ineligible(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(market_cap=5_000_000, total_volume=500_000),
            {"product_id": "TEST-USDC", "price": .1}, self.evidence(),
            {"classification": "RISING"}, datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("market cap or volume below all policy tiers", failures)

    def test_shadow_outcome_tracks_rejected_candidate(self):
        feed = self.make_feed(); at = datetime.now(timezone.utc)
        market = self.market(price_change_percentage_1h_in_currency=-1)
        components, failures = feed.score(market, self.evidence(), {"classification": "MIXED"})
        feed.record_shadow("TEST-USDC", market, failures, components, at)
        feed.update_shadow_outcomes({"TEST": {**market, "current_price": .11}}, at + timedelta(minutes=5))
        summary = feed.shadow_summary()
        self.assertEqual(1, summary["sample_size"])
        self.assertAlmostEqual(10, summary["best_missed_return_pct"])

    def test_missing_evidence_fails_closed(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(), {"product_id": "TEST-USDC", "price": .1}, {},
            {"classification": "RISING"}, datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("identity not attested", failures)

    def test_clean_established_asset_does_not_require_positive_news(self):
        feed = self.make_feed()
        at = datetime.now(timezone.utc)
        candidate, failures = feed.build_candidate(
            self.market(), {"product_id": "TEST-USDC", "price": .1},
            self.evidence(news_score=0, social_score=0), {"classification": "MIXED"}, at,
        )
        self.assertEqual([], failures)
        self.assertEqual("MIXED", candidate["regime"])
        self.assertEqual(78, sum(candidate["component_scores"].values()))

    def test_news_veto_still_rejects(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(), {"product_id": "TEST-USDC", "price": .1},
            self.evidence(news_veto=True), {"classification": "RISING"},
            datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("news veto active", failures)

    def test_extension_and_dilution_are_rejected(self):
        feed = self.make_feed()
        candidate, failures = feed.build_candidate(
            self.market(price_change_percentage_24h_in_currency=18, fully_diluted_valuation=400_000_000),
            {"product_id": "TEST-USDC", "price": .1}, self.evidence(),
            {"classification": "RISING"}, datetime.now(timezone.utc),
        )
        self.assertIsNone(candidate)
        self.assertIn("24h momentum outside policy range", failures)
        self.assertIn("severe dilution", failures)

    def test_duplicate_symbols_are_excluded(self):
        result = ResearchFeed.unique_by_symbol([self.market(id="one"), self.market(id="two")])
        self.assertNotIn("TEST", result)

    def test_regime_requires_broad_positive_returns(self):
        markets = [self.market(id=str(i)) for i in range(6)]
        self.assertEqual("RISING", ResearchFeed.regime(markets)["classification"])
        for item in markets[:4]: item["price_change_percentage_24h_in_currency"] = -1
        self.assertEqual("MIXED", ResearchFeed.regime(markets)["classification"])

    def test_regime_classifies_broad_short_term_decline_as_falling(self):
        markets = [self.market(id=str(i), price_change_percentage_1h_in_currency=-1, price_change_percentage_24h_in_currency=-3) for i in range(6)]
        self.assertEqual("FALLING", ResearchFeed.regime(markets)["classification"])

    def test_rate_limit_retries_and_honors_retry_after(self):
        feed = self.make_feed()
        feed.http_max_retries = 2
        error = urllib.error.HTTPError(
            "https://api.example/data", 429, "Too Many Requests",
            {"Retry-After": "0"}, BytesIO(b"rate limited"),
        )

        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def read(self): return b'{"ok": true}'

        with patch("urllib.request.urlopen", side_effect=[error, Response()]) as request, \
             patch("time.sleep") as sleep:
            self.assertEqual({"ok": True}, feed.fetch("https://api.example/data"))
        self.assertEqual(2, request.call_count)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
