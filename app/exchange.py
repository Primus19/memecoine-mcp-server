from __future__ import annotations

from typing import Any
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from coinbase.rest import RESTClient


class CoinbaseOrderRejected(RuntimeError):
    """Coinbase returned a structured rejection instead of an order id."""

    def __init__(self, detail: str, *, response: dict | None = None):
        super().__init__(detail)
        self.response = response or {}


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
        available = sum(float(field(a.available_balance, "value") or field(a.available_balance, "amount") or 0) for a in accounts if a.currency == "USDC")
        total = sum(float(field(a.available_balance, "value") or field(a.available_balance, "amount") or 0) + float(field(a.hold, "value") or field(a.hold, "amount") or 0) for a in accounts if a.currency == "USDC")
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
        products = []
        cursor = None
        for _ in range(10):
            params = {"limit": limit, "product_type": "SPOT", "get_tradability_status": True}
            if cursor:
                params["cursor"] = cursor
            response = self.client.get_products(**params)
            for raw in field(response, "products", []):
                data = as_dict(raw)
                product_id = str(data.get("product_id", "")).upper()
                if not product_id.endswith("-USDC"):
                    continue
                if any(bool(data.get(k, False)) for k in ("trading_disabled", "view_only", "cancel_only")):
                    continue
                products.append({"product_id": product_id,"price": float(data.get("price") or 0),"volume_24h": float(data.get("volume_24h") or 0),"price_percentage_change_24h": float(data.get("price_percentage_change_24h") or 0),"quote_min_size": float(data.get("quote_min_size") or 0)})
            pagination = field(response, "pagination", {}) or {}
            cursor = field(pagination, "next_cursor", None)
            if not cursor or not field(pagination, "has_next", False):
                break
        return products

    def buy_configuration(self, ticket: dict, product: dict) -> dict:
        limit_price = self.normalize_price(product, float(ticket["limit_price"]), rounding=ROUND_DOWN)
        base_size = self.normalize_base_size(product, float(ticket["notional_usdc"]) / float(limit_price))
        if float(base_size) < float(product.get("base_min_size") or 0):
            raise RuntimeError("derived base size is below Coinbase minimum")
        return {"limit_limit_gtc": {"base_size": base_size, "limit_price": limit_price, "post_only": False}}

    def attached_configuration(self, ticket: dict, product: dict) -> dict:
        # Both bracket legs must obey the product quote increment. Round the
        # profit target and protective stop upward so normalization never
        # weakens either requested exit threshold for a long position.
        return {"trigger_bracket_gtc": {
            "limit_price": self.normalize_price(product, float(ticket["target_price"]), rounding=ROUND_UP),
            "stop_trigger_price": self.normalize_price(product, float(ticket["stop_price"]), rounding=ROUND_UP),
        }}

    def preview_buy(self, ticket: dict, product: dict):
        return self.client.preview_order(
            product_id=ticket["product_id"], side="BUY",
            order_configuration=self.buy_configuration(ticket, product),
            attached_order_configuration=self.attached_configuration(ticket, product),
        )

    def execution_quote(self, product_id: str, quote_size: float, limit: int = 50) -> dict:
        """Reconstruct spread and buy slippage from the live Coinbase book."""
        response = self.client.get_product_book(product_id=product_id, limit=limit)
        book = field(response, "pricebook", response)
        bids = list(field(book, "bids", []) or [])
        asks = list(field(book, "asks", []) or [])
        if not bids or not asks:
            raise RuntimeError("Coinbase order book has no executable bid/ask")

        def level(raw: Any) -> tuple[float, float]:
            return float(field(raw, "price", 0) or 0), float(field(raw, "size", 0) or 0)

        best_bid, _ = level(bids[0]); best_ask, _ = level(asks[0])
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            raise RuntimeError("Coinbase order book is invalid")
        remaining = float(quote_size); base = 0.0; spent = 0.0
        for raw in asks:
            price, size = level(raw)
            if price <= 0 or size <= 0:
                continue
            take_quote = min(remaining, price * size)
            base += take_quote / price; spent += take_quote; remaining -= take_quote
            if remaining <= 1e-9:
                break
        if remaining > 0.01 or base <= 0:
            raise RuntimeError("insufficient visible liquidity for requested order")
        average = spent / base
        return {
            "best_bid": best_bid, "best_ask": best_ask, "average_buy_price": average,
            "spread_bps": (best_ask-best_bid)/((best_ask+best_bid)/2)*10_000,
            "slippage_bps": (average-best_ask)/best_ask*10_000,
            "visible_quote_filled": spent,
        }

    def normalize_increment(self, value: float, increment: str, *, rounding=ROUND_DOWN) -> str:
        raw = Decimal(str(value))
        step = Decimal(str(increment))
        if not raw.is_finite() or raw <= 0:
            raise RuntimeError("Coinbase order value must be positive and finite")
        if not step.is_finite() or step <= 0:
            raise RuntimeError("Coinbase product increment must be positive and finite")
        units = (raw / step).to_integral_value(rounding=rounding)
        normalized = (units * step).quantize(step)
        if normalized <= 0:
            raise RuntimeError("Coinbase order value is below the product increment")
        return format(normalized, "f")

    def normalize_base_size(self, product: dict, size: float) -> str:
        increment = str(product.get("base_increment") or "0.00000001")
        return self.normalize_increment(size, increment, rounding=ROUND_DOWN)

    def normalize_price(self, product: dict, price: float, *, rounding=ROUND_DOWN) -> str:
        increment = str(product.get("quote_increment") or "0.00000001")
        return self.normalize_increment(price, increment, rounding=rounding)

    def submit_buy(self, ticket: dict, product: dict) -> dict:
        response = self.client.create_order(
            client_order_id=ticket["ticket_id"], product_id=ticket["product_id"], side="BUY",
            order_configuration=self.buy_configuration(ticket, product),
            attached_order_configuration=self.attached_configuration(ticket, product),
        )
        result = as_dict(response)
        success = as_dict(result.get("success_response"))
        order_id = str(success.get("order_id") or result.get("order_id") or "").strip()
        if order_id:
            return result
        error = as_dict(result.get("error_response"))
        code = str(error.get("error") or error.get("error_code") or "UNKNOWN")
        message = str(error.get("message") or error.get("error_details") or "Coinbase returned no order id")
        # Preserve the useful broker diagnosis without leaking the whole raw
        # response into logs or reports.
        raise CoinbaseOrderRejected(f"{code}: {message}", response={"error": code, "message": message})

    def get_order(self, order_id: str) -> dict:
        response = self.client.get_order(order_id)
        return as_dict(field(response, "order", response))

    def base_balance(self, product_id: str) -> float:
        base = product_id.split("-", 1)[0]
        return sum(float(field(a.available_balance, "value") or field(a.available_balance, "amount") or 0) + float(field(a.hold, "value") or field(a.hold, "amount") or 0) for a in self.accounts() if a.currency == base)

    def available_base_balance(self, product_id: str) -> float:
        base = product_id.split("-", 1)[0]
        return sum(float(field(a.available_balance, "value") or field(a.available_balance, "amount") or 0) for a in self.accounts() if a.currency == base)

    def cancel_open_sell_orders(self, product_id: str) -> dict:
        # Coinbase rejects OPEN when it is combined with other order statuses.
        # OPEN is the aggregate state for active orders, including attached
        # bracket legs that have not reached a terminal state.  Query it alone
        # so a managed exit can cancel venue protection before market-selling
        # the then-available balance.
        response = self.client.get(
            "/api/v3/brokerage/orders/historical/batch",
            params={"product_ids": [product_id], "order_status": ["OPEN"],
                    "order_side": "SELL", "limit": 100},
        )
        order_ids = [str(field(order, "order_id", "")) for order in field(response, "orders", []) if field(order, "order_id", "")]
        return {"order_ids": order_ids, "response": as_dict(self.client.cancel_orders(order_ids=order_ids)) if order_ids else {}}

    def fills(self, product_id: str, opened_at: str, order_id: str = "") -> list[dict]:
        response = self.client.get_fills(product_ids=[product_id], limit=100)
        cutoff = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        result = []
        for raw in field(response, "fills", []):
            data = as_dict(raw)
            fill_order_id = str(data.get("order_id", ""))
            if order_id and fill_order_id != str(order_id):
                continue
            stamp = str(data.get("trade_time") or data.get("trade_time_utc") or "")
            try:
                if datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                continue
            result.append({"order_id": fill_order_id, "side": str(data.get("side", "")).upper(), "price": float(data.get("price") or 0), "size": float(data.get("size") or 0), "commission": float(data.get("commission") or 0), "trade_time": stamp})
        return result

    def cancel_order(self, order_id: str) -> dict:
        return as_dict(self.client.cancel_orders(order_ids=[order_id]))

    def market_sell(self, product_id: str, base_size: float, client_order_id: str) -> dict:
        return as_dict(self.client.create_order(client_order_id=client_order_id, product_id=product_id, side="SELL", order_configuration={"market_market_ioc": {"base_size": str(base_size)}}))
