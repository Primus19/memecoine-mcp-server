import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.forex_executor import Ledger, live_armed, practice_armed, safe_quantity


class Adapter:
    environment = "live"
    account = "001-001-1234567-001"

    def price(self, _symbol):
        return {
            "status": "tradeable",
            "time": datetime.now(timezone.utc).isoformat(),
            "quoteHomeConversionFactors": {"negativeUnits": "1.0"},
        }


class ForexExecutorTests(unittest.TestCase):
    def test_live_requires_all_four_independent_gates(self):
        values = {
            "FOREX_LIVE_ENABLED": "true",
            "FOREX_LIVE_ACK": "I_ACCEPT_REAL_MONEY_RISK",
            "FOREX_ALLOWED_ACCOUNT_ID": Adapter.account,
        }
        with patch.dict(os.environ, values, clear=False):
            self.assertTrue(live_armed(Adapter()))
            os.environ["FOREX_LIVE_ACK"] = "wrong"
            self.assertFalse(live_armed(Adapter()))

    def test_practice_execution_has_separate_acknowledgement(self):
        adapter = Adapter(); adapter.environment = "practice"
        with patch.dict(os.environ, {"FOREX_PRACTICE_EXECUTION_ENABLED":"true",
                                     "FOREX_PRACTICE_ACK":"I_ACCEPT_PRACTICE_ORDER_EXECUTION"}, clear=False):
            self.assertTrue(practice_armed(adapter))
            self.assertFalse(live_armed(adapter))

    def test_append_only_hash_chain_and_position_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":100,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":2.5}
            ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            ledger.event("PAPER_FILL", proposal)
            ledger.event("CHECK", {"ok": True})
            self.assertEqual(1, ledger.open_count())
            rows = ledger.db.execute("SELECT previous_hash,record_hash FROM events ORDER BY seq").fetchall()
            self.assertEqual("GENESIS", rows[0]["previous_hash"])
            self.assertEqual(rows[0]["record_hash"], rows[1]["previous_hash"])

    def test_home_currency_risk_sizing(self):
        proposal = {"symbol":"EUR_USD","reference_price":1.1,"stop_price":1.095}
        # Binary floating point rounds conservatively down, never above risk.
        self.assertEqual(499.0, safe_quantity(Adapter(), proposal, 2.5))


if __name__ == "__main__": unittest.main()
