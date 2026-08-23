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
        self.store.add_submitted_position(t, "o1")
        self.assertEqual(self.store.open_position()["status"], "SUBMITTED")
        self.store.update_position("t1", status="CLOSED", realized_pnl=1.2)
        self.assertIsNone(self.store.open_position())
