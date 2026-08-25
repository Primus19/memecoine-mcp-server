import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, Fernet.generate_key().decode())

    def tearDown(self): self.temp.cleanup()

    def test_baseline_is_immutable_and_profit_compounds(self):
        self.assertEqual(self.store.initialize_baseline(25), 25)
        self.assertEqual(self.store.initialize_baseline(29), 25)
        self.store.add_realized_pnl(3.5)
        self.assertAlmostEqual(self.store.permitted_capital(), 28.5)

    def test_initial_external_balance_is_rejected(self):
        with self.assertRaises(RuntimeError): self.store.initialize_baseline(40)

    def test_position_lifecycle(self):
        t = {"ticket_id": "t1", "product_id": "TURBO-USDC", "notional_usdc": 23.75, "limit_price": .1}
        self.store.add_position(t, "o1")
        self.assertEqual(self.store.open_position()["status"], "SUBMITTED")
        self.store.update_position("t1", status="CLOSED", pnl=1.2)
        self.assertIsNone(self.store.open_position())

    def test_external_deposit_is_separate_from_profit(self):
        self.store.initialize_baseline(25)
        flow=self.store.sync_external_flow(40)
        self.assertEqual(flow["kind"],"DEPOSIT")
        self.assertAlmostEqual(self.store.permitted_capital(),25)
        self.assertAlmostEqual(float(self.store.setting("realized_pnl_usdc")),0)

    def test_two_losses_pause(self):
        self.store.initialize_baseline(25)
        for i in range(2):
            payload={"ticket_id":f"t{i}","recommendation_hash":f"h{i}","model_version":"3.1","created_at":"x","expires_at":"x","product_id":"TURBO-USDC","score":85}
            self.store.issue_recommendation(payload);self.store.add_position({"ticket_id":f"t{i}","product_id":"TURBO-USDC","notional_usdc":5,"limit_price":1},f"o{i}");self.store.record_closed_trade(f"t{i}",-1,-20)
        controls=self.store.update_equity_controls(22)
        self.assertIn("two consecutive losses",controls["circuit_breakers"])
        self.assertTrue(self.store.paused())

    def test_profitable_close_does_not_use_unsettled_cash_for_drawdown(self):
        self.store.initialize_baseline(25)
        self.store.set_setting("peak_equity_usdc",26.068091076)
        payload={"ticket_id":"t1","recommendation_hash":"h1","model_version":"3.1","created_at":"x","expires_at":"x","product_id":"FARTCOIN-USDC","score":85}
        self.store.issue_recommendation(payload)
        self.store.add_position({"ticket_id":"t1","product_id":"FARTCOIN-USDC","notional_usdc":23.75,"limit_price":.181},"o1")

        result=self.store.record_closed_trade("t1",.0444456428,.1853943158)

        self.assertAlmostEqual(result["controls"]["equity_usdc"],25.0444456428)
        self.assertAlmostEqual(result["controls"]["drawdown_pct"],3.9268139359)
        self.assertEqual(result["controls"]["equity_source"],"REALIZED_CAPITAL")
        self.assertEqual(result["controls"]["circuit_breakers"],[])
        self.assertFalse(self.store.paused())

    def test_open_trade_equity_uses_fill_pnl_not_account_settlement(self):
        self.store.initialize_baseline(25)
        self.assertAlmostEqual(self.store.reconciled_equity(.305993376),25.305993376)

    def test_real_closed_loss_still_triggers_drawdown_pause(self):
        self.store.initialize_baseline(25)
        payload={"ticket_id":"t1","recommendation_hash":"h1","model_version":"3.1","created_at":"x","expires_at":"x","product_id":"FARTCOIN-USDC","score":85}
        self.store.issue_recommendation(payload)
        self.store.add_position({"ticket_id":"t1","product_id":"FARTCOIN-USDC","notional_usdc":23.75,"limit_price":.181},"o1")

        result=self.store.record_closed_trade("t1",-4,-16.84)

        self.assertIn("daily drawdown reached 15%",result["controls"]["circuit_breakers"])
        self.assertTrue(self.store.paused())

    def test_external_flow_waits_for_close_settlement_grace(self):
        self.store.initialize_baseline(25)
        until=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        self.store.set_setting("settlement_grace_until",until)

        result=self.store.sync_external_flow(1.0264)

        self.assertEqual(result["status"],"SETTLEMENT_GRACE")
        self.assertEqual(float(self.store.setting("net_external_flows_usdc")),0)

    def test_rejection_reason_is_preserved_in_reports(self):
        payload={"ticket_id":"t-reject","recommendation_hash":"h-reject","model_version":"3.1","created_at":"x","expires_at":"x","product_id":"TURBO-USDC","score":85}
        self.store.issue_recommendation(payload)
        self.store.mark_recommendation("t-reject","SUBMISSION_REJECTED",rejection_reason="broker rejected attached order")
        row=self.store.recent_recommendations()[0]
        self.assertEqual("broker rejected attached order",row["rejection_reason"])

    def test_closed_trade_review_retains_mfe_and_mae(self):
        payload={"ticket_id":"t-excursion","recommendation_hash":"h-excursion","model_version":"3.1",
                 "created_at":"x","expires_at":"x","product_id":"TURBO-USDC","score":85}
        self.store.issue_recommendation(payload)
        self.store.add_position({"ticket_id":"t-excursion","product_id":"TURBO-USDC",
                                 "notional_usdc":20,"limit_price":1},"o-excursion")
        self.store.update_position_excursions("t-excursion",1.25)
        self.store.update_position_excursions("t-excursion",-.40)
        self.store.record_closed_trade("t-excursion",.75,3.75)
        review=self.store.model_review("trade_close","trade:t-excursion")
        self.assertAlmostEqual(1.25,review["average_max_favorable_excursion_usdc"])
        self.assertAlmostEqual(-.40,review["average_max_adverse_excursion_usdc"])
        self.assertAlmostEqual(.60,review["average_profit_capture"])
