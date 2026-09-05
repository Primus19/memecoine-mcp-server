# Profitability review: Primus Unified Coinbase Pilot

## Integration status

The supplied branch was independently reviewed before integration. Its negative
findings for the hour-scale Coinbase and Forex rules are treated as veto evidence,
not as a prompt to tune those rules into production. The safe execution corrections
and offline replay harness are retained.

The positive multi-week result is now represented prospectively inside the existing
paper-only crypto service as `LIQUID_TSMOM_FORWARD_PAPER`: at least 200 completed
daily candles, price above the 200-day average, at least two positive 90/120/180/270
day momentum windows, at least 67% horizon agreement, two observations at least
twelve hours apart, executable order-book recovery, volatility-aware USD sizing,
three total portfolio slots and a 15% paper-budget exposure ceiling. It remains
separate in attribution from `EMERGING_FORWARD_PAPER_HOLD`; evidence on today's ten
liquid survivors is not evidence that an emerging token is safe or profitable.

The portfolio backtester was additionally hardened so missing prices are never
forward-filled into artificial zero returns. Its committed historical JSON remains
the supplied run and must not be represented as a fresh rerun of that correction.
Promotion still requires prospective positive cost-stressed evidence. No backtest or
code change guarantees future profit.

Branch: `claude/profitability-review`. Every number below was produced by code in this
branch (`app/research`) on real exchange history; nothing is estimated by hand. The JSON
behind each table is in `docs/research/`.

## 1. The short version

1. **The pilot is engineered for safety, not profit, and the two are currently in
   conflict.** Its risk controls (one position, $2.50 loss cap, drawdown breakers,
   hash-frozen tickets, 100-trade promotion gates) are better than most retail
   systems. But at $23.75 notional per trade and 120 bps per side, the round trip
   costs about 3.0% before spread; the expected edge of the entry rule is smaller than
   that. The system cannot compound its way to meaningful profit from here no matter how
   well it executes.
2. **The crypto entry rule (Model 3.1 momentum gate) has negative expectancy on
   history.** Replaying the mechanical part of the gate on 11 months of hourly candles
   for 8 large-cap Coinbase markets produced a 15% win rate and about -254 bps per
   trade at production fees, and it is still -57 bps at Kraken-level fees. The 1-hour
   momentum filter neither helps nor hurts. The problem is the timing rule and the
   exits, not only the fees.
3. **The forex paper sleeves lose on history, and daily trend-following in spot G10
   pairs has not worked since 2017.** Replaying the pilot's own H1 rules on two years
   of history gives 2,500 to 3,200 trades per trend sleeve with expectancy of -0.11 to
   -0.27 R and profit factors of 0.6 to 0.8; Bryne V5 is -0.04 R on 124 trades. Every
   lookback and rebalance combination of daily time-series momentum over 2017 to 2026
   is also negative (Sharpe -0.1 to -0.6). Details in section 4.
4. **One thing does hold up: multi-week time-series momentum on crypto as an
   allocation, not as a ticket.** A 120 to 270 day lookback, weekly rebalance,
   vol-targeted long-only book earned 17 to 28% a year with Sharpe 0.6 to 0.8 across
   the whole parameter neighbourhood at 26 bps fees (2021-10 to 2026-09), against 11%
   and a 77% drawdown for holding BTC. It does not beat BTC in bull years; it wins by
   sitting out 2022. At 120 bps fees the same rule keeps only 4 to 17%.
5. **The single largest controllable lever is execution cost.** Post-only maker
   entries (added, off by default), a lower-fee venue, or longer holding periods each
   move net expectancy by more than any signal change I tested.
6. **The second is a historical validation loop.** Until now every rule had to earn
   100 forward paper trades before anyone could judge it; at one position and
   hour-scale cooldowns that is years. `app/research` reduces that to minutes and is
   the tool this review was written with.

What I changed in code is in section 5. What I recommend doing next, in order, is in
section 6.

## 2. What exists today

### 2.1 `Primus19/memecoine-mcp-server` (this repository)

Services: the MCP/REST executor for Coinbase USDC spot (`server.py`), the research feed
that builds Model 3.1 candidates from CoinGecko plus an evidence adapter (`research_feed`,
`evidence_worker`), a signal worker that forwards fresh candidates, a position supervisor,
the OANDA market feed and hardened forex executor with three paper sleeves
(`market_feed`, `forex_executor`), the legacy multi-asset paper worker, Solana early
discovery and its executor, an economic calendar service, dashboards and email reporting.
355 tests passed on Python 3.12 before the supplied branch; the reviewed integration
passes 381 tests.

Strengths worth keeping exactly as they are: hash-frozen tickets with two-minute expiry;
independent Coinbase re-verification of spread, slippage and drift; attached bracket
protection with an emergency close if the bracket is missing; permitted capital that
deposits cannot expand; consecutive-loss and drawdown throttles; the promotion gate
(100 closes, positive 95% lower confidence bound, cost-stressed expectancy); currency
overlap and calendar vetoes on forex; single-writer ledgers with hash-chained events.

Weaknesses (all measured, see sections 3 and 4):

- `research_feed.score` and `risk.validate_ticket` require `1h > 0` and
  `0 < 24h <= 15%`; in crypto, hour-scale returns mean-revert and the rule buys after
  the move. The evidence, news and safety components are good universe filters but
  are being used as entry timing.
- `lifecycle.supervision_levels` moved the stop to the entry price once a trade was
  ~3.5% up and called it break-even. After 240 bps of fees plus spread and slippage
  that exit realizes roughly a -3% loss. Fixed in this branch.
- The 4% trailing stop, 2.5% trail after 8%, and the 1-hour momentum-reversal exit
  are all inside one day's noise for these assets, so most winners are cut before the
  2.5R target (see exit-reason counts in `docs/research/crypto_replay_results.json`).
- The forex sleeves (`FOREX_ENGINE` continuation/pullback, `TREND_CONTINUATION_V1`,
  `BRYNE_V5`) were designed from discretionary trading material and had never been run
  on history; section 4.2 shows all three are net losers on the last two years.
- `trend_continuation_signals` read a quote age of exactly 0.0 seconds as 999 seconds
  (`value or 999`) and rejected the snapshot as stale. Fixed in this branch.
- Coinbase Advanced retail taker fees are the highest of any venue in the design
  space; entries are limit orders but `post_only` was hard-coded `False`, so a marketable
  limit fills as taker.

### 2.2 `primus-cloud-solutions/ai-trading-pro`

This was reviewed first because the session opened there. It is a Flask SaaS shell
whose trading is entirely simulated: prices are a random walk, ADX and social scores
are `random.uniform`, the "real" broker adapters return "not yet implemented", and the
`/api/trading/algorithm-status`, `/api/advanced/performance` and `/api/advanced/backtest`
endpoints return hard-coded or random performance figures (94% success rate, $14,261
profit today, Sharpe 2.1 to 2.8). It also drops and recreates the database on every
start, ships an admin login of `admin123`, and lets any unauthenticated caller trade as
user 1. Nothing in it should be presented to customers as trading performance. No
changes were made there at your instruction; if it is going to front the pilot, its
trading endpoints should read the pilot's real ledgers.

## 3. Measured evidence: crypto

All runs use closed candles only, enter at the next candle's open, pay half-spread plus
slippage on each fill and the per-side fee on notional, and evaluate stops intra-bar.
Data: Coinbase Exchange public candles (daily 2021-03 to 2026-09; hourly 2025-10 to
2026-09) with Kraken as fallback. Costs: "production" = 120 bps/side fee, 10 bps spread,
5 bps slippage; "maker" = 60/10/5; "Kraken" = 26/5/3.

### 3.1 Replay of the production entry gate (Model 3.1 mechanical part)

`app.research.replay.replay_model31_crypto`: 8 markets (BTC, ETH, SOL, XRP, ADA, LINK,
DOGE, AVAX), hourly, one position at a time, entries when `1h > 0`, `0 < 24h <= 15%`,
`7d > 0`, breadth regime not FALLING; stop/target/trail from `research_feed.build_candidate`;
exits from `lifecycle.supervision_levels` every hour, plus a maximum hold.

| Variant | Signals | Trades | Win rate | Avg net bps | Expectancy (R) | Profit factor | 95% LCB bps |
|---|---|---|---|---|---|---|---|
| Production defaults (120 bps, 72h) | 7,153 | 98 | 15.3% | -254 | -0.39 | 0.11 | -304 |
| Maker entry (60 bps) | 7,153 | 98 | 25.5% | -134 | -0.26 | 0.29 | -183 |
| Kraken fees (26 bps) | 7,153 | 98 | 30.6% | -57 | -0.13 | 0.57 | -106 |
| No 1h gate, 120 bps | 12,821 | 100 | 16.0% | -254 | -0.39 | 0.11 | -303 |
| No 1h gate, 26 bps | 12,820 | 100 | 30.0% | -57 | -0.13 | 0.55 | -104 |
| 168h max hold, 26 bps | 7,135 | 86 | 29.1% | -56 | -0.13 | 0.59 | -111 |
| 24h max hold, 120 bps | 7,216 | 145 | 10.3% | -255 | -0.39 | 0.07 | -289 |
| Multiple positions, 26 bps | 1,096 | 1,096 | 29.7% | -64 | -0.14 | 0.58 | -82 |

Reading: the rule loses at every fee level and every holding period; the loss is
smaller with lower fees but the sign never flips, and with 1,096 independent trades the
upper confidence bound is still negative. Exit-reason counts show the trades are mostly
ended by the trailing/break-even/momentum-reversal logic, not by the 2.5R target.
Caveat: production also requires verified news, safety and identity evidence and scans
about 1,000 coins, which cannot be replayed; on the 8 most liquid markets, the timing
component alone has no edge.

### 3.2 Candidate per-trade rules (daily bars, 10 markets, 2021-03 to 2026-09)

`app.research.strategies` + `app.research.backtest`, 1% risk per trade, at most 4 open
positions, vol-targeted sizing, EV gate after costs, drawdown throttles.

| Rule | Fees | Trades | CAGR | Sharpe | Max DD | Win rate | PF | Avg bps/trade |
|---|---|---|---|---|---|---|---|---|
| Donchian-55 breakout + EMA/ADX filter, 2.5 ATR trail | 120 | 61 | -1.0% | -0.17 | -8.3% | 38% | 0.79 | +26 |
| same | 26 | 73 | -0.2% | -0.01 | -9.1% | 38% | 0.96 | +167 |
| Bollinger z-score + RSI(2) mean reversion, regime gated | 26 | 21 | -0.1% | -0.04 | -7.3% | 48% | 0.93 | -8 |
| Ensemble of the two | 26 | 94 | -0.3% | -0.04 | -9.3% | 40% | 0.94 | +128 |

| Ensemble + gradient-boosting veto (walk-forward) | 26 | 49 | +0.7% | 0.18 | -8.2% | 49% | 1.25 | +94 |
| Ensemble | 120 | 74 | -1.3% | -0.27 | -9.1% | 37% | 0.70 | -43 |
| Breakout restricted to top-4 cross-sectional momentum | 26 | 54 | -1.0% | -0.25 | -8.6% | 41% | 0.69 | +52 |

Reading: per-trade stop/target rules on daily crypto bars, even textbook ones with proper
exits, are roughly zero-sum after costs on this sample (positive average return per
trade, profit factor just under 1 because losers were sized larger). The risk layer did
its job: drawdowns stayed under 10%. This is not a strategy to scale; it is evidence
that ticket-style timing is the wrong shape for this asset class.

The machine-learning veto deserves a specific note because "AI models" are the usual
promise in this space. Its strictly out-of-sample direction accuracy was 49.4% against
a 53.3% base rate across the 10 markets (walk-forward, embargoed labels, cost-aware
targets). The improved profit factor in the table above comes from vetoing roughly half
the entries, not from predictive skill; with 49 trades it is not distinguishable from
luck. Walk-forward windows of the ensemble show the same instability: +794 and +258 bps
per trade in the 2023-09 to 2025-01 bull windows, -556 and -178 bps in the windows either
side. Nothing here supports selling an ML signal.

### 3.3 Allocation rules (weekly rebalance, 10 markets, 2021-10 to 2026-09)

`app.research.portfolio`, long-only, inverse-volatility sizing to 20% target vol, gross
exposure capped at 100%.

| Rule | Fees | CAGR | Sharpe | Max DD | Turnover/yr | 2022 | 2023 | 2024 | 2025 | 2026 YTD |
|---|---|---|---|---|---|---|---|---|---|---|
| Hold BTC | 0 | 11.2% | 0.46 | -76.7% | 0 | -64% | +156% | +121% | -6% | -7% |
| Equal-weight hold, 10 coins | 0 | -11.0% | 0.16 | -79.3% | 0 | | | | | |
| TSMOM 90d | 26 | -1.0% | 0.20 | -57.0% | 16.1 | -45% | +59% | +86% | -19% | -28% |
| TSMOM 90d + above 200d MA | 26 | 15.7% | 0.56 | -43.1% | 11.2 | -4.5% | +40% | +75% | -15% | +3% |
| TSMOM 120d | 26 | 28.4% | 0.78 | -46.9% | 10.2 | | | | | |
| TSMOM 180d | 26 | 16.9% | 0.57 | -51.8% | 11.0 | -22% | +82% | +67% | -26% | +23% |
| TSMOM 270d | 26 | 19.4% | 0.61 | -49.9% | 8.9 | | | | | |
| TSMOM 120d | 120 | 16.2% | 0.56 | | | | | | | |
| TSMOM 180d | 120 | 4.9% | 0.34 | -53.5% | 11.0 | | | | | |
| TSMOM 120d | 60 (maker) | 23.5% | 0.69 | | | | | | | |
| XSMOM 28d top-3 | 26 | 0.7% | 0.14 | -40.7% | 13.0 | | | | | |
| XSMOM 90d top-3 | 26 | 0.8% | 0.14 | -32.6% | 8.4 | | | | | |

Parameter sweep (26 bps): lookbacks 120, 180 and 270 days are positive at every
rebalance frequency (Sharpe 0.49 to 0.78); 30 to 90 days are not. That plateau, not
the single best cell, is the evidence. Start-date checks: from 2022-06 TSMOM-180 made
28.6% a year (BTC 26.5%); from 2023-01 it made 22.8% (BTC 54.0%). So the rule roughly
tracks BTC with about two thirds of the drawdown, and its whole advantage is the 2022
bear market. Expect it to lag in a straight-up year.

Cross-sectional momentum among only 10 large caps is flat; the published effect lives
in a wider universe with weekly rebalancing and is hurt badly by 120 bps fees.

## 4. Measured evidence: forex

Data: Yahoo Finance daily (2017-01 to 2026-09) and hourly (2024-09 to 2026-09) mid
prices for 10 G10 pairs, spread 1.5 bps, no commission, no financing. Financing is a
real cost for held positions and would make every long-horizon number below slightly
worse for the pair bought and better for the pair sold; it is not modelled.

### 4.1 Time-series momentum, long/short, vol-targeted (8% target, 2017 to 2026)

| Lookback | Rebalance | CAGR | Sharpe | Max DD |
|---|---|---|---|---|
| 20d | 5 | -1.7% | -0.38 | -18.1% |
| 60d | 5 | -1.4% | -0.29 | -19.3% |
| 120d | 5 | -2.4% | -0.53 | -24.5% |
| 250d | 5 | -0.8% | -0.16 | -11.1% |
| 250d | 20 | -0.9% | -0.18 | -10.8% |

All 18 cells of the sweep are negative. Cross-sectional (top-3 of 10) is -0.1% a year.
This matches the published record: trend-following in developed-market spot FX has been
roughly zero to negative since about 2012 while it kept working in rates, commodities
and equity indices. Daily-bar directional forex is not where the edge is.

Per-trade daily rules on the same 10 pairs, long/short, 2x gross leverage cap:

| Rule | Trades | CAGR | Sharpe | Max DD | Win rate | PF | Avg bps/trade |
|---|---|---|---|---|---|---|---|
| Donchian breakout + ATR trail | 185 | -1.1% | -0.51 | -11.6% | 24% | 0.53 | -42 |
| Bollinger/RSI(2) mean reversion | 345 | +0.3% | 0.12 | -5.0% | 49% | 1.06 | +2 |
| Ensemble | 493 | -1.1% | -0.49 | -11.1% | 40% | 0.78 | -13 |
| Ensemble + ML veto | 335 | -0.4% | -0.19 | -9.4% | 43% | 0.90 | -8 |

Short-horizon mean reversion is the only daily forex rule with a non-negative result,
and at +2 bps per trade it is inside the spread noise. The ML veto's out-of-sample
accuracy on forex was 52.4% against a 52.4% base rate: exactly nothing. Walk-forward
windows for the ensemble: +11 bps (2019-2020), -24 bps (2021-2022), -22 bps (2023-2024).

### 4.2 Replay of the pilot's own H1 forex rules

`app.research.replay.replay_forex` rebuilds `market_feed.forex_snapshot` from hourly
candles (H4 and D1 resampled from H1; M5 candles approximated by the last H1 bar, which
disables the five-streak logic but not the trend or Bryne logic), then runs
`ForexEngine.evaluate`, `trend_continuation_signals` and `bryne_liquidity_signals`
exactly as production does, closing trades with the paper supervisor's stop, target
and profit-protection floor. Calendar vetoes and session-liquidity flags are assumed
clear, which is optimistic.

Four pairs (EUR/USD, GBP/USD, USD/JPY, AUD/USD), 2024-09 to 2026-09, one open position
per sleeve, 48-bar maximum hold. Only AUD/USD (and USD/JPY for Bryne) produced signals
that passed the sleeves' own gates; EUR/USD and GBP/USD were rejected almost always by
the 75% multi-horizon agreement and 0.05 trend-strength floors, or by the Bryne range
definition.

| Sleeve | Spread | Trades | Win rate | Avg net bps | Expectancy (R) | Profit factor | 95% LCB bps | Avg bars | Exits (stop / target / profit floor) |
|---|---|---|---|---|---|---|---|---|---|
| FOREX_ENGINE (continuation / pullback) | 1.5 | 2,558 | 43.8% | -2.7 | -0.11 | 0.78 | -3.8 | 8.1 | 1,257 / 421 / 873 |
| FOREX_ENGINE | 3.0 | 2,598 | 41.8% | -4.1 | -0.18 | 0.69 | -5.2 | 7.8 | 1,341 / 408 / 842 |
| TREND_CONTINUATION_V1 | 1.5 | 3,242 | 42.1% | -2.9 | -0.14 | 0.75 | -3.7 | 6.5 | 1,662 / 784 / 793 |
| TREND_CONTINUATION_V1 | 3.0 | 3,254 | 37.4% | -5.0 | -0.27 | 0.61 | -5.8 | 6.4 | 1,842 / 720 / 689 |
| BRYNE_V5 (range sweep / order block) | 1.5 | 124 | 44.4% | -1.6 | -0.04 | 0.84 | -5.9 | 6.2 | 63 / 27 / 33 |
| BRYNE_V5 | 3.0 | 116 | 43.1% | -2.5 | -0.08 | 0.78 | -7.1 | 6.7 | 58 / 24 / 33 |

Reading: with 2,500 to 3,200 trades the confidence intervals are tight, and every
sleeve's upper bound is below zero at a 1.5 bps spread. The pattern is the same as in
crypto: roughly 45% of trades hit the 1.5 to 2R target but the profit-protection ratchet
(0.2R / 0.5R / 0.75R floors) converts a third of the rest into small scratches and the
stop takes the remainder, so the average trade lasts 6 to 8 hours and pays the spread
twice for a slightly negative result. Bryne V5 is the least bad and the rarest (about
one trade a week across four pairs); the trend sleeves trade every few hours on AUD/USD.
The replay understates costs (no financing, calendar and session vetoes assumed clear),
so live results should be expected to be somewhat worse than this table.

Consequence for the promotion gates: the paper ledgers will reach 100 closes for the
trend sleeves within weeks and, on this evidence, fail the positive lower-bound test;
Bryne V5 will take about two years to reach 100 closes and is also expected to fail.
Section 6 proposes what to test instead.

## 5. Code changes in this branch

Production (all covered by tests; `python -m pytest -q` = 374 passed):

1. `app/lifecycle.py`: the break-even stop is now `entry * (1 + fees + spread + slippage)`
   so a break-even exit realizes zero, not -3%. `LIFECYCLE_BREAK_EVEN_INCLUDES_COSTS=false`
   restores the old behaviour for comparison. The fee used honours a maker entry when
   post-only is enabled.
2. `app/policy.py`: `maker_fee_bps_per_side` (`LIVE_MAKER_FEE_BPS_PER_SIDE`, default 60),
   `entry_post_only` (`LIVE_ENTRY_POST_ONLY`, default false), `require_positive_1h`
   (`LIVE_REQUIRE_POSITIVE_1H`, default true), and `round_trip_fee_bps` /
   `entry_fee_bps_per_side` properties.
3. `app/exchange.py`: `buy_configuration` sets `post_only` from policy (or the ticket).
   With post-only on, an entry that would cross the book is rejected by Coinbase instead
   of filling as taker; the existing 45-second timeout cancels it.
4. `app/risk.py` and `app/research_feed.py`: the round-trip cost and expected-value
   checks use the maker/taker split; the 24h and 1h momentum gates are separate and the
   1h one is policy-controlled.
5. `app/quant.py`: `kelly_fraction`, `volatility_target_notional`,
   `stop_distance_quantity`, `max_drawdown`, `sharpe_ratio`, `expectancy_r` for sizing
   and reviews.
6. `.env.example`: the four new variables documented.

Research (optional dependencies in `requirements-research.txt`, not imported by any
service or Dockerfile):

- `app/research/data.py`: Kraken, Coinbase, Yahoo and Frankfurter history with disk
  cache; `load_history`.
- `app/research/indicators.py`, `regime.py`: Wilder RSI/ATR/ADX, MACD, Bollinger,
  Donchian (shifted, no look-ahead), realized vol, efficiency ratio, regime labels.
- `app/research/strategies.py`: trend breakout, mean reversion, cross-sectional
  momentum universe, gradient-boosting direction veto with embargoed walk-forward and
  out-of-sample reporting, regime-gated ensemble.
- `app/research/risk.py`, `backtest.py`: risk manager mirroring production limits;
  event-driven backtester; walk-forward report; production `promotion_gate` on results.
- `app/research/portfolio.py`: TSMOM and XSMOM allocation backtests with parameter sweeps.
- `app/research/replay.py`: the production rules on history (sections 3.1 and 4.2).
- `app/research/cli.py`: `python -m app.research.cli replay-crypto|replay-forex|backtest|portfolio|data`.
- `tests/test_research.py`: 10 offline tests on synthetic data (no network).
- `docs/research/*.json`: raw results.

## 6. Recommendations, in order

**P0. Cut execution cost before touching signals.**
Set `LIVE_ENTRY_POST_ONLY=true` and measure fill rate for two weeks; a 50% reduction
in entry fee is worth more than any rule change in section 3. In parallel, price the
same flow on a lower-fee venue (Kraken spot 0.16/0.26%, or Coinbase Advanced volume
tiers) and on longer holds. The pilot's minimum net edge (50 bps) is a rounding error
next to a 300 bps round trip.

**P1. Replace hour-scale timing with multi-week allocation for the crypto sleeve.**
Keep Model 3.1's identity, safety, liquidity, news and turnover checks as the
*universe*. Inside that universe, run TSMOM: hold a coin when its 120 to 270 day
return is positive and it is above its 200-day average, size inversely to 30-day
volatility to a 15 to 20% vol target, rebalance weekly, no leverage. Expect BTC-like
returns with roughly two thirds of the drawdown, and expect to lag in a pure bull run.
This is the only rule in this review whose evidence survives fees, start dates and
parameter neighbours. It needs the one-position limit lifted to 3 to 5 names and the
$25 pilot capital replaced by an amount where 26 to 60 bps of turnover cost is not
material (turnover is about 10x a year).

**P1. Put the hourly momentum entries behind the replay gate.**
Do not arm `LIVE_TRADING` for `MODEL_3_1_MOMENTUM` tickets until
`python -m app.research.cli replay-crypto` on the latest year shows a positive
cost-stressed lower confidence bound. Today it shows -104 to -304 bps.

**P2. Forex: stay paper, change the question.**
Daily and hourly directional rules on spot majors do not carry an edge in this
decade (section 4). The snapshot already contains OANDA financing rates: the tested
FX factor with the best 2010s to 2020s record is carry (long high-rate, short low-rate,
vol-scaled, with a drawdown-triggered de-risk), followed by short-horizon reversal
around session opens. Add both as paper sleeves through the same `replay_forex`
harness before spending forward time on them. Keep the calendar and spread vetoes.

**P3. Market-neutral crypto income is the most reliable "profit" in the space.**
Perpetual-futures funding capture and spot/futures basis (cash-and-carry) have paid
5 to 20% a year with equity-like drawdowns of a few percent, independent of direction.
They need a derivatives venue (Kraken Futures, Coinbase International, or an offshore
venue), which is outside the current Coinbase-spot mandate; the ledger, reconciliation
and gate code here would transfer directly.

**P3. Machine learning stays a veto, not a signal.**
The gradient-boosting filter reports its own out-of-sample accuracy against the base
rate (see `ml_reports` in `docs/research/strategy_backtest_results.json`). Use it to
block entries, never to size them, and retrain on a schedule with an embargo.

**Capital and expectations.**
With 26 bps fees and the P1 book, a defensible planning range is 10 to 25% a year with
40 to 50% peak-to-trough drawdowns in a crypto bear market; with 120 bps fees, roughly
a third of that. Nothing in this review supports the double-digit *monthly* returns
implied by the marketing copy in the other repository, and any pitch that does should
be removed.

## 7. Caveats

- Sample sizes: hourly crypto history is 11 months; daily crypto is 5.5 years including
  one full bear market; forex daily is 9.7 years. Results are period-dependent.
- Universe: the 10 crypto markets were chosen today, which flatters buy-and-hold and
  any long-only rule (survivorship). Multi-week TSMOM was checked from three start dates.
- Costs: Coinbase fee tiers, spread by market and time of day, and financing on forex
  are modelled as constants. Shorts in forex ignore borrow/financing entirely.
- Replay approximations: M5 candles are synthesised from H1 (five-streak logic is
  therefore inactive), calendar vetoes are assumed clear, and the news, social and
  safety score components of Model 3.1 are not replayable.
- Yahoo forex hourly data is mid-price with occasional gaps; results at the 1 to 3 bps
  level are inside its noise.

## 8. Reproducing

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-research.txt
.venv/bin/python -m pytest -q                      # 381 tests in the reviewed integration
.venv/bin/python -m app.research.cli replay-crypto --fee 120
.venv/bin/python -m app.research.cli replay-crypto --fee 26 --no-1h-gate
.venv/bin/python -m app.research.cli replay-forex --spread 1.5
.venv/bin/python -m app.research.cli portfolio --fee 26
.venv/bin/python -m app.research.cli backtest --asset crypto --fee 26
.venv/bin/python scripts/research/run_strategy_backtests.py
```
