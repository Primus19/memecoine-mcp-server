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
                          "minimum_score":80, "reason":"score below minimum"}],
            "intents": [{"created_at":"2026-08-24T03:00:00Z", "symbol":"EUR_USD",
                          "side":"BUY", "mode":"LIVE", "status":"OPEN", "score":82,
                          "entry_reason":"Multi Horizon Buy signal; score 82 met minimum 80."}],
            "events": [],
            "model_review": {"model_version":"FOREX_TREND_1.1", "sample_size":0,
                             "status":"MODEL LOCKED - COLLECTING EVIDENCE"},
            "risk_configuration": {"minimum_score":80,"maximum_open_positions":1,
                "maximum_risk_per_trade_usd":.5,"maximum_combined_risk_usd":.5,
                "current_open_risk_usd":0,"daily_loss_limit_usd":2.5,
                "maximum_notional_usd":50,"maximum_margin_used_usd":5,"currency_overlap_guard":True},
            "cross_strategy_learning": {"live_forex_observations": [{
                "eligible": True, "trade_id": "10", "instrument": "EUR_JPY",
                "current_r": .3, "maximum_favorable_r": .8,
                "protected_floor_r": .5, "would_exit_now": True}]},
        })
        self.assertIn("Production Forex Dashboard", body)
        self.assertIn("Five-Streak paper experiment", body)
        self.assertIn("Bryne and Lot-Bill Strategy", body)
        self.assertIn("Signal UTC", body)
        self.assertIn("Market and calendar", body)
        self.assertIn("Auditable positions and intents", body)
        self.assertIn("PRACTICE_ARMED", body)
        self.assertIn("76.10 / 80.00", body)
        self.assertIn("Model quality and guarded improvement", body)
        self.assertIn("Effective guardrails", body)
        self.assertIn("Position limit:</b> 1", body)
        self.assertIn("Position-limit reconciliation", body)
        self.assertIn("Main reason for entry", body)
        self.assertIn("Multi Horizon Buy signal; score 82 met minimum 80.", body)
        self.assertIn("PURPLE • Bryne and Lot-Bill Strategy", body)
        self.assertIn("BLUE • Live Control Strategy", body)
        self.assertIn("Cross-strategy learning", body)
        self.assertIn("WOULD EXIT", body)
        self.assertIn("SHADOW ONLY", body)
        self.assertNotIn("<script", body.lower())


if __name__ == "__main__": unittest.main()
