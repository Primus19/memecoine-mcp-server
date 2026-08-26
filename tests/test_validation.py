import unittest

from app.validation import promotion_gate, walk_forward_splits


class ValidationTests(unittest.TestCase):
    def test_challenger_cannot_promote_with_small_sample(self):
        result = promotion_gate([1.0] * 30, minimum_samples=100)
        self.assertFalse(result.eligible)
        self.assertIn("sample size below 100", result.reasons)

    def test_cost_stress_can_block_promotion(self):
        result = promotion_gate([.1] * 100, minimum_samples=100, cost_stress=.2)
        self.assertFalse(result.eligible)
        self.assertIn("cost-stressed mean is not positive", result.reasons)

    def test_walk_forward_splits_are_chronological_and_embargoed(self):
        splits = walk_forward_splits(100, train=40, test=20, embargo=5)
        self.assertEqual(range(0, 40), splits[0][0])
        self.assertEqual(range(45, 65), splits[0][1])


if __name__ == "__main__": unittest.main()
