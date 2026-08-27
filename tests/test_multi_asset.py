import os
import tempfile
import unittest
from datetime import datetime, timezone
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
                    "change_24h_pct": .20, "trend_strength": .01,
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
            engine = MultiAssetEngine(self.ledger, self.policy)
            engine.process(snapshot)
            with self.assertRaisesRegex(MultiAssetRejected, "position limit"):
                engine.process(snapshot)

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


if __name__ == "__main__":
    unittest.main()
