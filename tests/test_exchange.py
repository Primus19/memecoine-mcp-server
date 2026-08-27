import unittest
import sys
from types import SimpleNamespace
sys.modules.setdefault("coinbase",SimpleNamespace())
sys.modules.setdefault("coinbase.rest",SimpleNamespace(RESTClient=object))
from app.exchange import CoinbaseOrderRejected,Exchange

class ProductClient:
    def __init__(self): self.calls=0
    def get_products(self,**params):
        self.calls+=1
        if self.calls==1:return SimpleNamespace(products=[{"product_id":"AAA-USDC","price":"1","volume_24h":"20","view_only":False}],pagination={"next_cursor":"next","has_next":True})
        return SimpleNamespace(products=[{"product_id":"BBB-USD","price":"2"},{"product_id":"CCC-USDC","price":"3","trading_disabled":True}],pagination={"has_next":False})

class BookClient:
    def get_product_book(self,**kwargs):
        return {"pricebook":{"bids":[{"price":"0.999","size":"100"}],"asks":[{"price":"1.001","size":"10"},{"price":"1.002","size":"20"}]}}

class RejectedOrderClient:
    def create_order(self,**kwargs):
        return {"success":False,"error_response":{"error":"INVALID_ORDER_CONFIGURATION","message":"attached order is invalid"}}

class CapturingOrderClient:
    def __init__(self):
        self.preview_kwargs=None
        self.create_kwargs=None

    def preview_order(self,**kwargs):
        self.preview_kwargs=kwargs
        return {"preview_id":"preview-1"}

    def create_order(self,**kwargs):
        self.create_kwargs=kwargs
        return {"success_response":{"order_id":"order-1"}}

class OpenSellClient:
    def __init__(self):
        self.params=None
        self.cancelled=[]
    def get(self,path,params):
        self.params=params
        return {"orders":[{"order_id":"sell-1"},{"order_id":"sell-2"}]}
    def cancel_orders(self,*,order_ids):
        self.cancelled=list(order_ids)
        return {"results":[]}

class FillClient:
    def get_fills(self,**kwargs):
        return {"fills":[
            {"order_id":"wanted","side":"BUY","price":"1","size":"2","commission":".01","trade_time":"2026-08-27T00:01:00Z"},
            {"order_id":"other","side":"BUY","price":"1","size":"99","commission":".50","trade_time":"2026-08-27T00:02:00Z"},
        ]}

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

    def test_buy_and_bracket_prices_use_product_quote_increment(self):
        ex=Exchange.__new__(Exchange)
        ticket={"ticket_id":"t1","product_id":"AAA-USDC","notional_usdc":25,"limit_price":.176749,
                "target_price":.229814,"stop_price":.162609}
        product={"base_increment":"0.01","quote_increment":"0.0001","base_min_size":.01}
        buy=ex.buy_configuration(ticket,product)["limit_limit_gtc"]
        bracket=ex.attached_configuration(ticket,product)["trigger_bracket_gtc"]
        self.assertEqual(buy["limit_price"],"0.1767")
        self.assertEqual(buy["base_size"],"141.48")
        self.assertEqual(bracket["limit_price"],"0.2299")
        self.assertEqual(bracket["stop_trigger_price"],"0.1627")

    def test_increment_normalization_supports_non_power_of_ten_steps(self):
        ex=Exchange.__new__(Exchange)
        self.assertEqual(ex.normalize_price({"quote_increment":"0.05"},1.03),"1.00")
        bracket=ex.attached_configuration(
            {"target_price":1.03,"stop_price":.97},
            {"quote_increment":"0.05"},
        )["trigger_bracket_gtc"]
        self.assertEqual(bracket["limit_price"],"1.05")
        self.assertEqual(bracket["stop_trigger_price"],"1.00")

    def test_preview_and_submission_use_identical_price_payloads(self):
        ex=Exchange.__new__(Exchange);ex.client=CapturingOrderClient()
        ticket={"ticket_id":"t1","product_id":"AAA-USDC","notional_usdc":25,"limit_price":.176749,
                "target_price":.229814,"stop_price":.162609}
        product={"base_increment":"0.01","quote_increment":"0.0001","base_min_size":.01}
        ex.preview_buy(ticket,product)
        ex.submit_buy(ticket,product)
        self.assertEqual(ex.client.preview_kwargs["order_configuration"],
                         ex.client.create_kwargs["order_configuration"])
        self.assertEqual(ex.client.preview_kwargs["attached_order_configuration"],
                         ex.client.create_kwargs["attached_order_configuration"])

    def test_managed_exit_queries_open_status_alone_before_cancelling(self):
        ex=Exchange.__new__(Exchange);ex.client=OpenSellClient()
        result=ex.cancel_open_sell_orders("AAA-USDC")
        self.assertEqual(["OPEN"],ex.client.params["order_status"])
        self.assertEqual("SELL",ex.client.params["order_side"])
        self.assertEqual(["sell-1","sell-2"],ex.client.cancelled)
        self.assertEqual(["sell-1","sell-2"],result["order_ids"])

    def test_structured_order_rejection_is_not_silently_returned(self):
        ex=Exchange.__new__(Exchange);ex.client=RejectedOrderClient()
        ticket={"ticket_id":"t1","product_id":"AAA-USDC","notional_usdc":25,"limit_price":1,
                "target_price":1.3,"stop_price":.92}
        product={"base_increment":"0.01","base_min_size":.01}
        with self.assertRaisesRegex(CoinbaseOrderRejected,"INVALID_ORDER_CONFIGURATION"):
            ex.submit_buy(ticket,product)

    def test_fills_are_scoped_to_the_tracked_order(self):
        ex=Exchange.__new__(Exchange);ex.client=FillClient()
        fills=ex.fills("AAA-USDC","2026-08-27T00:00:00+00:00","wanted")
        self.assertEqual(1,len(fills))
        self.assertEqual("wanted",fills[0]["order_id"])
