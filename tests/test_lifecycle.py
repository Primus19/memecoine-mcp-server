import unittest

from app.lifecycle import supervision_levels


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.ticket={"stop_price":92,"trail_activation_pct":12,"trail_pct":8}

    def test_hard_stop_fallback(self):
        result=supervision_levels(self.ticket,entry=100,mark=91,high_water=105)
        self.assertEqual("HARD_STOP_FALLBACK",result["exit_reason"])
        self.assertFalse(result["trail_active"])

    def test_trail_activates_and_ratchets(self):
        result=supervision_levels(self.ticket,entry=100,mark=115,high_water=120)
        self.assertTrue(result["trail_active"])
        self.assertEqual(110.4,result["effective_stop_price"])
        self.assertEqual("",result["exit_reason"])

    def test_trailing_stop_exit(self):
        result=supervision_levels(self.ticket,entry=100,mark=110,high_water=120)
        self.assertEqual("TRAILING_STOP",result["exit_reason"])

    def test_falling_regime_is_immediate_exit(self):
        result=supervision_levels(self.ticket,entry=100,mark=105,high_water=110,regime="FALLING")
        self.assertEqual("FALLING_REGIME",result["exit_reason"])


if __name__=="__main__":unittest.main()
