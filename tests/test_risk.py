import unittest
from datetime import datetime, timedelta, timezone

from app.risk import TicketRejected, validate_ticket


def ticket(notional=23.75):
    return {
        "ticket_id": "x", "product_id": "TURBO-USDC", "regime": "RISING", "score": 90,
        "news_score": 5, "change_1h_pct": 1, "change_24h_pct": 5,
        "market_cap_usd": 100_000_000, "volume_24h_usd": 20_000_000, "turnover": .2,
        "spread_bps": 20, "slippage_bps": 20, "identity_verified": True,
        "spot_available": True, "no_safety_veto": True, "notional_usdc": notional,
        "max_loss_usdc": 2, "limit_price": .1, "stop_price": .092, "target_price": .115,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }


PRODUCT = {"product_id": "TURBO-USDC", "product_type": "SPOT", "trading_disabled": False, "view_only": False}


class RiskTests(unittest.TestCase):
    def validate(self, value, **kwargs):
        validate_ticket(value, available_usdc=kwargs.get("cash", 25), permitted_capital=kwargs.get("capital", 25), open_positions=kwargs.get("positions", 0), product=kwargs.get("product", PRODUCT))

    def test_accept_dynamic_coinbase_product(self): self.validate(ticket())

    def test_reject_non_usdc_market(self):
        t = ticket(); t["product_id"] = "BTC-USD"
        with self.assertRaisesRegex(TicketRejected, "USDC-quoted"):
            self.validate(t, product={**PRODUCT, "product_id": "BTC-USD"})

    def test_compounds_realized_profit(self): self.validate(ticket(28.5), cash=30, capital=30)

    def test_rejects_external_cash_above_permitted_capital(self):
        with self.assertRaisesRegex(TicketRejected, "envelope"): self.validate(ticket(28), cash=40, capital=25)

    def test_reject_mixed(self):
        t = ticket(); t["regime"] = "MIXED"
        with self.assertRaises(TicketRejected): self.validate(t)

    def test_reject_duplicate_position(self):
        with self.assertRaises(TicketRejected): self.validate(ticket(), positions=1)

    def test_reject_excess_loss(self):
        t = ticket(); t["max_loss_usdc"] = 3
        with self.assertRaises(TicketRejected): self.validate(t)
