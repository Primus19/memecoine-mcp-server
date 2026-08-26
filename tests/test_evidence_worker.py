import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import Mock

from app.evidence_worker import (
    EvidenceAdapter,
    article_mentions,
    canonical_contract,
    evaluate_goplus,
    evaluate_goplus_solana,
    score_news,
)
from app.policy import OpportunityPolicy


class EvidenceWorkerTests(unittest.TestCase):
    def test_requires_two_independent_positive_sources(self):
        articles = [
            {"title": "Turbo announces exchange listing", "summary": "", "link": "https://one.example/turbo"},
            {"title": "TURBO listing expands access", "summary": "", "link": "https://two.example/turbo"},
        ]
        score, veto, urls = score_news(articles, name="Turbo", symbol="TURBO")
        self.assertEqual(4, score)
        self.assertFalse(veto)
        self.assertEqual(2, len(urls))

    def test_single_source_cannot_create_news_score(self):
        score, veto, _ = score_news(
            [{"title": "Turbo announces listing", "summary": "", "link": "https://one.example/turbo"}],
            name="Turbo", symbol="TURBO",
        )
        self.assertEqual(0, score)
        self.assertFalse(veto)

    def test_two_source_security_event_is_veto(self):
        articles = [
            {"title": "Turbo exploit reported", "summary": "", "link": "https://one.example/turbo"},
            {"title": "TURBO hacked", "summary": "", "link": "https://two.example/turbo"},
        ]
        score, veto, _ = score_news(articles, name="Turbo", symbol="TURBO")
        self.assertEqual(0, score)
        self.assertTrue(veto)

    def test_short_ambiguous_symbol_does_not_match(self):
        self.assertFalse(article_mentions({"title": "A dog runs", "summary": ""}, name="Dogecoin", symbol="DOG"))

    def test_complete_clean_goplus_result_passes(self):
        clean, score, failures = evaluate_goplus({
            "is_honeypot": "0", "is_blacklisted": "0", "cannot_sell_all": "0",
            "is_mintable": "0", "hidden_owner": "0", "buy_tax": "0", "sell_tax": "0",
            "holders": [{"percent": "0.02"}, {"percent": "0.03"}],
        })
        self.assertTrue(clean)
        self.assertEqual(15, score)
        self.assertEqual([], failures)

    def test_missing_security_fields_fail_closed(self):
        clean, score, failures = evaluate_goplus({})
        self.assertFalse(clean)
        self.assertEqual(0, score)
        self.assertIn("is_honeypot", failures)

    def test_canonical_platform_wins_over_bridged_contracts(self):
        detail = {
            "asset_platform_id": "ethereum",
            "platforms": {"ethereum": "0xcanonical", "base": "0xbridged"},
        }
        self.assertEqual(("ethereum", "0xcanonical"), canonical_contract(detail))

    def test_ambiguous_platform_without_canonical_fails_closed(self):
        detail = {"platforms": {"ethereum": "0xone", "base": "0xtwo"}}
        self.assertEqual(("", ""), canonical_contract(detail))

    def test_clean_solana_security_result_passes(self):
        clean, score, failures = evaluate_goplus_solana({
            "mintable": {"status": "0"},
            "freezable": {"status": "0"},
            "closable": {"status": "0"},
            "balance_mutable_authority": {"status": "0"},
            "non_transferable": "0",
            "default_account_state": "1",
            "holders": [{"percent": "0.03"}, {"percent": "0.02"}],
        })
        self.assertTrue(clean)
        self.assertEqual(15, score)
        self.assertEqual([], failures)

    def test_solana_dangerous_authorities_fail(self):
        clean, score, failures = evaluate_goplus_solana({
            "mintable": {"status": "1"},
            "freezable": {"status": "0"},
            "closable": {"status": "0"},
            "balance_mutable_authority": {"status": "0"},
            "holders": [{"percent": "0.01"}],
        })
        self.assertFalse(clean)
        self.assertEqual(0, score)
        self.assertIn("mintable", failures)

    def test_coin_detail_is_cached(self):
        adapter = EvidenceAdapter.__new__(EvidenceAdapter)
        adapter.detail_cache = {}
        adapter.cg_base = "https://api.example"
        adapter.json = Mock(return_value={"id": "turbo", "symbol": "turbo"})
        first = adapter.coin_detail("turbo")
        second = adapter.coin_detail("turbo")
        self.assertEqual(first, second)
        adapter.json.assert_called_once()

    def test_native_asset_does_not_require_a_token_contract(self):
        adapter = EvidenceAdapter.__new__(EvidenceAdapter)
        clean, score, failures, identity = adapter.security({"asset_platform_id": None, "platforms": {}})
        self.assertTrue(clean)
        self.assertEqual(15, score)
        self.assertEqual([], failures)
        self.assertEqual("NATIVE_ASSET", identity)

    def test_unresolved_token_contract_still_fails_closed(self):
        adapter = EvidenceAdapter.__new__(EvidenceAdapter)
        clean, score, failures, _ = adapter.security({"platforms": {"ethereum": "", "base": ""}})
        self.assertFalse(clean)
        self.assertEqual(0, score)
        self.assertIn("canonical contract platform unavailable", failures)

    def test_market_page_uses_requested_page_for_full_research_coverage(self):
        adapter = EvidenceAdapter.__new__(EvidenceAdapter)
        adapter.cg_base = "https://api.example"
        adapter.json = Mock(return_value=[])

        adapter.market_page(2)

        self.assertIn("page=2", adapter.json.call_args.args[0])

    def test_market_page_is_not_restricted_to_meme_tokens(self):
        adapter = EvidenceAdapter.__new__(EvidenceAdapter)
        adapter.cg_base = "https://api.example"
        adapter.json = Mock(return_value=[])
        adapter.market_page(1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(adapter.json.call_args.args[0]).query)
        self.assertNotIn("category", query)

    def test_evidence_policy_includes_documented_emerging_tier(self):
        policy = OpportunityPolicy()
        self.assertEqual("EMERGING", policy.tier(15_000_000, 2_000_000))


if __name__ == "__main__":
    unittest.main()
