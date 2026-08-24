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

    @patch("app.broker_adapters.request_json")
    def test_oanda_order_has_attached_protection_and_client_id(self, request):
        request.side_effect = [
            {"instruments": [{"name": "EUR_USD", "displayPrecision": 5}]},
            {"orderCreateTransaction": {"id": "11"}},
        ]
        with patch.dict(os.environ, {"OANDA_API_TOKEN": "x", "OANDA_ACCOUNT_ID": "101-001-123-001"}):
            adapter = OandaAdapter()
            adapter.create_order({"quantity": 100, "side": "BUY", "symbol": "EUR_USD",
                                  "reference_price": 1.10,
                                  "stop_price": 1.09, "target_price": 1.12}, client_order_id="intent123")
        payload = request.call_args.kwargs["payload"]["order"]
        self.assertEqual("intent123", payload["clientExtensions"]["id"])
        self.assertEqual("1.09000", payload["stopLossOnFill"]["price"])
        self.assertEqual("1.12000", payload["takeProfitOnFill"]["price"])
        self.assertEqual("1.10165", payload["priceBound"])

    def test_alpaca_rejects_undefined_risk_option(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "x", "ALPACA_API_SECRET": "y"}):
            with self.assertRaisesRegex(BrokerError, "defined-risk"):
                AlpacaAdapter().preview({"proposal_id": "p1", "asset_class": "OPTION", "strategy": "SHORT_CALL",
                                         "symbol": "SPY", "side": "BUY", "quantity": 1, "reference_price": 1,
                                         "maximum_loss_usd": 25})


if __name__ == "__main__": unittest.main()
