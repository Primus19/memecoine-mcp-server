from __future__ import annotations

from coinbase.rest import RESTClient


class Exchange:
    def __init__(self, key_name: str, private_key: str):
        self.client = RESTClient(api_key=key_name, api_secret=private_key.replace("\\n", "\n"))

    def preflight(self) -> dict:
        p = self.client.get_api_key_permissions()
        permissions = {"can_view": bool(p.can_view), "can_trade": bool(p.can_trade), "can_transfer": bool(p.can_transfer)}
        if not permissions["can_view"] or not permissions["can_trade"] or permissions["can_transfer"]:
            raise RuntimeError("Key must have View + Trade and Transfer disabled")
        accounts = self.client.get_accounts(limit=250).accounts
        balance = sum(float(a.available_balance.value) + float(a.hold.value) for a in accounts if a.currency == "USDC")
        if balance > 30: raise RuntimeError("USDC exceeds $30 pilot ceiling; verify portfolio scope")
        return {"permissions": permissions, "usdc_equity": balance}

    def preview_buy(self, t: dict):
        return self.client.preview_order(product_id=t["product_id"], side="BUY", order_configuration={"limit_limit_gtc":{"quote_size":str(t["notional_usdc"]),"limit_price":str(t["limit_price"]),"post_only":False}})

    def submit_buy(self, t: dict):
        return self.client.create_order(client_order_id=t["ticket_id"], product_id=t["product_id"], side="BUY", order_configuration={"limit_limit_gtc":{"quote_size":str(t["notional_usdc"]),"limit_price":str(t["limit_price"]),"post_only":False}}, attached_order_configuration={"trigger_bracket_gtc":{"limit_price":str(t["target_price"]),"stop_trigger_price":str(t["stop_price"])}})

