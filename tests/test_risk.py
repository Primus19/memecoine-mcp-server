import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.risk import TicketRejected, risk_size_ticket, validate_ticket


def ticket(notional=23.75):
    return {
        "ticket_id": "x", "product_id": "TURBO-USDC", "regime": "RISING", "score": 90,
        "news_score": 5, "change_1h_pct": 1, "change_24h_pct": 5,
        "market_cap_usd": 100_000_000, "volume_24h_usd": 20_000_000, "turnover": .2,
        "spread_bps": 20, "slippage_bps": 20, "identity_verified": True,
        "spot_available": True, "no_safety_veto": True, "notional_usdc": notional,
        "max_loss_usdc": .48, "limit_price": .1, "stop_price": .098, "target_price": .115,
        "reference_price": .1, "source_timestamp": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(),
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

    def test_compounds_realized_profit(self):
        t=ticket(28.5);t["stop_price"]=.0983;t["max_loss_usdc"]=.4845
        self.validate(t,cash=30,capital=30)

    def test_rejects_external_cash_above_permitted_capital(self):
        with self.assertRaisesRegex(TicketRejected, "envelope"): self.validate(ticket(28), cash=40, capital=25)

    def test_accept_mixed_under_opportunity_policy(self):
        t = ticket(); t["regime"] = "MIXED"
        self.validate(t)

    def test_can_disable_mixed_regime(self):
        t = ticket(); t["regime"] = "MIXED"
        with patch.dict("os.environ", {"LIVE_ALLOW_MIXED_REGIME": "false"}):
            with self.assertRaisesRegex(TicketRejected, "regime"):
                self.validate(t)

    def test_news_is_optional_without_veto(self):
        t = ticket(); t["news_score"] = 0
        self.validate(t)

    def test_news_veto_remains_hard(self):
        t = ticket(); t["news_score"] = 10; t["news_veto"] = True
        with self.assertRaisesRegex(TicketRejected, "news"):
            self.validate(t)

    def test_reject_duplicate_position(self):
        with self.assertRaises(TicketRejected): self.validate(ticket(), positions=1)

    def test_reject_excess_loss(self):
        t = ticket(); t["max_loss_usdc"] = .51
        with self.assertRaises(TicketRejected): self.validate(t)

    def test_reject_stale_source(self):
        t=ticket();t["source_timestamp"]=(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()
        with self.assertRaisesRegex(TicketRejected,"fresh"):self.validate(t)

    def test_rejects_trade_whose_target_cannot_clear_round_trip_costs(self):
        t=ticket();t["target_price"]=.102
        with self.assertRaisesRegex(TicketRejected,"round-trip costs"):
            self.validate(t)

    def test_fee_assumption_is_configurable_but_remains_fee_aware(self):
        t=ticket();t["target_price"]=.105
        with patch.dict("os.environ",{"LIVE_ESTIMATED_FEE_BPS_PER_SIDE":"10",
                                      "LIVE_MIN_NET_EDGE_BPS":"25"}):
            self.validate(t)

    def test_emerging_tier_is_small_and_stricter(self):
        t=ticket(5);t.update(opportunity_tier="EMERGING",market_cap_usd=15_000_000,
                             volume_24h_usd=2_000_000,max_loss_usdc=.25,score=86,
                             spread_bps=25,slippage_bps=25,stop_price=.095)
        self.validate(t)
        t["notional_usdc"]=6
        with self.assertRaisesRegex(TicketRejected,"emerging-tier notional"):
            self.validate(t)

    def test_emerging_tier_rejects_wide_execution(self):
        t=ticket(5);t.update(opportunity_tier="EMERGING",market_cap_usd=15_000_000,
                             volume_24h_usd=2_000_000,max_loss_usdc=.25,score=86,
                             spread_bps=31,slippage_bps=25,stop_price=.095)
        with self.assertRaisesRegex(TicketRejected,"30 bps"):
            self.validate(t)

    def test_recovery_mode_reduces_risk_budget_and_notional(self):
        sized=risk_size_ticket(ticket(),permitted_capital=23.75,available_usdc=23.75,
                               allocation_fraction=.35,risk_multiplier=.625)
        self.assertLessEqual(sized["notional_usdc"],23.75*.35)
        self.assertAlmostEqual(.296875,sized["risk_budget_usdc"])
        self.assertEqual(.625,sized["risk_multiplier"])

    def test_recovery_score_boost_rejects_marginal_candidate(self):
        candidate=ticket(8);candidate["score"]=80
        with self.assertRaisesRegex(TicketRejected,"score below 82"):
            validate_ticket(candidate,available_usdc=25,permitted_capital=25,
                            open_positions=0,product=PRODUCT,allocation_fraction=.35,
                            minimum_score_boost=4)


class ExecutionCostPolicyTests(unittest.TestCase):
    def validate(self, value):
        validate_ticket(value, available_usdc=25, permitted_capital=25, open_positions=0, product=PRODUCT)

    def test_post_only_entry_lowers_required_edge(self):
        # 620 bps gross target. Taker round trip (240 fee + 20 spread + 40 slippage) leaves
        # negative expected value at the conservative 0.56 win probability; a post-only
        # entry (60 maker + 120 taker) turns the same ticket positive.
        t=ticket();t["target_price"]=.1062
        with self.assertRaisesRegex(TicketRejected,"expected value is not positive"):
            self.validate(t)
        with patch.dict("os.environ",{"LIVE_ENTRY_POST_ONLY":"true","LIVE_MAKER_FEE_BPS_PER_SIDE":"60"}):
            self.validate(t)

    def test_positive_1h_gate_is_policy_controlled(self):
        t=ticket();t["change_1h_pct"]=-.2
        with self.assertRaisesRegex(TicketRejected,"1h momentum gate failed"):
            self.validate(t)
        with patch.dict("os.environ",{"LIVE_REQUIRE_POSITIVE_1H":"false"}):
            self.validate(t)
        t["change_24h_pct"]=-1
        with patch.dict("os.environ",{"LIVE_REQUIRE_POSITIVE_1H":"false"}):
            with self.assertRaisesRegex(TicketRejected,"24h momentum gate failed"):
                self.validate(t)
