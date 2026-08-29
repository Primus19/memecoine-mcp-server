import os
import tempfile
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.solana_early import (EarlyPolicy, Ledger, MicrocapLaunchPolicy, PumpfunEvPolicy, contract_safety_failures,
                              goplus_safety, json_request, public_onchain_candidates,
                              safety_failures, score_candidate, score_microcap_launch_candidate, score_pumpfun_ev_candidate,
                              strategy_diagnostics)


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
        "market_cap_usd": 5000, "trades_5m": 120, "volume_24h_usd": 25000,
        "safety_evidence_status": "VERIFIED", "price_change_5m_pct": 10,
        "price_change_15m_pct": 15,
    }
    value.update(changes)
    return value


class SolanaEarlyTests(unittest.TestCase):
    def test_public_http_throttle_honors_bounded_retry(self):
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {"Retry-After":"0"}, io.BytesIO())
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch("app.solana_early.urllib.request.urlopen", side_effect=[error, response]) as request, \
             patch("app.solana_early.time.sleep") as sleep:
            self.assertEqual({"ok": True}, json_request("https://example.test"))
        self.assertEqual(2, request.call_count)
        sleep.assert_called_once_with(2.0)

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

    def test_unavailable_safety_numbers_fail_closed_without_crashing_scan(self):
        value = candidate(
            safety_evidence_status="UNAVAILABLE", mint_authority_active=None,
            freeze_authority_active=None, transfer_hook_active=None,
            non_transferable=None, creator_selling=None,
            top10_holder_fraction=None, creator_fraction=None,
        )
        result = score_candidate(value, self.ledger, self.policy)
        self.assertFalse(result["qualified"])
        self.assertIn("verified safety evidence missing", result["failures"])
        self.assertIn("top-10 concentration unavailable", result["failures"])
        self.assertIsNone(result["top10_holder_fraction"])
        self.assertEqual("UNAVAILABLE", result["concentration_evidence_status"])

    def test_sell_simulation_is_mandatory(self):
        result = score_candidate(candidate(sell_simulation_ok=False), self.ledger, self.policy)
        self.assertFalse(result["qualified"])
        self.assertIn("sell simulation failed", result["failures"])
        self.assertFalse(result["paper_qualified"])

    def test_exploratory_paper_tier_relaxes_only_non_contract_gates(self):
        result = score_candidate(candidate(
            token_age_minutes=90, liquidity_usd=15000, unique_buyers_5m=20,
            unique_buyers_previous_5m=5, buy_volume_5m_usd=10000,
            buy_volume_previous_5m_usd=2000, sell_volume_5m_usd=1000,
            sell_price_impact_bps=200, top10_holder_fraction=.7,
            creator_fraction=.25, social_velocity_ratio=3, creator_history_score=5,
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
        request.return_value = {"code": 1, "result": {"mint1": {
            "mintable": "0", "freezable": False, "transfer_hook": "",
            "non_transferable": "0",
            "holders": [{"percent": "2.5"}, {"percent": "7.5"}],
            "creators": [{"percent": "5", "sell_all": "0"}],
        }}}
        result = goplus_safety("mint1")
        self.assertFalse(result["mint_authority_active"])
        self.assertFalse(result["freeze_authority_active"])
        self.assertFalse(result["transfer_hook_active"])
        self.assertAlmostEqual(.10, result["top10_holder_fraction"])
        self.assertAlmostEqual(.05, result["creator_fraction"])
        self.assertEqual("VERIFIED", result["safety_evidence_status"])

    @patch("app.solana_early.json_request")
    def test_goplus_missing_authority_flags_fail_closed(self, request):
        request.return_value = {"code": 1, "result": {"mint1": {
            "holders": [{"percent": ".1"}], "creators": [{"percent": ".01"}],
        }}}
        with self.assertRaisesRegex(RuntimeError, "fact missing"):
            goplus_safety("mint1")

    @patch("app.solana_early.json_request", return_value={"code": 5000, "message": "system error", "result": None})
    def test_goplus_provider_error_is_not_mislabeled_verified(self, _request):
        with self.assertRaisesRegex(RuntimeError, "provider error"):
            goplus_safety("mint1")

    @patch("app.solana_early.jupiter_sell_check", return_value=(True, 10.0))
    @patch("app.solana_early.goplus_safety", return_value={
        "mint_authority_active": False, "freeze_authority_active": False,
        "transfer_hook_active": False, "non_transferable": False,
        "top10_holder_fraction": .1, "creator_fraction": .01,
        "creator_selling": False, "safety_evidence_status": "VERIFIED",
    })
    @patch("app.solana_early.json_request")
    def test_public_discovery_needs_no_key_or_per_pool_info(self, request, _safety, _sell):
        request.return_value = {"data": [], "included": []}
        with patch.dict(os.environ, {"SOLANA_EARLY_MARKET_PAGES": "1"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "no candidate pools"):
                public_onchain_candidates(self.ledger)
        url, headers = request.call_args.args
        self.assertIn("api.geckoterminal.com/api/v2/networks/solana/new_pools", url)
        self.assertNotIn("x-cg-demo-api-key", headers)
        self.assertNotIn("/info", url)

    def test_discovery_scan_is_bounded_by_default(self):
        source = (Path(__file__).parents[1] / "app/solana_early.py").read_text()
        self.assertIn('SOLANA_EARLY_MAX_CANDIDATES", "20"', source)
        self.assertIn('scan_status="IN_PROGRESS"', source)
        self.assertIn('timeout=5.0', source)

    def test_pumpfun_ev_is_separate_and_paper_only(self):
        result = score_pumpfun_ev_candidate(candidate(), self.ledger, self.policy, PumpfunEvPolicy())
        self.assertEqual("SOLANA_PUMPFUN_EV_EXPERIMENT", result["strategy"])
        self.assertFalse(result["live_eligible"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["probability_calibrated"])
        self.assertEqual("5m_proxy", result["checkpoint"])
        self.assertGreater(result["ev_rank"], 0)

    def test_pumpfun_ev_requires_market_cap(self):
        result = score_pumpfun_ev_candidate(candidate(market_cap_usd=0), self.ledger,
                                            self.policy, PumpfunEvPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("market cap missing", result["paper_failures"])

    def test_pumpfun_ev_rejects_late_or_expensive_entry(self):
        result = score_pumpfun_ev_candidate(candidate(market_cap_usd=20000, token_age_minutes=45),
                                            self.ledger, self.policy, PumpfunEvPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("above pumpfun entry market-cap ceiling", result["paper_failures"])
        self.assertIn("outside pumpfun EV age window", result["paper_failures"])

    def test_pumpfun_ev_keeps_jupiter_sellability_hard(self):
        result = score_pumpfun_ev_candidate(candidate(sell_simulation_ok=False), self.ledger,
                                            self.policy, PumpfunEvPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("sell simulation failed", result["paper_failures"])

    def test_pumpfun_ev_rejects_dangerous_holder_concentration(self):
        result = score_pumpfun_ev_candidate(
            candidate(top10_holder_fraction=.90, creator_fraction=.80),
            self.ledger, self.policy, PumpfunEvPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("pumpfun top-10 concentration too high", result["paper_failures"])
        self.assertIn("pumpfun creator concentration too high", result["paper_failures"])

    def test_pumpfun_ev_requires_positive_cost_stressed_expectancy(self):
        result = score_pumpfun_ev_candidate(
            candidate(market_cap_usd=14000, trades_5m=5, buys_5m=3, sells_5m=2),
            self.ledger, self.policy, PumpfunEvPolicy(minimum_ev_rank=0),
        )
        self.assertFalse(result["paper_qualified"])
        self.assertTrue(any("cost-stressed expectancy" in x for x in result["paper_failures"]))

    def test_pumpfun_ev_records_plain_language_entry_reason(self):
        result = score_pumpfun_ev_candidate(candidate(), self.ledger, self.policy, PumpfunEvPolicy())
        self.assertIn("Divine V3 confirmed entry", result["entry_reason"])
        self.assertIn("cost-stressed expectancy", result["entry_reason"])

    def test_microcap_launch_qualifies_serious_run_as_paper_only(self):
        result = score_microcap_launch_candidate(candidate(), self.ledger, MicrocapLaunchPolicy())
        self.assertTrue(result["paper_qualified"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["live_eligible"])
        self.assertEqual("MICROCAP_LAUNCH_V1", result["strategy_version"])
        self.assertIn("$25,000 24h volume", result["entry_reason"])

    def test_microcap_launch_accepts_volume_upward_without_small_ceiling(self):
        result = score_microcap_launch_candidate(candidate(volume_24h_usd=250000), self.ledger,
                                                 MicrocapLaunchPolicy())
        self.assertTrue(result["paper_qualified"])

    def test_microcap_launch_rejects_weak_run_and_low_volume(self):
        result = score_microcap_launch_candidate(
            candidate(volume_24h_usd=19999, price_change_5m_pct=2,
                      transaction_buy_pressure=.10), self.ledger, MicrocapLaunchPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("microcap 24h volume below $20k minimum", result["paper_failures"])
        self.assertIn("microcap five-minute momentum outside serious-run range", result["paper_failures"])
        self.assertIn("microcap net buy pressure below minimum", result["paper_failures"])

    def test_microcap_launch_keeps_sellability_and_safety_mandatory(self):
        result = score_microcap_launch_candidate(
            candidate(sell_simulation_ok=False, safety_evidence_status="UNAVAILABLE"),
            self.ledger, MicrocapLaunchPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("sell simulation failed", result["paper_failures"])
        self.assertIn("verified safety evidence missing", result["paper_failures"])

    def test_microcap_launch_cannot_enter_live_executor_path(self):
        result = score_microcap_launch_candidate(candidate(), self.ledger, MicrocapLaunchPolicy())
        self.assertFalse(result["qualified"])
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        self.assertIn('candidates.filter(x=>x.qualified===true)', source)
        self.assertIn('candidates.filter(x=>x.paper_qualified===true)', source)

    def test_microcap_executor_retains_fast_exit_and_named_reporting(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        for expected in ("MICROCAP_DOWNTREND", "MICROCAP_PROFIT_PROTECTION",
                         "MAX_HOLD_20M", 'stop=isMicrocap?.08', 'target=isMicrocap?.20',
                         'strategyName(f.strategy)', "microcapLaunchV1Performance"):
            self.assertIn(expected, source)

    def test_strategy_diagnostics_counts_rejection_reasons(self):
        rows = [
            {"strategy":"SOLANA_PUMPFUN_EV_EXPERIMENT", "paper_qualified":False,
             "paper_failures":["market cap missing", "insufficient recent trades"]},
            {"strategy":"SOLANA_PUMPFUN_EV_EXPERIMENT", "paper_qualified":False,
             "paper_failures":["market cap missing"]},
            {"strategy":"SOLANA_EARLY_CONTROL", "paper_qualified":True, "paper_failures":[]},
        ]
        report = strategy_diagnostics(rows)
        self.assertEqual(2, report["SOLANA_PUMPFUN_EV_EXPERIMENT"]["evaluated"])
        self.assertEqual(2, report["SOLANA_PUMPFUN_EV_EXPERIMENT"]["top_rejections"][0]["count"])


if __name__ == "__main__":
    unittest.main()
