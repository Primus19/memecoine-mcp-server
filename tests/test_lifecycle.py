import unittest

from app.lifecycle import profit_protection_challenger,supervision_levels


class LifecycleTests(unittest.TestCase):
    def test_half_r_favorable_move_activates_risk_ratchet(self):
        result = supervision_levels(self.ticket, entry=100, mark=100, high_water=104)
        self.assertTrue(result["risk_ratchet_active"])
        self.assertAlmostEqual(-.1, result["risk_ratchet_floor_r"])
        self.assertGreater(result["effective_stop_price"], self.ticket["stop_price"])

    def setUp(self):
        self.ticket={"stop_price":92,"trail_activation_pct":5,"trail_pct":4,
                     "spread_bps":20,"slippage_bps":20,
                     "opportunity_policy":{"estimated_fee_bps_per_side":120,"minimum_net_edge_bps":50}}

    def test_hard_stop_fallback(self):
        result=supervision_levels(self.ticket,entry=100,mark=91,high_water=102)
        self.assertEqual("HARD_STOP_FALLBACK",result["exit_reason"])
        self.assertFalse(result["trail_active"])

    def test_trail_activates_and_ratchets(self):
        result=supervision_levels(self.ticket,entry=100,mark=118,high_water=120)
        self.assertTrue(result["trail_active"])
        self.assertEqual(117.0,result["effective_stop_price"])
        self.assertEqual("",result["exit_reason"])

    def test_trailing_stop_exit(self):
        result=supervision_levels(self.ticket,entry=100,mark=110,high_water=120)
        self.assertEqual("TRAILING_STOP",result["exit_reason"])

    def test_single_falling_regime_observation_does_not_exit(self):
        result=supervision_levels(self.ticket,entry=100,mark=108,high_water=110,regime="FALLING")
        self.assertEqual("",result["exit_reason"])

    def test_confirmed_falling_regime_exits_only_deteriorating_loser(self):
        result=supervision_levels(self.ticket,entry=100,mark=97,high_water=101,regime="FALLING",falling_observations=3)
        self.assertEqual("FALLING_REGIME_CONFIRMED",result["exit_reason"])

    def test_confirmed_falling_regime_does_not_override_healthy_profit(self):
        result=supervision_levels(self.ticket,entry=100,mark=105,high_water=106,regime="FALLING",falling_observations=3)
        self.assertEqual("",result["exit_reason"])

    def test_fee_aware_break_even_protects_after_costs_clear(self):
        result=supervision_levels(self.ticket,entry=100,mark=100,high_water=104)
        self.assertTrue(result["break_even_active"])
        self.assertEqual(100,result["effective_stop_price"])
        self.assertEqual("TRAILING_STOP",result["exit_reason"])

    def test_position_momentum_reversal_exits_before_broad_regime_falls(self):
        result=supervision_levels(self.ticket,entry=100,mark=102,high_water=105,momentum_1h_pct=-1.2)
        self.assertEqual("POSITION_MOMENTUM_REVERSAL",result["exit_reason"])

    def test_profit_protection_challenger_is_shadow_only_and_locks_break_even(self):
        result=profit_protection_challenger(self.ticket,entry=100,mark=102,high_water=104)
        self.assertTrue(result["shadow_only"])
        self.assertTrue(result["break_even_active"])
        self.assertFalse(result["trail_active"])
        self.assertEqual(100,result["effective_stop_price"])
        self.assertFalse(result["would_exit"])

    def test_profit_protection_challenger_trails_after_five_percent(self):
        result=profit_protection_challenger(self.ticket,entry=100,mark=100.7,high_water=105)
        self.assertTrue(result["trail_active"])
        self.assertAlmostEqual(100.8,result["effective_stop_price"])
        self.assertTrue(result["would_exit"])


if __name__=="__main__":unittest.main()
