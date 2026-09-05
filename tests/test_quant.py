import unittest

from app.quant import (average_true_range, conservative_probability, ewma_volatility,
                       expected_net_value, liquidity_quality, multi_horizon_consensus)


class QuantTests(unittest.TestCase):
    def test_atr_includes_gaps(self):
        candles = [
            {"mid": {"h": "10", "l": "9", "c": "9.5"}},
            {"mid": {"h": "12", "l": "11", "c": "11.5"}},
        ]
        self.assertAlmostEqual(1.75, average_true_range(candles, 14))

    def test_ewma_volatility_is_positive(self):
        self.assertGreater(ewma_volatility([100, 101, 99, 103, 102]), 0)

    def test_multi_horizon_consensus_is_weighted(self):
        result = multi_horizon_consensus([1, -1, 2], [.2, .1, .7])
        self.assertEqual(1, result["direction"])
        self.assertAlmostEqual(.9, result["agreement"])

    def test_liquidity_fails_closed_on_stale_quote(self):
        fresh = liquidity_quality(spread_bps=2, median_spread_bps=3, bid_liquidity=10,
                                  ask_liquidity=10, quote_age_seconds=0)
        stale = liquidity_quality(spread_bps=2, median_spread_bps=3, bid_liquidity=10,
                                  ask_liquidity=10, quote_age_seconds=100)
        self.assertGreater(fresh, stale)

    def test_expected_value_includes_all_costs(self):
        result = expected_net_value(win_probability=.6, expected_gain_bps=200,
                                    expected_loss_bps=100, fee_bps=20,
                                    spread_bps=5, slippage_bps=5, financing_bps=2)
        self.assertEqual(48, result.expected_net_bps)

    def test_probability_is_deliberately_bounded(self):
        self.assertEqual(.65, conservative_probability(100, 1))


if __name__ == "__main__": unittest.main()


class SizingAndPerformanceTests(unittest.TestCase):
    def test_quarter_kelly_is_bounded_and_zero_without_edge(self):
        from app.quant import kelly_fraction
        self.assertAlmostEqual(.25 * (.55 - .45 / 2), kelly_fraction(.55, 2.0))
        self.assertEqual(0.0, kelly_fraction(.30, 1.0))
        self.assertEqual(0.0, kelly_fraction(.60, 0.0))

    def test_volatility_targeting_scales_inversely_with_volatility(self):
        from app.quant import volatility_target_notional
        calm = volatility_target_notional(10_000, .10, target_annual_volatility=.15, max_notional_fraction=1.0)
        wild = volatility_target_notional(10_000, .90, target_annual_volatility=.15, max_notional_fraction=1.0)
        self.assertEqual(10_000, calm)  # capped at 100% of equity
        self.assertAlmostEqual(10_000 * .15 / .90, wild)
        self.assertEqual(0.0, volatility_target_notional(10_000, 0.0))

    def test_stop_distance_quantity(self):
        from app.quant import stop_distance_quantity
        self.assertAlmostEqual(2.0, stop_distance_quantity(20, 110, 100))
        self.assertEqual(0.0, stop_distance_quantity(20, 100, 100))

    def test_max_drawdown_and_sharpe(self):
        from app.quant import max_drawdown, sharpe_ratio
        self.assertAlmostEqual(.5, max_drawdown([100, 120, 60, 90]))
        self.assertEqual(0.0, max_drawdown([1, 2, 3]))
        self.assertGreater(sharpe_ratio([.01, .02, .01, .015], 252), 0)
        self.assertEqual(0.0, sharpe_ratio([.01]))

    def test_expectancy_in_r_units(self):
        from app.quant import expectancy_r
        summary = expectancy_r([2.0, -1.0, 2.0, -1.0])
        self.assertEqual(4, summary["trades"])
        self.assertAlmostEqual(.5, summary["expectancy_r"])
        self.assertAlmostEqual(2.0, summary["payoff_ratio"])
        self.assertAlmostEqual(2.0, summary["profit_factor"])
