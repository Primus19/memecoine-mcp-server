"""Candidate-strategy backtests on multi-year real history. Writes docs/research/strategy_backtest_results.json."""
import json, logging, time, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.basicConfig(level=logging.WARNING)
from app.research.data import load_history
from app.research import indicators as I, regime as R
from app.research.strategies import TrendBreakoutStrategy, MeanReversionStrategy, EnsembleStrategy, MLDirectionFilter, eligible_universe
from app.research.backtest import run_backtest, walk_forward_report
from app.research.config import StrategyConfig, RiskConfig, CostConfig

CACHE = "data/research_cache"
t = time.time()
crypto = load_history(["BTC/USD","ETH/USD","SOL/USD","XRP/USD","ADA/USD","LINK/USD","DOGE/USD","AVAX/USD","LTC/USD","DOT/USD"], "1d", 2000, cache_dir=CACHE)
fx = load_history(["EUR/USD","GBP/USD","USD/JPY","AUD/USD","USD/CAD","USD/CHF","NZD/USD","EUR/JPY","GBP/JPY","EUR/GBP"], "1d", 2500, cache_dir=CACHE)
results = {}

def make_decider(strats, cfg, asset_class, allow_short, ml=False):
    def decide(symbol, df):
        f = I.feature_frame(df, "1d", asset_class); reg = R.classify(f)
        probs = None
        if ml:
            filt = MLDirectionFilter(horizon=cfg.ml_horizon, cost_bps=CostConfig.for_asset_class(asset_class).round_trip_bps, min_train=cfg.ml_min_train)
            probs = filt.walk_forward_probabilities(f); decide.ml_reports[symbol] = filt.report.to_dict()
        d = EnsembleStrategy(cfg, strats).decide(f, reg, allow_short=allow_short, ml_probs=probs)
        d["atr"] = f["atr_14"]; d["vol"] = f["vol_20"]; return d
    decide.ml_reports = {}
    return decide

cfg = StrategyConfig()
KEYS = ("trades","cagr_pct","sharpe","max_drawdown_pct","win_rate","profit_factor","avg_trade_return_bps","expectancy_r")
runs = [
  ("crypto_trend_120bps", crypto, [TrendBreakoutStrategy(cfg)], "crypto", False, CostConfig(120,10,5), False, 1.0),
  ("crypto_trend_26bps", crypto, [TrendBreakoutStrategy(cfg)], "crypto", False, CostConfig(26,5,3), False, 1.0),
  ("crypto_meanrev_26bps", crypto, [MeanReversionStrategy(cfg)], "crypto", False, CostConfig(26,5,3), False, 1.0),
  ("crypto_ensemble_26bps", crypto, [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg)], "crypto", False, CostConfig(26,5,3), False, 1.0),
  ("crypto_ensemble_ml_26bps", crypto, [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg)], "crypto", False, CostConfig(26,5,3), True, 1.0),
  ("crypto_ensemble_120bps", crypto, [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg)], "crypto", False, CostConfig(120,10,5), False, 1.0),
  ("fx_trend_longshort", fx, [TrendBreakoutStrategy(cfg)], "forex", True, CostConfig(0,1.5,0.5), False, 2.0),
  ("fx_meanrev_longshort", fx, [MeanReversionStrategy(cfg, require_trend_alignment=False)], "forex", True, CostConfig(0,1.5,0.5), False, 2.0),
  ("fx_ensemble_longshort", fx, [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg, require_trend_alignment=False)], "forex", True, CostConfig(0,1.5,0.5), False, 2.0),
  ("fx_ensemble_ml_longshort", fx, [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg, require_trend_alignment=False)], "forex", True, CostConfig(0,1.5,0.5), True, 2.0),
]
for name, data, strats, ac, short, cost, ml, lev in runs:
    dec = make_decider(strats, cfg, ac, short, ml)
    risk = RiskConfig(max_gross_leverage=lev, max_open_positions=4 if ac == "crypto" else 5)
    res = run_backtest(data, dec, starting_equity=10000, timeframe="1d", costs={ac: cost}, risk=risk)
    results[name] = res.to_dict(); results[name]["ml_reports"] = dec.ml_reports
    print(name, json.dumps({k: res.metrics.get(k) for k in KEYS}), res.rejected, round(time.time() - t), flush=True)

elig = eligible_universe({s: df["close"] for s, df in crypto.items()}, top_k=4, lookback=30, skip=2)
dec = make_decider([TrendBreakoutStrategy(cfg)], cfg, "crypto", False)
res = run_backtest(crypto, dec, starting_equity=10000, timeframe="1d", costs={"crypto": CostConfig(26,5,3)}, risk=RiskConfig(), eligibility=elig)
results["crypto_trend_xsmom_26bps"] = res.to_dict()
print("crypto_trend_xsmom_26bps", json.dumps({k: res.metrics.get(k) for k in KEYS}), res.rejected, flush=True)

dec = make_decider([TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg)], cfg, "crypto", False)
results["walk_forward_crypto_ensemble_26bps"] = walk_forward_report(crypto, dec, train_bars=400, test_bars=250, starting_equity=10000, timeframe="1d", costs={"crypto": CostConfig(26,5,3)})
print("wf crypto", json.dumps(results["walk_forward_crypto_ensemble_26bps"]), flush=True)
dec = make_decider([TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg, require_trend_alignment=False)], cfg, "forex", True)
results["walk_forward_fx_ensemble"] = walk_forward_report(fx, dec, train_bars=500, test_bars=500, starting_equity=10000, timeframe="1d", costs={"forex": CostConfig(0,1.5,0.5)}, risk=RiskConfig(max_gross_leverage=2.0, max_open_positions=5))
print("wf fx", json.dumps(results["walk_forward_fx_ensemble"]), flush=True)
json.dump(results, open("docs/research/strategy_backtest_results.json", "w"), indent=1, default=str)
print("STRATEGY BACKTESTS COMPLETE", round(time.time() - t), flush=True)
