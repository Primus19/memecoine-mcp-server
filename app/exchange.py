from __future__ import annotations

from typing import Any

from coinbase.rest import RESTClient


def as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {k: v for k, v in vars(value).items() if not k.startswith("_")}


def field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class Exchange:
    def __init__(self, key_name: str, private_key: str):
        self.client = RESTClient(api_key=key_name, api_secret=private_key.replace("\\n", "\n"))

    def accounts(self) -> list[Any]:
        return list(self.client.get_accounts(limit=250).accounts)

    def preflight(self) -> dict:
        p = self.client.get_api_key_permissions()
        permissions = {"can_view": bool(p.can_view), "can_trade": bool(p.can_trade), "can_transfer": bool(p.can_transfer)}
        if not permissions["can_view"] or not permissions["can_trade"] or permissions["can_transfer"]:
            raise RuntimeError("Key must have View + Trade and Transfer disabled")
        accounts = self.accounts()
        available = sum(float(a.available_balance.value) for a in accounts if a.currency == "USDC")
        total = sum(float(a.available_balance.value) + float(a.hold.value) for a in accounts if a.currency == "USDC")
        return {"permissions": permissions, "usdc_available": available, "usdc_total": total}

    def product(self, product_id: str) -> dict:
        raw = self.client.get_product(product_id.upper())
        data = as_dict(raw)
        return {
            "product_id": str(data.get("product_id", product_id)).upper(),
            "product_type": str(data.get("product_type", "SPOT")).upper(),
            "price": float(data.get("price") or 0),
            "volume_24h": float(data.get("volume_24h") or 0),
            "base_increment": str(data.get("base_increment") or ""),
            "quote_increment": str(data.get("quote_increment") or ""),
            "base_min_size": float(data.get("base_min_size") or 0),
            "quote_min_size": float(data.get("quote_min_size") or 0),
            "trading_disabled": bool(data.get("trading_disabled", False)),
            "view_only": bool(data.get("view_only", False)),
            "cancel_only": bool(data.get("cancel_only", False)),
            "limit_only": bool(data.get("limit_only", False)),
        }

    def eligible_products(self, limit: int = 250) -> list[dict]:
        response = self.client.get_products(limit=limit, product_type="SPOT", get_tradability_status=True)
        products = []
        for raw in field(response, "products", []):
            data = as_dict(raw)
            product_id = str(data.get("product_id", "")).upper()
            if not product_id.endswith("-USDC"):
                continue
            if any(bool(data.get(k, False)) for k in ("trading_disabled", "view_only", "cancel_only")):
                continue
            products.append({
                "product_id": product_id,
                "price": float(data.get("price") or 0),
                "volume_24h": float(data.get("volume_24h") or 0),
                "price_percentage_change_24h": float(data.get("price_percentage_change_24h") or 0),
                "quote_min_size": float(data.get("quote_min_size") or 0),
            })
        return products

    def preview_buy(self, ticket: dict):
        return self.client.preview_order(
            product_id=ticket["product_id"], side="BUY",
            order_configuration={"limit_limit_gtc": {"quote_size": str(ticket["notional_usdc"]), "limit_price": str(ticket["limit_price"]), "post_only": False}},
        )

    def submit_buy(self, ticket: dict) -> dict:
        response = self.client.create_order(
            client_order_id=ticket["ticket_id"], product_id=ticket["product_id"], side="BUY",
            order_configuration={"limit_limit_gtc": {"quote_size": str(ticket["notional_usdc"]), "limit_price": str(ticket["limit_price"]), "post_only": False}},
            attached_order_configuration={"trigger_bracket_gtc": {"limit_price": str(ticket["target_price"]), "stop_trigger_price": str(ticket["stop_price"])}}
        )
        return as_dict(response)

    def get_order(self, order_id: str) -> dict:
        response = self.client.get_order(order_id)
        return as_dict(field(response, "order", response))

    def base_balance(self, product_id: str) -> float:
        base = product_id.split("-", 1)[0]
        return sum(float(a.available_balance.value) + float(a.hold.value) for a in self.accounts() if a.currency == base)
