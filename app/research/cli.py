"""
Command line entry point for offline research.

  python -m app.research.cli replay-crypto  [--fee 120] [--no-1h-gate] [--hold 72]
  python -m app.research.cli replay-forex   [--spread 1.5] [--pairs EUR/USD,GBP/USD]
  python -m app.research.cli backtest       --asset crypto|forex [--fee 26] [--short]
  python -m app.research.cli portfolio      [--fee 26]
  python -m app.research.cli data           --symbol BTC/USD [--timeframe 1d] [--bars 500]

All commands print JSON so results can be committed under docs/research/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import CostConfig, RiskConfig, StrategyConfig
from .data import load_history

CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "LINK/USD", "DOGE/USD", "AVAX/USD", "LTC/USD", "DOT/USD"]
FOREX = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF", "NZD/USD", "EUR/JPY", "GBP/JPY", "EUR/GBP"]


def _emit(payload) -> None:
    json.dump(payload, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")


def cmd_data(args) -> None:
    data = load_history([args.symbol], args.timeframe, args.bars, cache_dir=args.cache)
    df = data[args.symbol]
    _emit({"symbol": args.symbol, "bars": len(df), "first": df.index[0], "last": df.index[-1], "tail": df.tail(5).to_dict(orient="index")})


def cmd_replay_crypto(args) -> None:
    from .replay import replay_model31_crypto
    hourly = load_history(args.symbols.split(",") if args.symbols else CRYPTO[:8], "1h", args.bars, cache_dir=args.cache)
    result = replay_model31_crypto(hourly, cost=CostConfig(args.fee, args.spread, args.slippage),
                                   require_positive_1h=not args.no_1h_gate, max_hold_hours=args.hold)
    _emit(result.summary())


def cmd_replay_forex(args) -> None:
    from .replay import replay_forex
    pairs = args.pairs.split(",") if args.pairs else FOREX[:4]
    hourly = load_history(pairs, "1h", args.bars, cache_dir=args.cache)
    results = replay_forex(hourly, spread_bps=args.spread, max_hold_bars=args.hold, min_score=args.min_score)
    _emit({name: r.summary() for name, r in results.items()})


def cmd_backtest(args) -> None:
    from . import indicators as I, regime as R
    from .backtest import run_backtest
    from .strategies import EnsembleStrategy, MeanReversionStrategy, TrendBreakoutStrategy
    symbols = args.symbols.split(",") if args.symbols else (CRYPTO if args.asset == "crypto" else FOREX)
    data = load_history(symbols, args.timeframe, args.bars, cache_dir=args.cache)
    cfg = StrategyConfig()
    strategies = [TrendBreakoutStrategy(cfg), MeanReversionStrategy(cfg, require_trend_alignment=args.asset == "crypto")]

    def decide(symbol, df):
        f = I.feature_frame(df, args.timeframe, args.asset)
        d = EnsembleStrategy(cfg, strategies).decide(f, R.classify(f), allow_short=args.short)
        d["atr"] = f["atr_14"]
        d["vol"] = f["vol_20"]
        return d

    cost = CostConfig(args.fee, args.spread, args.slippage) if args.fee is not None else CostConfig.for_asset_class(args.asset)
    result = run_backtest(data, decide, starting_equity=args.equity, timeframe=args.timeframe, costs={args.asset: cost},
                          risk=RiskConfig(max_gross_leverage=2.0 if args.asset == "forex" else 1.0))
    payload = result.to_dict()
    payload.pop("trades", None)
    _emit(payload)


def cmd_portfolio(args) -> None:
    import pandas as pd
    from .portfolio import buy_and_hold, tsmom, xsmom
    data = load_history(CRYPTO, "1d", args.bars, cache_dir=args.cache)
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index().dropna(how="all")
    cost = CostConfig(args.fee, args.spread, args.slippage)
    _emit({"btc_buy_hold": buy_and_hold(closes, "BTC/USD", 365),
           "tsmom_90d_weekly": tsmom(closes, lookback=90, rebalance=7, cost=cost),
           "xsmom_28d_top3_weekly": xsmom(closes, lookback=28, top_k=3, rebalance=7, cost=cost)})


def main(argv=None) -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.research.cli")
    parser.add_argument("--cache", default="data/research_cache")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("data"); p.add_argument("--symbol", required=True); p.add_argument("--timeframe", default="1d"); p.add_argument("--bars", type=int, default=500); p.set_defaults(fn=cmd_data)
    p = sub.add_parser("replay-crypto"); p.add_argument("--symbols"); p.add_argument("--bars", type=int, default=8000); p.add_argument("--fee", type=float, default=120.0); p.add_argument("--spread", type=float, default=10.0); p.add_argument("--slippage", type=float, default=5.0); p.add_argument("--no-1h-gate", action="store_true"); p.add_argument("--hold", type=int, default=72); p.set_defaults(fn=cmd_replay_crypto)
    p = sub.add_parser("replay-forex"); p.add_argument("--pairs"); p.add_argument("--bars", type=int, default=12000); p.add_argument("--spread", type=float, default=1.5); p.add_argument("--hold", type=int, default=48); p.add_argument("--min-score", type=float, default=80.0); p.set_defaults(fn=cmd_replay_forex)
    p = sub.add_parser("backtest"); p.add_argument("--asset", choices=["crypto", "forex"], default="crypto"); p.add_argument("--symbols"); p.add_argument("--timeframe", default="1d"); p.add_argument("--bars", type=int, default=2000); p.add_argument("--fee", type=float); p.add_argument("--spread", type=float, default=5.0); p.add_argument("--slippage", type=float, default=3.0); p.add_argument("--short", action="store_true"); p.add_argument("--equity", type=float, default=10_000.0); p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("portfolio"); p.add_argument("--bars", type=int, default=2000); p.add_argument("--fee", type=float, default=26.0); p.add_argument("--spread", type=float, default=5.0); p.add_argument("--slippage", type=float, default=3.0); p.set_defaults(fn=cmd_portfolio)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
