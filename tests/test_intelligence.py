import tempfile
import unittest

from app.intelligence import IntelligenceLedger, classify_cohort


class IntelligenceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.ledger = IntelligenceLedger(self.tmp.name)

    def test_cohorts_distinguish_soft_near_miss_from_hard_execution_risk(self):
        cohort, hard, soft = classify_cohort({"failures": ["five-minute momentum below floor"]})
        self.assertEqual("NEAR_MISS_SHADOW", cohort)
        self.assertFalse(hard)
        self.assertTrue(soft)
        cohort, hard, _ = classify_cohort({"failures": ["no sell route"]})
        self.assertEqual("HARD_REJECT_CONTROL", cohort)
        self.assertTrue(hard)

    def test_snapshot_is_deduplicated_and_preserves_executable_checkpoints(self):
        snapshot = {
            "generated_at": "2026-09-01T00:00:00+00:00",
            "sources": {"solana": {"source": "https://example/report.json"}},
            "solana": {"postExitFollowups": [{
                "id": "follow:AAPL", "symbol": "AAPL", "strategy": "RUNNER_CAPTURE_V1",
                "strategyVersion": "V1", "entryUsd": 2, "realizedPnlUsd": .23,
                "costStressedPnlUsd": .21, "checkpoints": {
                    "15": {"executablePnlUsd": .5, "sell_route_ok": True},
                    "240": {"pnlVsEntryUsd": 15.6, "sell_route_ok": True},
                },
            }]},
        }
        first = self.ledger.ingest_snapshot(snapshot)
        second = self.ledger.ingest_snapshot(snapshot)
        self.assertEqual(1, first["observations_added"])
        self.assertEqual(2, first["checkpoints_added"])
        self.assertEqual(0, second["observations_added"])
        self.assertEqual(0, second["checkpoints_added"])
        report = self.ledger.report()
        self.assertEqual(1, report["totals"]["observations"])
        self.assertEqual(2, report["totals"]["checkpoints"])
        self.assertEqual(2, self.ledger.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0])

    def test_historical_services_and_promotion_gates_are_reported(self):
        for index, pnl in enumerate((.2, -.1, .3)):
            self.ledger.record("forex", "FOREX", "BRYNE_V5", {
                "id": f"trade-{index}", "instrument": "EUR_USD", "status": "CLOSED",
                "realized_pnl_usd": pnl, "cost_stressed_pnl_usd": pnl - .01,
            }, event_type="PAPER_CLOSE", mode="PAPER")
        self.ledger.extract_learnings()
        report = self.ledger.report()
        strategy = next(row for row in report["strategies"] if row["strategy"] == "BRYNE_V5")
        self.assertEqual(3, strategy["closed"])
        learning = next(row for row in report["learnings"] if row["strategy"] == "BRYNE_V5")
        self.assertEqual("PROMISING_INSUFFICIENT_SAMPLE", learning["status"])
        self.assertIn("100", learning["adoption_threshold"])
        self.assertFalse(report["promotion_policy"]["automatic_promotion"])

    def test_profit_giveback_becomes_a_retained_learning(self):
        self.ledger.record("forex", "FOREX", "FOREX_CONTROL", {
            "id": "nzd-loss", "instrument": "NZD_USD", "status": "CLOSED",
            "realized_pnl_usd": -.1126, "max_favorable_pnl_usd": .0815,
        }, event_type="BROKER_CLOSE", mode="LIVE")
        self.ledger.extract_learnings()
        learning = next(row for row in self.ledger.report()["learnings"]
                        if row["mechanism"] == "PROFIT_GIVEBACK")
        self.assertEqual("PROSPECTIVE_EXIT_EXPERIMENT", learning["status"])
        self.assertIn("turned a positive excursion", learning["statement"])

    def test_inactive_signals_and_forward_watchlists_become_shadow_evidence(self):
        snapshot = {
            "generated_at": "2026-09-01T01:00:00+00:00",
            "forex": {
                "outcomes": [{"id": "rejected-eurusd", "symbol": "EUR_USD",
                              "status": "REJECTED", "failures": ["score below minimum"]}],
                "five_streak": {"trades": [], "v4_ratchet_archived": {
                    "trades": [{"id": "v4-old", "symbol": "USD_JPY", "status": "CLOSED",
                                "realized_pnl_usd": -.12}]}}
            },
            "discovery": {"strategy_watchlists": {"SOLANA_MICROCAP_RUNNER_CAPTURE": [{
                "mint": "mint-aapl", "pool": "pool-aapl", "symbol": "AAPL",
                "status": "NEAR_MISS_SHADOW", "latest_candidate": {"price_usd": .01},
                "checkpoints": {"15": {"executablePnlUsd": .5, "sellRouteOk": True}},
            }]}}
        }
        result = self.ledger.ingest_snapshot(snapshot)
        self.assertEqual("", result["error"])
        self.assertEqual(3, result["observations_added"])
        self.assertEqual(1, result["checkpoints_added"])
        cohorts = {row[0] for row in self.ledger.db.execute("SELECT cohort FROM observations")}
        self.assertIn("NEAR_MISS_SHADOW", cohorts)

    def test_missing_concentration_creates_data_fix_not_live_relaxation(self):
        for index in range(12):
            self.ledger.record("discovery", "CRYPTO", "MICROCAP_V2", {
                "id": f"missing-{index}", "symbol": f"COIN{index}",
                "failures": ["creator concentration unavailable"],
            }, event_type="CANDIDATE_DECISION", mode="SHADOW")
        rec = next(row for row in self.ledger.report()["actionable_recommendations"]
                   if row["category"] == "DATA_COLLECTION_FIX")
        self.assertEqual("P0", rec["priority"])
        self.assertFalse(rec["live_change_allowed"])
        self.assertIn("95%", rec["adoption_threshold"])

    def test_positive_soft_rule_outcomes_create_shadow_only_experiment(self):
        for index in range(10):
            self.ledger.record("discovery", "CRYPTO", "RUNNER_V2", {
                "id": f"momentum-{index}", "symbol": f"COIN{index}",
                "failures": ["five-minute momentum below floor"],
                "checkpoints": {"60": {"executablePnlUsd": .10,
                                            "sellRouteOk": True}},
            }, event_type="SHADOW_FORWARD_PATH", mode="SHADOW")
        rec = next(row for row in self.ledger.report()["actionable_recommendations"]
                   if row["category"] == "SHADOW_EXPERIMENT")
        self.assertEqual("momentum_acceleration", rec["experiment"]["rule_family"])
        self.assertEqual("SHADOW_ONLY", rec["experiment"]["mode"])
        self.assertFalse(rec["live_change_allowed"])

    def test_negative_expectancy_blocks_promotion(self):
        self.ledger.record("forex", "FOREX", "FOREX_CONTROL", {
            "id": "loss", "instrument": "NZD_USD", "realized_pnl_usd": -.1,
            "cost_stressed_pnl_usd": -.12,
        }, event_type="BROKER_CLOSE", mode="LIVE")
        rec = next(row for row in self.ledger.report()["actionable_recommendations"]
                   if row["category"] == "PROMOTION_BLOCKED")
        self.assertIn("100 independent", rec["adoption_threshold"])
        self.assertFalse(rec["live_change_allowed"])


if __name__ == "__main__":
    unittest.main()
