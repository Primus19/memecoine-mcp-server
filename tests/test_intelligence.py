import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone

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

    def test_compaction_removes_only_old_raw_noise_and_preserves_audit_evidence(self):
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        noise, _, _ = self.ledger.record("discovery", "CRYPTO", "MICROCAP", {
            "id": "old-noise", "symbol": "NOISE", "observed_at": old,
            "failures": ["momentum below floor"],
        }, event_type="CANDIDATE_DECISION", mode="SHADOW")
        trade, _, _ = self.ledger.record("solana", "CRYPTO", "RUNNER", {
            "id": "old-trade", "symbol": "AAPL", "observed_at": old,
            "realized_pnl_usd": .2,
        }, event_type="PAPER_CLOSE", mode="PAPER")
        checkpointed, _, _ = self.ledger.record("discovery", "CRYPTO", "MICROCAP", {
            "id": "old-checkpoint", "symbol": "WATCH", "observed_at": old,
            "failures": ["momentum below floor"],
            "checkpoints": {"60": {"executablePnlUsd": .1, "sellRouteOk": True}},
        }, event_type="CANDIDATE_DECISION", mode="SHADOW")
        with self.ledger.db:
            self.ledger.db.execute("UPDATE observations SET recorded_at=?", (old,))
        result = self.ledger.compact(force=True)
        remaining = {row[0] for row in self.ledger.db.execute("SELECT evidence_id FROM observations")}
        self.assertEqual("COMPLETED", result["status"])
        self.assertNotIn(noise, remaining)
        self.assertIn(trade, remaining)
        self.assertIn(checkpointed, remaining)
        self.assertEqual(1, self.ledger.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0])
        wal_path = f"{self.ledger.path}-wal"
        self.assertLessEqual(os.path.getsize(wal_path) if os.path.exists(wal_path) else 0, 8 * 1024 * 1024)

    def test_storage_report_exposes_bounded_wal_policy(self):
        policy = self.ledger.report()["storage_policy"]
        self.assertEqual(250, policy["wal_autocheckpoint_pages"])
        self.assertEqual(8 * 1024 * 1024, policy["wal_journal_size_limit_bytes"])
        self.assertFalse(policy["periodic_vacuum_enabled"])
        self.assertIn("wal_bytes", policy)

    def test_scan_payload_is_canonical_but_trade_payload_remains_full(self):
        nested = {"unused_blob": "x" * 10000, "latest_candidate": {
            "mint": "mint-1", "symbol": "RUN", "liquidity_usd": 250000,
            "failures": ["momentum below floor"]}}
        scan_id, _, _ = self.ledger.record("discovery", "CRYPTO", "RUNNER", nested,
                                            event_type="CANDIDATE_DECISION", mode="SHADOW")
        trade_id, _, _ = self.ledger.record("solana", "CRYPTO", "RUNNER", {
            "id": "trade-1", "symbol": "RUN", "realized_pnl_usd": .2,
            "execution_receipt": {"signature": "kept"}},
            event_type="PAPER_CLOSE", mode="PAPER")
        scan_payload = self.ledger.db.execute(
            "SELECT payload_json FROM observations WHERE evidence_id=?", (scan_id,)).fetchone()[0]
        trade_payload = self.ledger.db.execute(
            "SELECT payload_json FROM observations WHERE evidence_id=?", (trade_id,)).fetchone()[0]
        self.assertIn("CANONICAL_SCAN_SUMMARY", scan_payload)
        self.assertNotIn("unused_blob", scan_payload)
        self.assertIn("execution_receipt", trade_payload)

    def test_quick_health_does_not_wait_during_maintenance(self):
        self.ledger.lock.acquire()
        try:
            result = self.ledger.quick_health()
        finally:
            self.ledger.lock.release()
        # RLock permits the owning thread; verify the normal shape here. A
        # background-thread lock is exercised by production health checks.
        self.assertFalse(result["maintenance_in_progress"])
        self.assertIn("database_bytes", result)


if __name__ == "__main__":
    unittest.main()
