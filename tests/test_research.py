"""Offline tests for app.research (synthetic data only; no network)."""
import math
import unittest

try:
    import numpy as np
    import pandas as pd
    HAS_DEPS = True
except ImportError:  # research deps are optional in production
    HAS_DEPS = False


def synthetic_ohlcv(n=600, seed=1, drift=0.0005, vol=0.02, start="2022-01-01"):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(high, open_), "low": np.minimum(low, open_), "close": close,
                         "volume": rng.uniform(1e6, 2e6, n)}, index=idx)


@unittest.skipUnless(HAS_DEPS, "numpy/pandas not installed")
class IndicatorTests(unittest.TestCase):
    def test_indicators_are_bounded_and_aligned(self):
        from app.research import indicators as I
        df = synthetic_ohlcv()
        f = I.feature_frame(df, "1d", "crypto")
        self.assertEqual(len(f), len(df))
        self.assertTrue(f["rsi_14"].dropna().between(0, 100).all())
        self.assertTrue(f["adx_14"].dropna().between(0, 100).all())
        self.assertTrue((f["atr_14"].dropna() > 0).all())

    def test_donchian_has_no_lookahead(self):
        from app.research.indicators import donchian
        df = synthetic_ohlcv(200)
        d = donchian(df, 20)
        # the channel at bar t must not include bar t's own high
        spike = df.copy()
        spike.iloc[150, spike.columns.get_loc("high")] *= 3
        d2 = donchian(spike, 20)
        self.assertEqual(d["upper"].iloc[150], d2["upper"].iloc[150])
        self.assertGreater(d2["upper"].iloc[151], d["upper"].iloc[151])

    def test_regime_labels_are_valid(self):
        from app.research import indicators as I, regime as R
        f = I.feature_frame(synthetic_ohlcv(), "1d", "crypto")
        labels = set(R.classify(f).unique())
        self.assertTrue(labels <= {"trend", "range", "volatile", "quiet"})


@unittest.skipUnless(HAS_DEPS, "numpy/pandas not installed")
class BacktestTests(unittest.TestCase):
    def decide(self, symbol, df):
        from app.research import indicators as I, regime as R
        from app.research.strategies import EnsembleStrategy, MeanReversionStrategy, TrendBreakoutStrategy
        f = I.feature_frame(df, "1d", "crypto")
        d = EnsembleStrategy(strategies=[TrendBreakoutStrategy(), MeanReversionStrategy()]).decide(f, R.classify(f))
        d["atr"] = f["atr_14"]
        d["vol"] = f["vol_20"]
        return d

    def test_backtest_runs_and_pays_costs(self):
        from app.research.backtest import run_backtest
        from app.research.config import CostConfig
        data = {"AAA/USD": synthetic_ohlcv(seed=3, drift=0.002), "BBB/USD": synthetic_ohlcv(seed=4, drift=0.002)}
        cheap = run_backtest(data, self.decide, costs={"crypto": CostConfig(0, 0, 0)})
        dear = run_backtest(data, self.decide, costs={"crypto": CostConfig(120, 10, 5)})
        self.assertGreater(cheap.metrics["trades"], 0)
        # the EV gate rejects more entries when costs are higher, and every fill pays
        self.assertLessEqual(dear.metrics["trades"], cheap.metrics["trades"])
        self.assertEqual(0.0, cheap.metrics["total_costs"])
        self.assertGreater(dear.metrics["total_costs"], 0.0)
        self.assertIn("promotion_gate", cheap.metrics)

    def test_entries_execute_at_next_open_not_signal_close(self):
        from app.research.backtest import run_backtest
        from app.research.config import CostConfig
        df = synthetic_ohlcv(seed=5, drift=0.003)
        result = run_backtest({"AAA/USD": df}, self.decide, costs={"crypto": CostConfig(0, 0, 0)})
        for trade in result.trades:
            loc = df.index.get_loc(trade.entry_time)
            self.assertAlmostEqual(trade.entry_price, float(df["open"].iloc[loc]), places=6)

    def test_risk_manager_halts_after_max_drawdown(self):
        from app.research.config import RiskConfig
        from app.research.risk import RiskManager
        rm = RiskManager(RiskConfig(max_drawdown_halt_pct=0.10), 1000)
        rm.mark_equity(1000)
        rm.mark_equity(850)
        self.assertFalse(rm.can_open("X", 1, {})[0])

    def test_position_size_respects_risk_budget(self):
        from app.research.config import RiskConfig
        from app.research.risk import RiskManager
        rm = RiskManager(RiskConfig(risk_per_trade_pct=0.01, target_annual_vol=10.0, max_position_notional_pct=10.0, kelly_fraction_cap=1.0), 10_000)
        qty = rm.position_size(entry=100, stop=95, annual_vol=0.5, bar_index=0, confidence=0.9)
        self.assertLessEqual(qty * 5, 100 + 1e-9)  # stop-out loses at most 1% = $100


@unittest.skipUnless(HAS_DEPS, "numpy/pandas not installed")
class PortfolioAndReplayTests(unittest.TestCase):
    def test_tsmom_and_xsmom_report_metrics(self):
        from app.research.config import CostConfig
        from app.research.portfolio import tsmom, xsmom
        closes = pd.DataFrame({f"C{i}/USD": synthetic_ohlcv(seed=10 + i, drift=0.001)["close"] for i in range(4)})
        for res in (tsmom(closes, cost=CostConfig(26, 5, 3)), xsmom(closes, top_k=2, cost=CostConfig(26, 5, 3))):
            self.assertIn("sharpe", res)
            self.assertIn("annual_turnover", res)
            self.assertTrue(math.isfinite(res["cagr_pct"]))

    def test_portfolio_does_not_forward_fill_missing_prices(self):
        from app.research.config import CostConfig
        from app.research.portfolio import tsmom
        index = pd.date_range("2023-01-01", periods=260, freq="D", tz="UTC")
        closes = pd.DataFrame({
            "A": np.linspace(100, 200, len(index)),
            "B": [50.0] * 220 + [np.nan] * 40,
        }, index=index)
        result = tsmom(closes, lookback=90, rebalance=7,
                       cost=CostConfig(26, 5, 3), ma_filter=200)
        self.assertEqual(2, result["symbols"])
        self.assertEqual(260, result["observations"])
        self.assertIn("data_start", result)

    def test_crypto_replay_uses_production_exit_logic(self):
        from app.research.config import CostConfig
        from app.research.replay import replay_model31_crypto
        hourly = {f"C{i}/USD": synthetic_ohlcv(1200, seed=20 + i, drift=0.0004, vol=0.01) for i in range(3)}
        for df in hourly.values():
            df.index = pd.date_range("2025-01-01", periods=len(df), freq="h", tz="UTC")
        result = replay_model31_crypto(hourly, cost=CostConfig(120, 10, 5))
        summary = result.summary()
        self.assertGreaterEqual(summary["signals_seen"], 0)
        self.assertIn("promotion_gate", summary)
        for trade in result.trades:
            self.assertIn(trade.reason.split("_")[0], {"HARD", "TARGET", "TRAILING", "PROFIT", "POSITION", "FALLING", "MAX"})

    def test_forex_snapshot_matches_production_shape(self):
        from app.multi_asset import AssetPolicy, ForexEngine, MultiAssetRejected
        from app.research.replay import build_forex_snapshot
        h1 = synthetic_ohlcv(800, seed=30, drift=0.0, vol=0.002)
        h1.index = pd.date_range("2025-01-01", periods=len(h1), freq="h", tz="UTC")
        snap = build_forex_snapshot("EUR_USD", h1, 700, spread_bps=1.5)
        for key in ("horizon_agreement", "trend_strength", "atr_14", "stop_distance", "bryne_h1_candles", "five_streak_candles"):
            self.assertIn(key, snap)
        try:
            ForexEngine(AssetPolicy(minimum_score=0)).evaluate(snap)
        except MultiAssetRejected:
            pass  # a rejection is a valid outcome; a KeyError would not be


if __name__ == "__main__":
    unittest.main()
