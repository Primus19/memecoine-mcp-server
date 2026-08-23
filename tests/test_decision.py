import unittest
from app.decision import build_recommendation,canonical_hash

def candidate():
    return {"product_id":"TURBO-USDC","regime":"RISING","component_scores":{"regime":15,"liquidity":18,"momentum":12,"volume_quality":11,"tokenomics":8,"safety":14,"news":5,"social":2},"change_1h_pct":1,"change_24h_pct":4,"market_cap_usd":100_000_000,"volume_24h_usd":20_000_000,"turnover":.2,"spread_bps":20,"slippage_bps":20,"identity_verified":True,"spot_available":True,"no_safety_veto":True,"notional_usdc":20,"max_loss_usdc":2,"limit_price":.1,"stop_price":.092,"target_price":.115,"evidence_urls":["https://example.com/source"]}

class DecisionTests(unittest.TestCase):
    def test_score_is_computed_not_trusted(self):
        c=candidate();c["score"]=1;r=build_recommendation(c);self.assertEqual(r["score"],85)
    def test_hash_changes_when_ticket_changes(self):
        r=build_recommendation(candidate());h=r["recommendation_hash"];r["limit_price"]=.2;self.assertNotEqual(h,canonical_hash({k:v for k,v in r.items() if k!="recommendation_hash"}))
    def test_component_limit_is_enforced(self):
        c=candidate();c["component_scores"]["news"]=11
        with self.assertRaises(ValueError):build_recommendation(c)
    def test_opportunity_policy_is_frozen_into_hash(self):
        r=build_recommendation(candidate())
        self.assertEqual("1.0",r["opportunity_policy"]["version"])
        self.assertEqual(78,r["opportunity_policy"]["min_score"])
