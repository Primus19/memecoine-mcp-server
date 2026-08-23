import unittest

from app.enrichment import enrich_with_coinbase


class CoinbaseEnrichmentTests(unittest.TestCase):
    def test_live_exchange_facts_replace_caller_assertions(self):
        candidate={"product_id":"turbo-usdc","spread_bps":0,"slippage_bps":0,"identity_verified":False,"spot_available":False}
        product={"product_id":"TURBO-USDC","product_type":"SPOT","trading_disabled":False,"view_only":False,"cancel_only":False}
        quote={"best_bid":.099,"best_ask":.1,"average_buy_price":.1002,"spread_bps":10,"slippage_bps":20,"visible_quote_filled":23.75}
        result=enrich_with_coinbase(candidate,product=product,quote=quote,checked_at="2026-08-23T00:00:00+00:00")
        self.assertTrue(result["identity_verified"])
        self.assertTrue(result["spot_available"])
        self.assertEqual(result["spread_bps"],10)
        self.assertEqual(result["slippage_bps"],20)
        self.assertEqual(result["coinbase_evidence"]["best_ask"],.1)

    def test_mismatched_or_disabled_product_cannot_be_verified(self):
        candidate={"product_id":"FAKE-USDC","identity_verified":True,"spot_available":True}
        product={"product_id":"TURBO-USDC","product_type":"SPOT","trading_disabled":True}
        quote={"best_bid":1,"best_ask":1.01,"average_buy_price":1.01,"spread_bps":10,"slippage_bps":0,"visible_quote_filled":5}
        result=enrich_with_coinbase(candidate,product=product,quote=quote)
        self.assertFalse(result["identity_verified"])
        self.assertFalse(result["spot_available"])


if __name__ == "__main__":
    unittest.main()
