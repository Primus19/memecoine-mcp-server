import os
import tempfile
import unittest
import urllib.error
import urllib.parse
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
        self.assertGreater(candidate["target_1_price"], .1)
        self.assertGreater(candidate["target_price"], candidate["target_1_price"])
        self.assertGreater(candidate["stop_price"], .092)
        self.assertEqual("MULTI_HORIZON_REALIZED_PROXY", candidate["volatility_method"])
        self.assertGreater(candidate["expected_net_bps_shadow"], 0)
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
        self.assertGreater(candidate["stop_price"], .095)
        self.assertLess(candidate["stop_price"], .1)

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

    def test_equal_scores_use_evidence_and_momentum_not_feed_order(self):
        base = {"component_scores": {"safety": 15, "news": 0, "momentum": 9},
                "change_1h_pct": 1, "product_id": "AAA-USDC"}
        stronger_news = {**base, "component_scores": {"safety": 15, "news": 4, "momentum": 5},
                         "product_id": "BBB-USDC"}
        values = [base, stronger_news]
        values.sort(key=ResearchFeed.candidate_rank_key)
        self.assertEqual("BBB-USDC", values[0]["product_id"])

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

    def test_account_key_is_disabled_by_default_and_polling_is_bounded(self):
        with patch.dict(os.environ, {
            "COINGECKO_API_KEY": "exhausted", "COINGECKO_USE_ACCOUNT_KEY": "false",
            "RESEARCH_SCAN_INTERVAL_SECONDS": "30",
        }, clear=False):
            feed = self.make_feed()
        self.assertEqual("", feed.cg_key)
        self.assertEqual(300, feed.interval)

    def test_market_discovery_is_not_limited_to_meme_category(self):
        feed = self.make_feed()
        with patch.object(feed, "fetch", return_value=[]) as fetch:
            feed.market_page(1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(fetch.call_args.args[0]).query)
        self.assertNotIn("category", query)
        self.assertEqual(["market_cap_desc"], query["order"])

    def test_default_discovery_covers_one_thousand_assets(self):
        with patch.dict(os.environ, {"RESEARCH_MARKET_PAGES":"4"}, clear=False):
            self.assertEqual(4, self.make_feed().pages)

    def test_stale_two_page_setting_cannot_narrow_broad_universe(self):
        with patch.dict(os.environ, {"RESEARCH_MARKET_PAGES":"2"}, clear=False):
            self.assertEqual(4, self.make_feed().pages)


if __name__ == "__main__":
    unittest.main()
