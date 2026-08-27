import tempfile
import unittest
from unittest.mock import patch

from app.solana_early import (EarlyPolicy, Ledger, contract_safety_failures,
                              goplus_safety, safety_failures, score_candidate)


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
        self.assertFalse(result["paper_qualified"])

    def test_exploratory_paper_tier_relaxes_only_non_contract_gates(self):
        result = score_candidate(candidate(
            token_age_minutes=180, liquidity_usd=5000, unique_buyers_5m=20,
            unique_buyers_previous_5m=5, buy_volume_5m_usd=10000,
            buy_volume_previous_5m_usd=2000, sell_volume_5m_usd=1000,
            sell_price_impact_bps=400, top10_holder_fraction=.8,
            creator_fraction=.4, social_velocity_ratio=3, creator_history_score=3,
        ), self.ledger, self.policy)
        self.assertFalse(result["qualified"])
        self.assertTrue(result["paper_qualified"])
        self.assertEqual([], result["paper_failures"])

    def test_paper_tier_never_relaxes_token_controls(self):
        value = candidate(mint_authority_active=True, top10_holder_fraction=.8,
                          creator_fraction=.4)
        result = score_candidate(value, self.ledger, self.policy)
        self.assertIn("mint_authority_active", contract_safety_failures(value))
        self.assertFalse(result["paper_qualified"])

    def test_paper_tier_never_relaxes_sellability(self):
        result = score_candidate(candidate(sell_price_impact_bps=501), self.ledger, self.policy)
        self.assertFalse(result["paper_qualified"])
        self.assertIn("paper sell price impact above maximum", result["paper_failures"])

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

    def test_wallet_events_close_timestamped_outcomes(self):
        for index in range(20):
            opened = f"2026-08-26T12:{index:02d}:00+00:00"
            closed = f"2026-08-26T13:{index:02d}:00+00:00"
            self.assertTrue(self.ledger.store_wallet_event({"signature": f"b{index}", "wallet": "proven",
                "mint": f"m{index}", "side": "BUY", "quantity": 10, "quote_usdc": 1, "observed_at": opened}))
            self.assertTrue(self.ledger.store_wallet_event({"signature": f"s{index}", "wallet": "proven",
                "mint": f"m{index}", "side": "SELL", "quantity": 10, "quote_usdc": 1.2, "observed_at": closed}))
        stats = self.ledger.wallet_stats("proven")
        self.assertEqual(20, stats["sample_size"])
        self.assertAlmostEqual(1, stats["win_rate"])
        self.assertAlmostEqual(.2, stats["mean_return"])

    def test_duplicate_wallet_event_is_idempotent(self):
        event = {"signature": "same", "wallet": "w", "mint": "m", "side": "BUY",
                 "quantity": 1, "quote_usdc": 1, "observed_at": "2026-08-26T12:00:00+00:00"}
        self.assertTrue(self.ledger.store_wallet_event(event))
        self.assertFalse(self.ledger.store_wallet_event(event))

    @patch("app.solana_early.json_request")
    def test_goplus_primitive_zero_flags_are_inactive(self, request):
        request.return_value = {"result": {"mint1": {
            "mintable": "0", "freezable": False, "transfer_hook": "",
            "non_transferable": "0",
            "holders": [{"percent": "2.5"}, {"percent": "7.5"}],
            "creators": [{"percent": "5", "sell_all": "0"}],
        }}}
        result = goplus_safety("mint1")
        self.assertFalse(result["mint_authority_active"])
        self.assertFalse(result["freeze_authority_active"])
        self.assertAlmostEqual(.10, result["top10_holder_fraction"])
        self.assertAlmostEqual(.05, result["creator_fraction"])

    @patch("app.solana_early.json_request")
    def test_goplus_missing_authority_flags_fail_closed(self, request):
        request.return_value = {"result": {"mint1": {
            "holders": [{"percent": ".1"}], "creators": [{"percent": ".01"}],
        }}}
        result = goplus_safety("mint1")
        self.assertTrue(result["mint_authority_active"])
        self.assertTrue(result["freeze_authority_active"])


if __name__ == "__main__":
    unittest.main()
