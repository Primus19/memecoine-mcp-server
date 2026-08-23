import os
import unittest
from unittest.mock import patch

from app.broker_adapters import AlpacaAdapter, BrokerError, BrokerPolicy, OandaAdapter


class BrokerAdapterTests(unittest.TestCase):
    def test_multi_asset_live_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(BrokerPolicy.from_env().live_enabled)

    def test_oanda_preview_does_not_submit(self):
        with patch.dict(os.environ, {"OANDA_API_TOKEN": "x", "OANDA_ACCOUNT_ID": "101-001-123-001"}):
            result = OandaAdapter().preview({"proposal_id": "p1", "symbol": "EUR_USD", "side": "BUY",
                                             "quantity": 100, "stop_price": 1.09, "target_price": 1.12,
                                             "maximum_loss_usd": 2.5})
        self.assertTrue(result["preview_only"])
        self.assertEqual(100, result["units"])

    def test_alpaca_rejects_undefined_risk_option(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "x", "ALPACA_API_SECRET": "y"}):
            with self.assertRaisesRegex(BrokerError, "defined-risk"):
                AlpacaAdapter().preview({"proposal_id": "p1", "asset_class": "OPTION", "strategy": "SHORT_CALL",
                                         "symbol": "SPY", "side": "BUY", "quantity": 1, "reference_price": 1,
                                         "maximum_loss_usd": 25})


if __name__ == "__main__": unittest.main()
