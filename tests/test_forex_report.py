import unittest

from app.forex_report import render_forex_report


class ForexReportTests(unittest.TestCase):
    def test_report_is_email_safe_and_contains_required_sections(self):
        body = render_forex_report({
            "generated_at": "2026-08-24T03:00:00+00:00", "mode": "PRACTICE_ARMED",
            "executor_ready": True, "broker": {"nav": 1000, "balance": 1000, "margin_available": 995},
            "capital_baseline_nav": 1000, "daily_baseline_nav": 1000,
            "snapshots": [{"symbol": "EUR_USD", "price": 1.17,
            "change_1h_pct": .1, "change_24h_pct": .2, "spread_bps": 1.2, "calendar_verified": True}],
            "outcomes": [{"symbol":"EUR_USD", "status":"REJECTED", "score":76.1,
                          "minimum_score":80, "reason":"score below minimum"}], "intents": [], "events": [],
        })
        self.assertIn("Production Forex Dashboard", body)
        self.assertIn("Market and calendar", body)
        self.assertIn("Auditable positions and intents", body)
        self.assertIn("PRACTICE_ARMED", body)
        self.assertIn("76.10 / 80.00", body)
        self.assertNotIn("<script", body.lower())


if __name__ == "__main__": unittest.main()
