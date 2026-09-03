import json
import os
import tempfile
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.solana_early import (EarlyPolicy, Ledger, MicrocapLaunchPolicy, PumpfunEvPolicy,
                              RunnerCapturePolicy, contract_safety_failures,
                              goplus_safety, json_request, public_onchain_candidates,
                              solana_rpc_mint_safety,
                              safety_failures, score_candidate, score_microcap_launch_candidate,
                              score_microcap_sub_million_shadow,
                              score_pumpfun_ev_candidate, score_runner_capture_candidate,
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
        "market_cap_usd": 1_250_000, "trades_5m": 120, "volume_24h_usd": 125000,
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

    def test_onchain_verified_mint_is_eligible_only_for_contract_safety_lane(self):
        value = candidate(
            safety_evidence_status="ONCHAIN_VERIFIED",
            top10_holder_fraction=None, creator_fraction=None, creator_selling=None,
        )
        self.assertEqual([], contract_safety_failures(value))
        # Full strategy continues to require distribution/provider evidence.
        self.assertFalse(score_candidate(value, self.ledger, self.policy)["qualified"])

    def test_solana_rpc_fallback_parses_classic_immutable_mint(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "result": {"value": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"type": "mint", "info": {
                    "mintAuthority": None, "freezeAuthority": None,
                }}},
            }}
        }).encode()
        with patch("app.solana_early.urllib.request.urlopen", return_value=response):
            result = solana_rpc_mint_safety("mint1")
        self.assertEqual("ONCHAIN_VERIFIED", result["safety_evidence_status"])
        self.assertFalse(result["mint_authority_active"])
        self.assertFalse(result["freeze_authority_active"])
        self.assertFalse(result["transfer_hook_active"])

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
        self.assertIn('SOLANA_EARLY_MAX_CANDIDATES", "40"', source)
        self.assertIn('SOLANA_MICROCAP_WATCH_REFRESH_LIMIT", "20"', source)
        self.assertIn('scan_status="IN_PROGRESS"', source)
        self.assertIn('last_completed_scan=completed_snapshot', source)
        self.assertIn('timeout=5.0', source)

    def test_pumpfun_ev_is_separate_and_paper_only(self):
        result = score_pumpfun_ev_candidate(candidate(market_cap_usd=5000), self.ledger,
                                            self.policy, PumpfunEvPolicy())
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
        self.assertEqual("MICROCAP_LAUNCH_V2", result["strategy_version"])
        self.assertIn("$125,000 24h volume", result["entry_reason"])

    def test_microcap_launch_accepts_volume_upward_above_one_million_cap(self):
        result = score_microcap_launch_candidate(candidate(volume_24h_usd=250000), self.ledger,
                                                 MicrocapLaunchPolicy())
        self.assertTrue(result["paper_qualified"])

    def test_sub_million_microcap_shadow_tracks_executable_candidate_only(self):
        result = score_microcap_sub_million_shadow(
            candidate(market_cap_usd=450_000, volume_24h_usd=300_000,
                      liquidity_usd=90_000, sell_simulation_ok=True,
                      sell_price_impact_bps=65),
            MicrocapLaunchPolicy(),
        )
        self.assertTrue(result["shadow_qualified"])
        self.assertFalse(result["paper_qualified"])
        self.assertFalse(result["live_eligible"])
        self.assertEqual("SHADOW_ONLY", result["mode"])
        self.assertEqual("MICROCAP_SUB_1M_SHADOW_V1", result["strategy_version"])

    def test_sub_million_microcap_shadow_keeps_sellability_and_safety_hard(self):
        result = score_microcap_sub_million_shadow(
            candidate(market_cap_usd=450_000, volume_24h_usd=300_000,
                      liquidity_usd=90_000, sell_simulation_ok=False,
                      sell_price_impact_bps=9999),
            MicrocapLaunchPolicy(),
        )
        self.assertFalse(result["shadow_qualified"])
        self.assertIn("sell simulation failed", result["paper_failures"])
        self.assertIn("shadow full-size sell route unavailable", result["paper_failures"])

    def test_sub_million_shadow_does_not_include_live_eligible_market_caps(self):
        result = score_microcap_sub_million_shadow(candidate(market_cap_usd=1_000_000),
                                                   MicrocapLaunchPolicy())
        self.assertFalse(result["shadow_qualified"])
        self.assertIn("shadow market cap outside $100k-$1m research range", result["paper_failures"])

    def test_microcap_and_runner_enforce_one_million_market_cap_minimum(self):
        too_small = candidate(market_cap_usd=999_999)
        micro = score_microcap_launch_candidate(too_small, self.ledger, MicrocapLaunchPolicy())
        self.assertFalse(micro["paper_qualified"])
        self.assertIn("microcap market cap below $1.0m minimum", micro["paper_failures"])

        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-cap", price_usd=.001, volume_24h_usd=25000,
                          observed_at="2026-08-29T12:00:00+00:00")
        current = candidate(pool="pool-cap", price_usd=.0013, volume_24h_usd=150000,
                            market_cap_usd=999_999, price_change_5m_pct=60,
                            price_change_15m_pct=75, transaction_buy_pressure=.65,
                            observed_at="2026-08-29T12:06:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        runner = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(runner["live_probe_qualified"])
        self.assertIn("runner market cap below $1.0m minimum", runner["live_probe_failures"])

    def test_microcap_launch_rejects_weak_run_and_low_volume(self):
        result = score_microcap_launch_candidate(
            candidate(volume_24h_usd=99999, price_change_5m_pct=2,
                      transaction_buy_pressure=.10), self.ledger, MicrocapLaunchPolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("microcap 24h volume below $100k execution minimum", result["paper_failures"])
        self.assertIn("microcap five-minute momentum outside serious-run range", result["paper_failures"])
        self.assertIn("microcap net buy pressure below minimum", result["paper_failures"])

    def test_microcap_launch_retains_promising_pool_before_execution_volume(self):
        value = candidate(volume_24h_usd=25000, pool="pool1", price_usd=.001)
        result = score_microcap_launch_candidate(value, self.ledger, MicrocapLaunchPolicy())
        self.assertTrue(result["watch_eligible"])
        self.assertFalse(result["paper_qualified"])
        self.assertTrue(self.ledger.upsert_watch_candidate(value, result["strategy"]))
        self.assertEqual(["pool1"], self.ledger.watched_pools(result["strategy"], 999999))

    def test_microcap_watchlist_records_price_checkpoints(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool1", price_usd=.001,
                          observed_at="2026-08-29T12:00:00+00:00")
        later = candidate(pool="pool1", price_usd=.0012,
                          observed_at="2026-08-29T12:16:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(later, strategy)
        row = self.ledger.watchlist_snapshot(strategy)[0]
        self.assertAlmostEqual(.2, row["checkpoints"]["5"]["mark_return"])
        self.assertAlmostEqual(.2, row["checkpoints"]["15"]["mark_return"])
        self.assertIn("sell_route_ok", row["checkpoints"]["15"])
        self.assertNotIn("30", row["checkpoints"])

    def test_microcap_watchlist_retains_first_and_latest_evidence(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool1", price_usd=.001, volume_24h_usd=25000,
                          observed_at="2026-08-29T12:00:00+00:00")
        later = candidate(pool="pool1", price_usd=.0013, volume_24h_usd=140000,
                          observed_at="2026-08-29T12:06:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(later, strategy)
        row = self.ledger.watchlist_snapshot(strategy)[0]
        self.assertEqual(25000, row["first_candidate"]["volume_24h_usd"])
        self.assertEqual(140000, row["latest_candidate"]["volume_24h_usd"])
        self.assertAlmostEqual(.3, row["return_since_seen"])

    def test_runner_capture_qualifies_persistent_explosive_move_for_paper_only(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool1", price_usd=.001, volume_24h_usd=25000,
                          observed_at="2026-08-29T12:00:00+00:00")
        current = candidate(
            pool="pool1", price_usd=.0013, volume_24h_usd=150000,
            liquidity_usd=250000,
            token_age_minutes=18, price_change_5m_pct=60, price_change_15m_pct=75,
            transaction_buy_pressure=.65, observed_at="2026-08-29T12:06:00+00:00",
            top10_holder_fraction=None, creator_fraction=None,
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertTrue(result["paper_qualified"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["live_eligible"])
        self.assertEqual("RUNNER_CAPTURE_V1", result["strategy_version"])
        self.assertEqual("UNAVAILABLE_PAPER_ONLY", result["distribution_evidence_status"])
        self.assertIn("30.0% since first observation", result["entry_reason"])

    def test_runner_rejects_dancedog_like_late_weak_pressure_and_shallow_depth(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="dancedog", price_usd=.001,
                          observed_at="2026-09-01T19:44:00+00:00")
        current = candidate(
            pool="dancedog", price_usd=.00234, market_cap_usd=7_000_000,
            volume_24h_usd=6_325_336, liquidity_usd=82_822,
            transaction_buy_pressure=.1938, price_change_5m_pct=10.565,
            price_change_15m_pct=46.987,
            observed_at="2026-09-01T20:31:00+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("runner late acceleration has weak buy pressure", result["paper_failures"])
        self.assertIn("runner $1m+ liquidity below $200k intelligence floor",
                      result["paper_failures"])

    def test_runner_rejects_implausible_momentum_outlier(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="stonk", price_usd=.001,
                          observed_at="2026-09-01T18:10:00+00:00")
        current = candidate(
            pool="stonk", price_usd=.00125, market_cap_usd=7_000_000,
            volume_24h_usd=200_000, liquidity_usd=250_000,
            transaction_buy_pressure=.86, price_change_5m_pct=3,
            price_change_15m_pct=1293.8,
            observed_at="2026-09-01T18:22:00+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("runner fifteen-minute momentum outlier requires validation",
                      result["paper_failures"])

    def test_runner_capture_rejects_late_retracement_or_missing_sell_route(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool1", price_usd=.001,
                          observed_at="2026-08-29T12:00:00+00:00")
        peak = candidate(pool="pool1", price_usd=.002,
                         observed_at="2026-08-29T12:05:00+00:00")
        current = candidate(
            pool="pool1", price_usd=.0015, volume_24h_usd=150000,
            token_age_minutes=18, price_change_5m_pct=25, price_change_15m_pct=45,
            transaction_buy_pressure=.65, sell_simulation_ok=False,
            observed_at="2026-08-29T12:06:00+00:00",
        )
        for item in (first, peak, current):
            self.ledger.upsert_watch_candidate(item, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("sell simulation failed", result["paper_failures"])
        self.assertIn("runner has already retraced more than 10% from its observed high",
                      result["paper_failures"])

    def test_runner_capture_rejects_extreme_late_chase(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-chase", price_usd=.00001,
                          observed_at="2026-08-29T12:00:00+00:00")
        current = candidate(pool="pool-chase", price_usd=.001,
                            market_cap_usd=2_000_000, volume_24h_usd=2_000_000,
                            liquidity_usd=250_000, price_change_5m_pct=20,
                            price_change_15m_pct=50, transaction_buy_pressure=.5,
                            observed_at="2026-08-29T12:10:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(result["paper_qualified"])
        self.assertIn("runner retained gain above 800% late-chase ceiling",
                      result["paper_failures"])

    def test_runner_live_probe_requires_sell_route_and_acceptable_impact(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-probe", price_usd=.001, volume_24h_usd=25000,
                          observed_at="2026-08-29T12:00:00+00:00")
        current = candidate(
            pool="pool-probe", price_usd=.0014, volume_24h_usd=150000,
            token_age_minutes=18, price_change_5m_pct=60, price_change_15m_pct=75,
            transaction_buy_pressure=.65, sell_simulation_ok=False,
            sell_price_impact_bps=9999,
            observed_at="2026-08-29T12:06:00+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)
        result = score_runner_capture_candidate(current, self.ledger, RunnerCapturePolicy())
        self.assertFalse(result["live_probe_qualified"])
        self.assertFalse(result["paper_qualified"])
        self.assertIn("sell simulation failed", result["paper_failures"])
        self.assertIn("sell simulation failed", result["live_probe_failures"])
        self.assertIn("runner executable sell impact above maximum",
                      result["live_probe_failures"])
        self.assertEqual("PAPER_ONLY", result["mode"])

        unsafe = score_runner_capture_candidate(
            dict(current, mint_authority_active=True), self.ledger, RunnerCapturePolicy())
        self.assertFalse(unsafe["live_probe_qualified"])
        self.assertIn("mint_authority_active", unsafe["live_probe_failures"])

    def test_runner_allows_only_low_risk_momentum_shortfall(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-anthropic", price_usd=.001,
                          observed_at="2026-08-30T14:20:00+00:00")
        current = candidate(
            pool="pool-anthropic", price_usd=.00135, market_cap_usd=1_520_497,
            volume_24h_usd=5_646_537, liquidity_usd=354_217,
            token_age_minutes=20, price_change_5m_pct=8.599,
            price_change_15m_pct=20.818, transaction_buy_pressure=.55,
            sell_price_impact_bps=58.45,
            observed_at="2026-08-30T14:52:00+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)

        result = score_runner_capture_candidate(current, self.ledger,
                                                RunnerCapturePolicy())

        self.assertTrue(result["live_probe_qualified"])
        self.assertTrue(result["paper_qualified"])
        self.assertTrue(result["conditional_entry_applied"])
        self.assertEqual("CONDITIONAL_LOW_RISK", result["risk_tier"])
        self.assertEqual([], result["hard_risk_failures"])
        self.assertIn("runner five-minute momentum below preferred explosive range",
                      result["conditional_risk_warnings"])

    def test_runner_does_not_override_hard_or_very_weak_momentum_failures(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-risk", price_usd=.001,
                          observed_at="2026-08-30T14:20:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        weak = candidate(
            pool="pool-risk", price_usd=.00135, market_cap_usd=1_520_497,
            volume_24h_usd=5_646_537, liquidity_usd=354_217,
            token_age_minutes=20, price_change_5m_pct=3,
            price_change_15m_pct=20, sell_price_impact_bps=58,
            observed_at="2026-08-30T14:52:00+00:00",
        )
        weak_result = score_runner_capture_candidate(weak, self.ledger,
                                                     RunnerCapturePolicy())
        self.assertFalse(weak_result["live_probe_qualified"])
        self.assertIn("runner five-minute momentum below conditional minimum",
                      weak_result["hard_risk_failures"])

        unsellable = score_runner_capture_candidate(
            dict(weak, price_change_5m_pct=8, sell_simulation_ok=False,
                 sell_price_impact_bps=9999),
            self.ledger, RunnerCapturePolicy())
        self.assertFalse(unsellable["live_probe_qualified"])
        self.assertIn("sell simulation failed", unsellable["hard_risk_failures"])

    def test_runner_allows_hshare_profile_as_exceptional_probe(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-hshare", price_usd=.001,
                          observed_at="2026-08-30T15:37:30+00:00")
        current = candidate(
            pool="pool-hshare", price_usd=.002660481, market_cap_usd=290_562.40,
            volume_24h_usd=937_513.37, liquidity_usd=37_687.58,
            token_age_minutes=31.5, price_change_5m_pct=5.413,
            price_change_15m_pct=42.241, transaction_buy_pressure=.0826,
            sell_price_impact_bps=81.15,
            observed_at="2026-08-30T16:07:06+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)

        result = score_runner_capture_candidate(current, self.ledger,
                                                RunnerCapturePolicy())

        self.assertTrue(result["live_probe_qualified"])
        self.assertTrue(result["paper_qualified"])
        self.assertTrue(result["exceptional_entry_applied"])
        self.assertEqual("EXCEPTIONAL_RUNNER", result["risk_tier"])
        self.assertEqual([], result["hard_risk_failures"])

    def test_exceptional_runner_never_waives_sellability_or_safety(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-exception", price_usd=.001,
                          observed_at="2026-08-30T15:37:30+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        base = candidate(
            pool="pool-exception", price_usd=.002, market_cap_usd=300_000,
            volume_24h_usd=1_000_000, liquidity_usd=40_000,
            token_age_minutes=30, price_change_5m_pct=8,
            price_change_15m_pct=40, transaction_buy_pressure=.08,
            sell_price_impact_bps=80,
            observed_at="2026-08-30T16:07:06+00:00",
        )
        unsafe = score_runner_capture_candidate(
            dict(base, mint_authority_active=True), self.ledger,
            RunnerCapturePolicy())
        self.assertFalse(unsafe["live_probe_qualified"])
        self.assertFalse(unsafe["exceptional_entry_applied"])

        unsellable = score_runner_capture_candidate(
            dict(base, sell_simulation_ok=False, sell_price_impact_bps=9999),
            self.ledger, RunnerCapturePolicy())
        self.assertFalse(unsellable["live_probe_qualified"])
        self.assertFalse(unsellable["exceptional_entry_applied"])

    def test_runner_allows_lower_momentum_for_large_liquid_coin(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-liquid", price_usd=.001,
                          observed_at="2026-08-30T17:30:00+00:00")
        current = candidate(
            pool="pool-liquid", price_usd=.00106, market_cap_usd=12_287_340,
            volume_24h_usd=141_807, liquidity_usd=301_273,
            token_age_minutes=25, price_change_5m_pct=2.463,
            price_change_15m_pct=5.556, transaction_buy_pressure=.375,
            sell_price_impact_bps=73.26,
            observed_at="2026-08-30T17:36:00+00:00",
        )
        self.ledger.upsert_watch_candidate(first, strategy)
        self.ledger.upsert_watch_candidate(current, strategy)

        result = score_runner_capture_candidate(current, self.ledger,
                                                RunnerCapturePolicy())

        self.assertTrue(result["live_probe_qualified"])
        self.assertTrue(result["liquid_momentum_entry_applied"])
        self.assertEqual("LIQUID_MOMENTUM", result["risk_tier"])
        self.assertNotIn("runner has not gained 20% since first observation",
                         result["live_probe_failures"])

    def test_liquid_momentum_keeps_exitability_and_trend_hard(self):
        strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        first = candidate(pool="pool-liquid-risk", price_usd=.001,
                          observed_at="2026-08-30T17:30:00+00:00")
        self.ledger.upsert_watch_candidate(first, strategy)
        base = candidate(
            pool="pool-liquid-risk", price_usd=.00106,
            market_cap_usd=8_000_000, volume_24h_usd=200_000,
            liquidity_usd=250_000, token_age_minutes=25,
            price_change_5m_pct=2.5, price_change_15m_pct=5,
            transaction_buy_pressure=.4, sell_price_impact_bps=75,
            observed_at="2026-08-30T17:36:00+00:00",
        )
        unsellable = score_runner_capture_candidate(
            dict(base, sell_simulation_ok=False, sell_price_impact_bps=9999),
            self.ledger, RunnerCapturePolicy())
        self.assertFalse(unsellable["live_probe_qualified"])
        self.assertFalse(unsellable["liquid_momentum_entry_applied"])

        weak = score_runner_capture_candidate(
            dict(base, price_change_15m_pct=1), self.ledger,
            RunnerCapturePolicy())
        self.assertFalse(weak["live_probe_qualified"])
        self.assertFalse(weak["liquid_momentum_entry_applied"])

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
        compact = "".join(source.split())
        self.assertIn('candidates.filter((x)=>x.qualified===true)', compact)
        self.assertIn('candidates.filter((x)=>x.paper_qualified===true)', compact)

    def test_microcap_executor_retains_fast_exit_and_named_reporting(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        self.assertIn("function probeRetryDue", source)
        self.assertIn("15 * 60000", source)
        compact = "".join(source.split())
        for expected in ("MICROCAP_DOWNTREND", "MICROCAP_PROFIT_PROTECTION",
                         "MAX_HOLD_20M", 'isMicrocap?0.08', 'isMicrocap?0.2',
                         'strategyName(f.strategy)'):
            self.assertTrue(expected in source or "".join(expected.split()) in compact)

    def test_runner_capture_has_isolated_capped_live_probe_and_tiered_paper_exits(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        compact = "".join(source.split())
        for expected in ("RUNNER_CAPTURE_V1", "RUNNER_TIERED_PROFIT", "RUNNER_DOWNTREND",
                         "MAX_HOLD_30M_NO_LIQUID_CONTINUATION",
                         "MAX_HOLD_60M_LIQUID_CONTINUATION", "Runner Capture Experiment",
                         "CYAN • Runner Capture V1",
                         "target=isRunner?5.0", "stop=isRunner?0.1",
                         "SOLANA_RUNNER_LIVE_PROBE_ENABLED",
                         "I_ACCEPT_THE_0_50_USD_RUNNER_LIQUIDITY_PROBE",
                         "probeEntry:Math.min(0.5", "probeDailyCap:Math.min(5",
                         "IMMEDIATE_EXITABILITY_TEST", "PROBE_PARTIAL_SELL",
                         "PROBE_FINAL_SELL", "runnerProbeBuy(candidate)",
                         'x.live_probe_qualified===true',
                         "REAL-MONEY RUNNER LIQUIDITY PROBE",
                         "ROUND_TRIP_PREFLIGHT_RECOVERY_BELOW_MINIMUM",
                         "REALIZED_LOSS_PLUS_NEXT_ENTRY_RESERVATION",
                         "runnerLiveProbePerformance",
                         "containsRealMoneyProbe:true"):
            self.assertTrue(expected in source or "".join(expected.split()) in compact)
        self.assertIn("probeConfirmationScans", source)
        self.assertIn("candidateHandoffs", source)
        self.assertIn("PAPER_ENTRY_QUOTE_FAILED", source)
        for field in ("highUsdAt", "lowUsdAt", "maximumFavorableAt",
                      "maximumAdversePnlUsd", "maximumAdverseAt", "lastExecutableQuoteAt"):
            self.assertIn(field, source)

    def test_runner_paper_uses_live_equivalent_round_trip_preflight(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        compact = "".join(source.split())
        for expected in (
            "paperRoundTripPreflight",
            "PAPER_ENTRY_PREFLIGHT_REJECTED",
            "ROUND_TRIP_PREFLIGHT_RECOVERY_BELOW_MINIMUM",
            'strategy==="SOLANA_MICROCAP_RUNNER_CAPTURE"?Math.max(2,cfg.paperConfirmationScans)',
            "roundTripPreflight",
        ):
            self.assertIn("".join(expected.split()), compact)

    def test_sub_million_shadow_has_dedicated_positions_and_performance(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        compact = "".join(source.split())
        for expected in (
            "MICROCAP_SUB_1M_SHADOW_V1",
            "SHADOW_ENTRY_CAPTURED",
            "SOLANA_MICROCAP_SUB_1M_EXECUTABLE_SHADOW",
        ):
            self.assertIn("".join(expected.split()), compact)

    def test_reports_fee_completeness_and_wallet_credit_status(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        for expected in (
            "feeEligibleTransactionCount", "missingOnChainFeeCount",
            "unvaluedFeeCount", "walletIntelligenceStatus",
            "NO_REVIEWED_WALLETS_CONFIGURED_NO_PREDICTIVE_CREDIT",
        ):
            self.assertIn(expected, source)

    def test_runner_live_probe_reprices_actual_fill_and_exits_fast(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        compact = "".join(source.split())
        for expected in (
            "preflightExpectedFullRecoveryUsd",
            "probePostBuyRecoveryFloorPct:0.97",
            "POST_BUY_RECOVERY_FLOOR",
            "POST_BUY_ROUTE_DIVERGENCE",
            "PROBE_TAKE_PROFIT_5PCT",
            "PROBE_BREAKEVEN_PROTECTION",
            "PROBE_MAX_HOLD_5M",
            "asyncfunctionliquidateProbe",
            "constfractions=[1,0.5,0.25,0.1]",
            'Math.max(60,num(env("SOLANA_EXECUTOR_INTERVAL_SECONDS"),60))',
            "asyncfunctionscheduledProbeSupervision",
            "15*1000",
        ):
            self.assertIn(expected, compact)

    def test_microcap_action_reports_are_named_colored_and_retried(self):
        source = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
        compact = "".join(source.split())
        for expected in ("Microcap Launch V2", "ORANGE • Microcap Launch V2",
                         "pendingTradeEvent:true", "state.email.pendingTradeEvent",
                         "pendingTradeEvent:false", "microcapWatchlist",
                         "microcapWatchlistSummary", "watchedWallets", "walletEvidence",
                         "reportableProbeAction", "NO_NEW_TRADE_ACTION"):
            self.assertTrue(expected in source or "".join(expected.split()) in compact)

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
        self.assertIn("evidence_coverage", report["SOLANA_PUMPFUN_EV_EXPERIMENT"])
        self.assertIn("near_misses", report["SOLANA_PUMPFUN_EV_EXPERIMENT"])

    def test_microcap_missing_creator_is_paper_warning_not_silent_approval(self):
        value = candidate(creator_fraction=None)
        result = score_microcap_launch_candidate(value, self.ledger, MicrocapLaunchPolicy())
        self.assertIn("microcap creator concentration unavailable", result["evidence_warnings"])
        self.assertNotIn("microcap creator concentration unavailable", result["paper_failures"])
        self.assertFalse(result["live_eligible"])


if __name__ == "__main__":
    unittest.main()
