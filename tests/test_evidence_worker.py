import unittest
from datetime import datetime, timezone

from app.evidence_worker import article_mentions, evaluate_goplus, score_news


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


if __name__ == "__main__":
    unittest.main()
