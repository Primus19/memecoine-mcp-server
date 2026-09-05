"""
Historical research and backtesting for the Primus pilot.

This package is deliberately NOT imported by any production service
(server, forex_executor, market_feed, workers). It needs numpy/pandas
(see requirements-research.txt) and is used offline to answer one question the
production system cannot answer on its own: *does a rule have positive
cost-stressed expectancy on real history* before we spend months of forward
paper evidence on it.

Modules
  data        real OHLCV history (Kraken, Coinbase, Yahoo, Frankfurter), no simulation
  indicators  textbook indicators (Wilder RSI/ATR/ADX, Donchian, z-score, ...)
  regime      trend / range / volatile classification
  strategies  candidate rules: trend breakout, mean reversion, cross-sectional momentum, ML veto
  backtest    event-driven backtester + walk-forward + metrics, same cost model as production
  replay      feeds history into the pilot's OWN signal functions (ForexEngine, trend
              continuation, Bryne V5, Model 3.1 momentum gate) so the live rules get a
              historical expectancy
  cli         python -m app.research.cli ...
"""
