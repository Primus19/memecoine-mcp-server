import unittest
from datetime import datetime, timedelta, timezone
from app.risk import TicketRejected, validate_ticket


def ticket():
    return {"ticket_id":"x","product_id":"DOGE-USDC","regime":"RISING","score":90,"news_score":5,"change_1h_pct":1,"change_24h_pct":5,"market_cap_usd":100_000_000,"volume_24h_usd":20_000_000,"turnover":.2,"spread_bps":20,"slippage_bps":20,"identity_verified":True,"spot_available":True,"no_safety_veto":True,"notional_usdc":25,"max_loss_usdc":2,"limit_price":.1,"stop_price":.092,"target_price":.115,"expires_at":(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}


class RiskTests(unittest.TestCase):
    def test_accept(self): validate_ticket(ticket(),25,0,{"DOGE-USDC"})
    def test_reject_mixed(self):
        t=ticket(); t["regime"]="MIXED"
        with self.assertRaises(TicketRejected): validate_ticket(t,25,0,{"DOGE-USDC"})
    def test_reject_duplicate_position(self):
        with self.assertRaises(TicketRejected): validate_ticket(ticket(),25,1,{"DOGE-USDC"})
    def test_reject_excess_loss(self):
        t=ticket(); t["max_loss_usdc"]=3
        with self.assertRaises(TicketRejected): validate_ticket(t,25,0,{"DOGE-USDC"})

