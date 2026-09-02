import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.multi_asset import AssetPolicy, ForexEngine, MultiAssetEngine, MultiAssetRejected, PaperLedger
from app.asset_worker import supervise


class MultiAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = PaperLedger(self.directory.name + "/ledger.jsonl")
        self.policy = AssetPolicy(minimum_score=75, max_risk_usd=2.5)

    @staticmethod
    def base(asset_class, symbol):
        return {
            "asset_class": asset_class, "symbol": symbol, "price": 1.1,
            "spread_bps": 2, "tradable": True, "market_veto": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_urls": ["https://official.example/market"],
            "stop_distance": .01, "maximum_loss_usd": 2.5,
            "thesis": "test thesis", "invalidation": "test invalidation",
        }

    def test_forex_engine_creates_paper_fill_only(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": .2,
                    "change_24h_pct": .5, "trend_strength": 1,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with patch.dict(os.environ, {"FOREX_ENGINE_ENABLED": "true"}):
            result = MultiAssetEngine(self.ledger, self.policy).process(snapshot)
        self.assertEqual("PAPER_FILL", result["type"])
        self.assertEqual("PAPER_ONLY", result["mode"])
        self.assertLessEqual(result["maximum_loss_usd"], 2.5)

    def test_forex_aligned_liquid_signal_reaches_calibrated_threshold(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": .02,
                    "change_24h_pct": .20, "trend_strength": .08,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        engine = ForexEngine(AssetPolicy(minimum_score=75, max_risk_usd=.5))
        proposal = engine.evaluate(snapshot)
        self.assertEqual("FOREX_TREND_CONTINUATION", proposal.strategy)
        self.assertEqual("BUY", proposal.side)
        self.assertGreaterEqual(proposal.score, 75)

    def test_forex_controlled_pullback_gets_partial_credit_but_needs_strong_trend(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": -.05,
                    "change_24h_pct": .40, "trend_strength": .30,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        engine = ForexEngine(AssetPolicy(minimum_score=75, max_risk_usd=.5))
        proposal = engine.evaluate(snapshot)
        self.assertEqual("FOREX_TREND_PULLBACK", proposal.strategy)
        self.assertEqual("BUY", proposal.side)
        self.assertEqual(("CONTROLLED_PULLBACK", 8.0, "BUY"), engine.alignment(snapshot))

    def test_forex_rejects_materially_contradictory_timeframes(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": -.20,
                    "change_24h_pct": .40, "trend_strength": .30,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        engine = ForexEngine(AssetPolicy(minimum_score=75, max_risk_usd=.5))
        with self.assertRaisesRegex(MultiAssetRejected, "contradict"):
            engine.evaluate(snapshot)

    def test_forex_rejects_aggregate_side_that_conflicts_with_live_trend(self):
        snapshot = {**self.base("FOREX", "AUD_USD"), "change_1h_pct": -.01,
                    "change_24h_pct": -.20, "trend_strength": .30,
                    "horizon_direction": -1, "horizon_agreement": .85,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with self.assertRaisesRegex(MultiAssetRejected, "trend strength"):
            ForexEngine(AssetPolicy(minimum_score=75, max_risk_usd=.5)).evaluate(snapshot)

    def test_forex_rejects_material_one_hour_reversal_against_signal(self):
        snapshot = {**self.base("FOREX", "AUD_USD"), "change_1h_pct": .08,
                    "change_24h_pct": -.20, "trend_strength": -.30,
                    "horizon_direction": -1, "horizon_agreement": .85,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with self.assertRaisesRegex(MultiAssetRejected, "1h reversal"):
            ForexEngine(AssetPolicy(minimum_score=75, max_risk_usd=.5)).evaluate(snapshot)

    def test_option_engine_rejects_undefined_risk_structure(self):
        snapshot = {**self.base("OPTION", "SPY"), "structure": "SHORT_CALL",
                    "days_to_expiry": 30, "open_interest": 1000,
                    "all_legs_verified": True, "liquidity_score": 1,
                    "edge_score": 1, "iv_context_verified": True,
                    "underlying_trend_aligned": True}
        with patch.dict(os.environ, {"OPTION_ENGINE_ENABLED": "true"}):
            with self.assertRaisesRegex(MultiAssetRejected, "defined-risk"):
                MultiAssetEngine(self.ledger, self.policy).process(snapshot)

    def test_option_engine_uses_whole_contract_defined_loss(self):
        policy = AssetPolicy(minimum_score=75, max_risk_usd=25.0)
        snapshot = {**self.base("OPTION", "SPY-SPREAD"), "price": .20,
                    "structure": "CALL_DEBIT_SPREAD", "days_to_expiry": 14,
                    "open_interest": 1000, "maximum_loss_usd": 20.0,
                    "all_legs_verified": True, "liquidity_score": 1,
                    "edge_score": 1, "iv_context_verified": True,
                    "underlying_trend_aligned": True}
        with patch.dict(os.environ, {"OPTION_ENGINE_ENABLED": "true"}):
            result = MultiAssetEngine(self.ledger, policy).process(snapshot)
        self.assertEqual(1.0, result["quantity"])
        self.assertEqual(20.0, result["maximum_loss_usd"])

    def test_equity_engine_respects_position_limit(self):
        snapshot = {**self.base("EQUITY", "SPY"), "change_1h_pct": .3,
                    "change_24h_pct": 1, "relative_volume": 2,
                    "above_vwap": True, "market_trend_positive": True}
        with patch.dict(os.environ, {"EQUITY_ENGINE_ENABLED": "true"}):
            engine = MultiAssetEngine(self.ledger, AssetPolicy(
                minimum_score=75, max_risk_usd=2.5, max_open_positions_per_sleeve=1))
            engine.process(snapshot)
            with self.assertRaisesRegex(MultiAssetRejected, "already has an open paper position"):
                engine.process(snapshot)

    def test_three_distinct_positions_can_collect_evidence_concurrently(self):
        policy = AssetPolicy(minimum_score=75, max_risk_usd=2.5,
                             max_open_positions_per_sleeve=3)
        with patch.dict(os.environ, {"EQUITY_ENGINE_ENABLED": "true"}):
            engine = MultiAssetEngine(self.ledger, policy)
            for symbol in ("SPY", "QQQ", "IWM"):
                engine.process({**self.base("EQUITY", symbol), "change_1h_pct": .3,
                                "change_24h_pct": 1, "relative_volume": 2,
                                "above_vwap": True, "market_trend_positive": True})
            with self.assertRaisesRegex(MultiAssetRejected, "capacity 3/3.*SPY, QQQ, IWM"):
                engine.process({**self.base("EQUITY", "DIA"), "change_1h_pct": .3,
                                "change_24h_pct": 1, "relative_volume": 2,
                                "above_vwap": True, "market_trend_positive": True})

    def test_full_sleeve_does_not_hide_actual_strategy_rejection(self):
        policy = AssetPolicy(minimum_score=75, max_risk_usd=2.5,
                             max_open_positions_per_sleeve=1)
        good = {**self.base("EQUITY", "SPY"), "change_1h_pct": .3,
                "change_24h_pct": 1, "relative_volume": 2,
                "above_vwap": True, "market_trend_positive": True}
        bad = {**self.base("EQUITY", "QQQ"), "change_1h_pct": -.3,
               "change_24h_pct": -1, "relative_volume": .2,
               "above_vwap": False, "market_trend_positive": False}
        with patch.dict(os.environ, {"EQUITY_ENGINE_ENABLED": "true"}):
            engine = MultiAssetEngine(self.ledger, policy)
            engine.process(good)
            with self.assertRaisesRegex(MultiAssetRejected, "positive 1h/24h momentum required"):
                engine.process(bad)

    def test_engine_disabled_by_default(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": .2,
                    "change_24h_pct": .5, "trend_strength": 1,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MultiAssetRejected, "disabled"):
                MultiAssetEngine(self.ledger, self.policy).process(snapshot)

    def test_paper_position_can_be_closed_append_only(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": .2,
                    "change_24h_pct": .5, "trend_strength": 1,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with patch.dict(os.environ, {"FOREX_ENGINE_ENABLED": "true"}):
            fill = MultiAssetEngine(self.ledger, self.policy).process(snapshot)
        close = self.ledger.close(fill["proposal_id"], 1.12, "TARGET")
        self.assertEqual("PAPER_CLOSE", close["type"])
        self.assertEqual([], self.ledger.positions())
        self.assertEqual(3, len(self.ledger.records()))

    def test_worker_supervises_the_same_ledger_that_opens_positions(self):
        snapshot = {**self.base("FOREX", "EUR_USD"), "change_1h_pct": .2,
                    "change_24h_pct": .5, "trend_strength": 1,
                    "liquidity_score": 1, "session_liquid": True,
                    "economic_event_within_minutes": 120}
        with patch.dict(os.environ, {"FOREX_ENGINE_ENABLED": "true"}):
            MultiAssetEngine(self.ledger, self.policy).process(snapshot)
        closes = supervise(self.ledger, [{"symbol":"EUR_USD", "price":1.3}])
        self.assertEqual("TARGET", closes[0]["reason"])
        self.assertEqual([], self.ledger.positions())
        report = self.ledger.report()
        self.assertEqual(1, report["closed"])
        self.assertGreater(report["realized_pnl_usd"], 0)

    def test_worker_expires_unmarked_position_without_blocking_forever(self):
        fill = self.ledger.append({"type": "PAPER_FILL", "mode": "PAPER_ONLY",
            "proposal_id": "stale-1", "asset_class": "FOREX", "strategy": "TEST",
            "symbol": "EUR_USD", "side": "BUY", "fill_price": 1.10, "quantity": 10,
            "stop_price": 1.09, "target_price": 1.12, "maximum_loss_usd": .10})
        records = self.ledger.records()
        records[-1]["recorded_at"] = (datetime.now(timezone.utc) - timedelta(minutes=300)).isoformat()
        self.ledger.path.write_text("\n".join(__import__("json").dumps(item) for item in records) + "\n")
        closes = supervise(self.ledger, [], max_hold_minutes=240)
        self.assertEqual("MAX_HOLD_STALE_MARK", closes[0]["reason"])
        self.assertEqual("ENTRY_FALLBACK_NO_MARK", closes[0]["price_source"])
        self.assertEqual([], self.ledger.positions())

    def test_report_identifies_open_position_and_excursions(self):
        position = self.ledger.append({"type": "PAPER_FILL", "mode": "PAPER_ONLY",
            "proposal_id": "open-1", "asset_class": "FOREX", "strategy": "TEST",
            "symbol": "EUR_USD", "side": "BUY", "fill_price": 1.10, "quantity": 10,
            "stop_price": 1.09, "target_price": 1.12, "maximum_loss_usd": .10})
        self.ledger.mark(position, 1.11)
        self.ledger.mark(position, 1.095)
        diagnostic = self.ledger.report()["open_positions"][0]
        self.assertEqual("EUR_USD", diagnostic["symbol"])
        self.assertAlmostEqual(.10, diagnostic["mfe_usd"])
        self.assertAlmostEqual(-.05, diagnostic["mae_usd"])
        self.assertAlmostEqual(-.05, diagnostic["current_unrealized_pnl_usd"])
        self.assertIsNotNone(diagnostic["age_minutes"])


if __name__ == "__main__":
    unittest.main()
