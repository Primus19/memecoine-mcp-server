import unittest
import sys
from types import SimpleNamespace
sys.modules.setdefault("coinbase",SimpleNamespace())
sys.modules.setdefault("coinbase.rest",SimpleNamespace(RESTClient=object))
from app.exchange import Exchange

class ProductClient:
    def __init__(self): self.calls=0
    def get_products(self,**params):
        self.calls+=1
        if self.calls==1:return SimpleNamespace(products=[{"product_id":"AAA-USDC","price":"1","volume_24h":"20","view_only":False}],pagination={"next_cursor":"next","has_next":True})
        return SimpleNamespace(products=[{"product_id":"BBB-USD","price":"2"},{"product_id":"CCC-USDC","price":"3","trading_disabled":True}],pagination={"has_next":False})

class BookClient:
    def get_product_book(self,**kwargs):
        return {"pricebook":{"bids":[{"price":"0.999","size":"100"}],"asks":[{"price":"1.001","size":"10"},{"price":"1.002","size":"20"}]}}

class ExchangeTests(unittest.TestCase):
    def test_dynamic_products_paginate_and_filter(self):
        ex=Exchange.__new__(Exchange);ex.client=ProductClient();products=ex.eligible_products()
        self.assertEqual([p["product_id"] for p in products],["AAA-USDC"])
        self.assertEqual(ex.client.calls,2)

    def test_execution_quote_walks_live_book(self):
        ex=Exchange.__new__(Exchange);ex.client=BookClient();quote=ex.execution_quote("AAA-USDC",25)
        self.assertLess(quote["spread_bps"],50)
        self.assertGreater(quote["average_buy_price"],quote["best_ask"])
        self.assertAlmostEqual(quote["visible_quote_filled"],25)

    def test_limit_buy_uses_rounded_base_size(self):
        ex=Exchange.__new__(Exchange)
        config=ex.buy_configuration({"notional_usdc":25,"limit_price":3}, {"base_increment":"0.01","base_min_size":.01})
        self.assertEqual(config["limit_limit_gtc"]["base_size"],"8.33")
        self.assertNotIn("quote_size",config["limit_limit_gtc"])
