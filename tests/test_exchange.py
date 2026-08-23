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

class ExchangeTests(unittest.TestCase):
    def test_dynamic_products_paginate_and_filter(self):
        ex=Exchange.__new__(Exchange);ex.client=ProductClient();products=ex.eligible_products()
        self.assertEqual([p["product_id"] for p in products],["AAA-USDC"])
        self.assertEqual(ex.client.calls,2)
