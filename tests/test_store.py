import tempfile
import unittest

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
