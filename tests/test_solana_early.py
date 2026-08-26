import tempfile
import unittest

from app.solana_early import EarlyPolicy, Ledger, safety_failures, score_candidate


def candidate(**changes):
    value = {
        "mint": "mint1", "symbol": "EARLY", "observed_at": "2026-08-26T12:00:00+00:00",
        "token_age_minutes": 12, "liquidity_usd": 80000, "unique_buyers_5m": 100,
        "unique_buyers_previous_5m": 35, "buy_volume_5m_usd": 50000,
        "buy_volume_previous_5m_usd": 12000, "sell_volume_5m_usd": 9000,
        "sell_price_impact_bps": 80, "mint_authority_active": False,
        "freeze_authority_active": False, "transfer_hook_active": False,
        "non_transferable": False, "creator_selling": False, "sell_simulation_ok": True,
        "top10_holder_fraction": .20, "creator_fraction": .02,
        "social_velocity_ratio": 3, "creator_history_score": 3, "buyer_wallets": [],
    }
    value.update(changes)
    return value


class SolanaEarlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.ledger = Ledger(self.tmp.name)
        self.policy = EarlyPolicy()

    def test_strong_acceleration_qualifies(self):
        result = score_candidate(candidate(), self.ledger, self.policy)
        self.assertTrue(result["qualified"])
        self.assertGreaterEqual(result["score"], 78)
        self.assertEqual("PAPER_ONLY", result["mode"])

    def test_active_mint_authority_fails_closed(self):
        value = candidate(mint_authority_active=True)
        self.assertIn("mint_authority_active", safety_failures(value, self.policy))
        self.assertFalse(score_candidate(value, self.ledger, self.policy)["qualified"])

    def test_sell_simulation_is_mandatory(self):
        result = score_candidate(candidate(sell_simulation_ok=False), self.ledger, self.policy)
        self.assertFalse(result["qualified"])
        self.assertIn("sell simulation failed", result["failures"])

    def test_one_lucky_wallet_receives_no_credit(self):
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO wallet_outcomes VALUES(?,?,?,?,?)",
                                   ("lucky", "x", "2026-01-01", "2026-01-02", 5.0))
        result = score_candidate(candidate(buyer_wallets=["lucky"]), self.ledger, self.policy)
        self.assertEqual(0, result["qualified_wallet_count"])

    def test_wallet_requires_repeatable_history(self):
        with self.ledger.db:
            for index in range(20):
                self.ledger.db.execute("INSERT INTO wallet_outcomes VALUES(?,?,?,?,?)",
                    ("proven", f"t{index}", f"2026-01-{index+1:02d}", "2026-02-01", .2 if index < 15 else -.1))
        result = score_candidate(candidate(buyer_wallets=["proven"]), self.ledger, self.policy)
        self.assertEqual(1, result["qualified_wallet_count"])
        self.assertEqual(2.5, result["components"]["smart_wallets"])

    def test_concentration_and_slow_flow_reject(self):
        result = score_candidate(candidate(top10_holder_fraction=.8,
                                           unique_buyers_previous_5m=95,
                                           buy_volume_previous_5m_usd=49000), self.ledger, self.policy)
        self.assertFalse(result["qualified"])
        self.assertIn("top-10 concentration too high", result["failures"])
        self.assertIn("buyers are not accelerating", result["failures"])


if __name__ == "__main__":
    unittest.main()
