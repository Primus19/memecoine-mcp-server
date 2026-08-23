# Primus Unified Coinbase Pilot

This MCP server is the authoritative decision and execution ledger for the Coinbase pilot. It does not guarantee profit.

## One report-to-execution chain

1. Market research supplies normalized evidence to `issue_model_3_1_recommendation`.
2. The server calculates the Model 3.1 total from bounded component scores, applies live Coinbase and risk checks, freezes the complete recommendation, and returns a ticket ID plus SHA-256 hash.
3. The hourly report must display that exact stored record.
4. `process_preauthorized_candidate` atomically freezes and processes the same candidate without waiting for an email response. `execute_issued_ticket` remains available for dry-run and diagnostic use.
5. Coinbase product state, cash, live order-book spread, modeled slippage, source age and entry drift are checked again immediately before preview and submission.
6. Entries expire within two minutes. A submitted entry that does not fill within the configured timeout is cancelled instead of being chased. An attached bracket carries the recorded stop and target.

The reporting agent must never invent a live ticket outside this flow.

## Tools

| Tool | Purpose |
|---|---|
| `preflight_coinbase` | Verify portfolio permissions and synchronize capital |
| `list_eligible_spot_products` | Discover current Coinbase USDC spot markets |
| `issue_model_3_1_recommendation` | Score and freeze the exact report/execution record |
| `pilot_status` | Reconcile fills and return the authoritative hourly snapshot, P&L, reviews and notification events |
| `execute_issued_ticket` | Preview or execute only an issued ticket ID/hash |
| `process_preauthorized_candidate` | Atomically freeze, revalidate, preview and process a pre-authorized candidate |
| `emergency_pause` | Block new entries |
| `emergency_flatten` | Cancel the tracked entry and market-sell the tracked asset after explicit confirmation |
| `resume_trading` | Resume only with the required explicit acknowledgement |

## Capital and P&L

- The first valid $5-$30 USDC balance is recorded as the initial baseline.
- Later deposits and withdrawals are recorded separately from trading P&L and never expand permitted capital automatically.
- The original baseline plus realized P&L is the only compounding capital. Adding cash to Coinbase does not authorize the executor to use it.
- Realized and unrealized P&L are calculated from Coinbase fills, quantities, prices and commissions—not from balance changes.
- Up to 95% of available permitted capital may be allocated by default, leaving a fee reserve.
- One open position maximum; USDC spot only; no leverage, derivatives, transfers, DEXs, presales or averaging down.

## Circuit breakers

New entries pause automatically after any of:

- two consecutive losing trades;
- 15% daily drawdown;
- 25% drawdown from peak equity.

`emergency_pause` blocks new entries. `emergency_flatten` is a separate destructive action because it can realize a loss. Resuming requires the exact acknowledgement `I_REVIEWED_THE_LOSSES_AND_ACCEPT_RESUMING`.

## Guarded model review

Every hourly `pilot_status` snapshot creates at most one review record for that UTC hour. Every closed trade creates a trade-specific review. Reviews track sample size, wins/losses, win rate, net expectancy and profit factor.

Model 3.1 remains locked until at least 30 closed outcomes exist. The server does not rewrite weights after one win or loss and never loosens safety controls automatically.

## Email reporting without SMTP

No SMTP server or app password is required. `pilot_status` returns an append-only notification/event feed plus the exact recommendations and review results. The existing ChatGPT scheduled task uses its connected Gmail account to format and email that snapshot.

This produces hourly reporting. Immediate server-originated email would require a separate mail provider, which is intentionally not required here.

## Security and deployment

- `/mcp` uses GitHub OAuth.
- `/api/pilot-status` requires `Authorization: Bearer $REST_API_TOKEN`.
- Coinbase credentials are entered only through `/setup?token=...`, encrypted at rest, and never returned through MCP.
- The Coinbase key must be portfolio-scoped with View + Trade and Transfer disabled.
- Rotate `SETUP_TOKEN` after setup.
- Keep a persistent Railway volume mounted at `/app/data`.

Required variables are documented in `.env.example`.

### OAuth deployment checklist

Use these exact values for the deployed Railway service:

```text
PUBLIC_BASE_URL=https://memecoin-mcp-server-production.up.railway.app
```

Do not append `/mcp` to `PUBLIC_BASE_URL`. In the GitHub OAuth App, set the authorization callback URL exactly to:

```text
https://memecoin-mcp-server-production.up.railway.app/auth/callback
```

Set `JWT_SIGNING_KEY` in Railway to a stable, randomly generated secret of at least 32 bytes. Do not paste that secret into ChatGPT, commit it, or rotate it casually: FastMCP uses it to sign OAuth proxy tokens. Keep the Railway volume mounted at `/app/data` so dynamically registered ChatGPT clients survive deploys and restarts.

After deployment, `/health` reports only non-secret OAuth diagnostics: the canonical base URL, exact callback URL, FastMCP version, signing-key presence, and whether persistent client storage is under `/app/data`.

Keep live execution locked while refreshing and testing the new MCP schema:

```text
LIVE_TRADING=false
LIVE_CONFIRMATION=
```

Arm only after preflight, product discovery, recommendation issuance, dry-run ticket execution, fill reconciliation, circuit-breaker and Gmail-report tests pass:

```text
LIVE_TRADING=true
LIVE_CONFIRMATION=I_ACCEPT_THE_25_USDC_LIVE_RISK
```

For unattended operation, a trusted signal worker may call `POST /api/auto-candidate` with `Authorization: Bearer $REST_API_TOKEN`. That endpoint invokes the exact same freeze, risk, preview and execution code as the MCP tool. Keep the bearer token only in the worker's secret manager. MCP write actions may still require ChatGPT confirmation and are not the low-latency path.

## Pre-authorized fast path

The fast path is disabled unless both live environment variables are armed. It does not weaken the Model 3.1 gates. Each candidate must be based on market data no more than two minutes old, and execution independently requires a live spread and modeled slippage of at most 50 bps, entry drift of at most 35 bps, a best ask at or below the frozen entry ceiling, one-position maximum, and the existing $2.50 loss cap. The order is previewed before submission and carries an attached stop/target bracket.

Some ChatGPT clients cache an older MCP tool list. Set `PREAUTHORIZED_AUTO_EXECUTION=true` to make the existing `issue_model_3_1_recommendation` tool invoke the exact same atomic fast path as `process_preauthorized_candidate`. This compatibility mode does not bypass any validation. With `LIVE_TRADING=false`, it performs a dry run only. `/health` reports schema version 3.3, the frozen opportunity-policy defaults, and whether compatibility mode is enabled without exposing secrets.

The caller no longer supplies authoritative Coinbase execution facts. Before a
candidate is accepted, the server fetches the Coinbase product and order book
and overwrites `identity_verified`, `spot_available`, `spread_bps` and
`slippage_bps`. The frozen ticket includes timestamped `coinbase_evidence`.
Research still must supply fresh, auditable regime, momentum, market-cap,
volume, news, social, tokenomics and safety evidence; missing research evidence
is never converted into a passing score.

## Opportunity policy 1.0

Model 3.1 component weights remain unchanged. A separately versioned,
human-directed opportunity policy controls whether a clean research candidate
may advance to the executor. The exact policy is frozen into every ticket and
therefore covered by the recommendation hash.

The default policy now:

- permits a strong candidate in a MIXED regime while continuing to reject a
  FALLING regime;
- uses a 78-point minimum;
- awards zero news points when no catalyst is verified instead of suppressing
  an otherwise clean established asset;
- keeps any verified negative-news veto hard;
- uses $25M market cap, $5M volume and 3%-150% turnover opportunity floors;
- keeps the 15% daily momentum chase ceiling.

This does **not** relax exact identity, clean contract safety, Coinbase spot
availability, USDC-only execution, live spread/slippage, source freshness,
entry drift, one-position maximum, capital limits, the $2.50 loss cap, attached
exit protection, or circuit breakers. Set `LIVE_REQUIRE_NEWS_CATALYST=true` to
restore the event-driven catalyst requirement without changing code.

## Paper-only multi-asset engines

`python -m app.asset_worker` consumes normalized, source-linked snapshots and
runs three independent paper sleeves:

- `FOREX_TREND`: aligned 1h/24h trend, liquid session, spread and economic-event
  distance checks;
- `EQUITY_MOMENTUM`: positive 1h/24h momentum, relative volume, VWAP, halt and
  broad-market checks;
- defined-risk options: call-debit and put-debit spreads only, with verified
  legs, minimum open interest, IV context, expiry and whole-contract sizing
  from the verified maximum loss per complete spread (including multiplier and
  estimated fees).

The worker writes an append-only JSONL ledger. It has no broker adapter and
cannot submit a live forex, equity or option order in this release. This is
intentional: each sleeve must first collect forward paper results and later use
a broker-specific preflight, preview, idempotency and reconciliation adapter.

Enable paper sleeves independently:

```text
MULTI_ASSET_WORKER_ENABLED=true
MULTI_ASSET_FEED_URL=https://your-normalized-feed.example/snapshots
FOREX_ENGINE_ENABLED=true
EQUITY_ENGINE_ENABLED=true
OPTION_ENGINE_ENABLED=true
MULTI_ASSET_LEDGER_PATH=/app/data/multi_asset.jsonl
```

The feed returns `{"snapshots": [...]}`. Every snapshot must contain a fresh
`observed_at`, at least one HTTPS source, `tradable=true`, a reference price,
spread, stop distance, maximum loss, thesis and invalidation. Asset-specific
fields are validated before a proposal can become a paper fill.

### Deployable multi-asset services

The broker-backed paper release adds three independently deployable processes:

- `python -m app.market_feed` obtains OANDA pricing and candles and emits
  normalized Forex snapshots;
- `python -m app.asset_worker` creates append-only paper proposals and fills;
- `python -m app.asset_supervisor` marks paper positions and appends stop/target
  closes without rewriting history.

`app.broker_adapters` provides read-only preflight and order-preview structures
for OANDA and Alpaca. No method in this release submits a Forex, equity, or
option order. `MULTI_ASSET_LIVE_ENABLED` defaults to false. A separately
reviewed execution adapter must add idempotent submission, reconciliation,
attached protection, and emergency exits before live use.

Required Forex feed variables:

```text
MULTI_ASSET_FEED_ENABLED=true
OANDA_ENVIRONMENT=practice
OANDA_API_TOKEN=<Railway secret>
OANDA_ACCOUNT_ID=<Railway secret>
FOREX_SYMBOLS=EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CAD
FOREX_DEFAULT_EVENT_DISTANCE_MINUTES=0
MULTI_ASSET_FEED_INTERVAL_SECONDS=60
```

`FOREX_DEFAULT_EVENT_DISTANCE_MINUTES=0` intentionally vetoes entries until a
trusted economic-calendar adapter attests that the event window is clear.
Paper and supervisor services must mount the same Railway volume at `/app/data`
so they share `multi_asset.jsonl`.

## Authoritative Forex executor

`python -m app.forex_executor` is the hardened single-writer Forex lifecycle
service. It stores intents, broker identifiers and a hash-chained audit stream
in SQLite on a Railway volume, independently rechecks OANDA pricing and
home-currency risk, suppresses duplicate intents, attaches stop-loss and
take-profit protection to an accepted OANDA order, and reconciles broker state.

It is paper-only unless **all** independent live gates are present. Never reuse
the practice deployment or database for a production account.

```text
FOREX_EXECUTOR_ENABLED=true
FOREX_EXECUTOR_INTERVAL_SECONDS=30
FOREX_LEDGER_PATH=/app/data/forex.sqlite3
MULTI_ASSET_FEED_URL=https://multi-asset-market-feed-production.up.railway.app/snapshots
FOREX_MAX_RISK_USD=2.50
FOREX_MAX_OPEN_POSITIONS=1
FOREX_DAILY_LOSS_LIMIT_USD=2.50
```

Live mode additionally requires all four values below plus a current verified
economic-calendar response. Missing or inconsistent values fail closed:

```text
OANDA_ENVIRONMENT=live
FOREX_LIVE_ENABLED=true
FOREX_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK
FOREX_ALLOWED_ACCOUNT_ID=<exact live v20 account id>
FOREX_LIVE_BASELINE_USD=<broker-confirmed starting balance>
```

The market feed accepts a normalized calendar endpoint through
`ECONOMIC_CALENDAR_URL` and optional `ECONOMIC_CALENDAR_BEARER_TOKEN`. Its
response must contain a fresh `observed_at`, direct HTTPS `source_url`, and
currency-tagged high-impact events. The fixed
`FOREX_DEFAULT_EVENT_DISTANCE_MINUTES` value is never accepted as verified
calendar evidence for live orders.

Before live promotion, exercise the identical broker path against OANDA
practice with virtual funds:

```text
OANDA_ENVIRONMENT=practice
FOREX_PRACTICE_EXECUTION_ENABLED=true
FOREX_PRACTICE_ACK=I_ACCEPT_PRACTICE_ORDER_EXECUTION
```

## Continuous signal worker

Run `python -m app.signal_worker` as a separate Railway service. It polls a
trusted research feed every 5-60 seconds (15 seconds by default), discards data
older than the configured freshness window, deduplicates candidates, and sends
only fresh complete payloads to the authenticated `/api/auto-candidate` route.
It never creates scores, fills missing news/safety evidence, or holds Coinbase
credentials. The executor performs the final Coinbase and portfolio checks.

Required worker variables:

```text
SIGNAL_WORKER_ENABLED=true
SIGNAL_SCAN_INTERVAL_SECONDS=15
SIGNAL_MAX_AGE_SECONDS=90
SIGNAL_FEED_URL=https://your-trusted-research-feed.example/candidates
SIGNAL_FEED_BEARER_TOKEN=<feed-specific secret, if required>
EXECUTOR_BASE_URL=https://memecoin-mcp-server-production.up.railway.app
REST_API_TOKEN=<same executor bearer token>
```

Keep `LIVE_TRADING=false` on the executor while testing. A healthy worker writes
`/app/data/signal_worker_status.json` and emits structured `SIGNAL_SCAN` logs.
Once a dry-run candidate returns `DRY_RUN_ONLY`, live mode can be armed on the
executor without changing the worker. The worker is a low-latency transport,
not a substitute for a genuine always-on research feed. If the feed is missing,
stale, malformed, or unavailable, it fails closed and forwards nothing.

## Continuous position supervisor

Run `python -m app.position_worker` as a separate Railway background service.
It polls the authoritative executor every 5-60 seconds, reconciles Coinbase
fills, records the +15% milestone, ratchets an 8% trailing exit after +12%, and
submits a managed exit on a FALLING regime. The Coinbase-attached hard stop and
+30% target remain live at the venue if the supervisor or Railway is offline.
Before a managed exit, the executor cancels open sell orders and uses only the
then-available base balance, preventing an intentional oversell.

Required supervisor variables:

```text
POSITION_SUPERVISOR_ENABLED=true
POSITION_SUPERVISION_INTERVAL_SECONDS=15
EXECUTOR_BASE_URL=https://memecoin-mcp-server-production.up.railway.app
REST_API_TOKEN=<same executor bearer token>
RESEARCH_FEED_URL=https://memecoin-research-feed-production.up.railway.app
SIGNAL_FEED_BEARER_TOKEN=<same research-feed bearer token>
```

Use start command `python -m app.position_worker`. It exposes `/health` for
Railway but does not need a public domain. Every entry fill, target milestone,
managed exit, close, error, pause and model review is written to the executor's
append-only event feed returned by `pilot_status`. The connected ChatGPT Gmail
task must use `pilot_status` as the live-pilot source of truth in every report.

## Continuous research-feed producer

Run `python -m app.research_feed` as a third Railway service from this same
repository. It continuously scans up to 750 CoinGecko meme-category markets,
intersects unique symbols with the executor's actual eligible Coinbase USDC
products, classifies liquid breadth, calculates the bounded Model 3.1
components, and publishes fresh candidates at `GET /candidates`.

The feed deliberately separates automatically observed market facts from
verified qualitative evidence. A candidate is not published until an
authenticated evidence adapter has submitted fresh identity, safety and news
attestations to `POST /evidence`. Missing evidence is zero—not neutral—and the
scanner fails closed. This prevents an article snippet, duplicate symbol, or
unsupported safety claim from becoming a real order.

Research-feed variables:

```text
RESEARCH_FEED_ENABLED=true
RESEARCH_SCAN_INTERVAL_SECONDS=30
RESEARCH_MARKET_PAGES=2
EXECUTOR_BASE_URL=https://memecoin-mcp-server-production.up.railway.app
REST_API_TOKEN=<same executor bearer token>
SIGNAL_FEED_BEARER_TOKEN=<new independent random bearer token>
COINGECKO_API_KEY=<optional CoinGecko demo/pro key>
RESEARCH_FEED_STATE_PATH=/app/data/research_feed.json
PORT=8080
```

Give the research service a Railway volume at `/app/data` and set its start
command to `python -m app.research_feed`. Railway creates its public domain; the
worker's `SIGNAL_FEED_URL` is that domain plus `/candidates`, and the worker's
`SIGNAL_FEED_BEARER_TOKEN` must equal the research service's token.

Generate that shared token yourself in a password manager or with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`; the service does
not print or expose secrets. `/health` is public and contains no evidence or
candidate payloads. `/candidates`, `/status`, and `/evidence` require the bearer
token.

Evidence submissions must include `coin_id`, `product_id`, `identity_verified`,
`no_safety_veto`, bounded `safety_score`, `news_score` and `social_score`, HTTPS
`source_urls`, `observed_at`, `expires_at`, `thesis`, and `invalidation`. This
endpoint is intended for a separately reviewed news/safety adapter; it is not a
way to assert passing values without source-backed verification.

End-to-end Railway topology:

1. Research service publishes authenticated `/candidates`.
2. Worker polls it and forwards each stable `signal_id` once.
3. Executor independently rechecks Coinbase identity, order book, capital,
   one-position limit, spread, slippage, drift, stop and loss cap.
4. Any missing/stale/conflicting fact stops the chain.

## Continuous evidence adapter

Run `python -m app.evidence_worker` as a fourth Railway service. It intersects
unique CoinGecko meme symbols with eligible Coinbase USDC products, verifies
CoinGecko contract identity, requires complete clean GoPlus EVM token-security
facts, and requires positive catalyst coverage from at least two independent
configured RSS domains. A two-source exploit, hack, rug, honeypot, delisting,
compromise, breach or chain-halt report is a veto. Missing contract fields,
unsupported chains, ambiguous symbols, missing holder data and single-source
news all fail closed. Social receives zero because the adapter has no reliable
public account-quality and bot-analysis source.

Required variables:

```text
EVIDENCE_ADAPTER_ENABLED=true
EVIDENCE_SCAN_INTERVAL_SECONDS=300
EVIDENCE_REQUEST_SPACING_SECONDS=2.5
EVIDENCE_HTTP_MAX_RETRIES=3
EXECUTOR_BASE_URL=https://memecoin-mcp-server-production.up.railway.app
REST_API_TOKEN=<reference the executor token>
RESEARCH_FEED_URL=https://memecoin-research-feed-production.up.railway.app
SIGNAL_FEED_BEARER_TOKEN=<reference the research-feed token>
COINGECKO_API_KEY=<optional demo/pro key>
EVIDENCE_NEWS_RSS_URLS=<optional comma-separated reviewed RSS URLs>
```

The adapter has no Coinbase credentials and cannot place orders. It only posts
bounded evidence to the authenticated research service. Keep
`LIVE_TRADING=false` until a genuine adapter-produced candidate completes the
full path and returns `DRY_RUN_ONLY`. Do not use fabricated evidence to force a
production test.

The adapter spaces requests per upstream host, honors HTTP `Retry-After`, uses
bounded retries, caches CoinGecko identity metadata for six hours, and isolates
per-token source failures. A rate-limited token is rejected for that cycle; it
is never filled with stale or invented evidence and does not abort the rest of
the scan.

## Tests

```bash
python -m py_compile app/*.py tests/*.py
PYTHONPATH=. python -m unittest discover -s tests -v
```

References: [OpenAI MCP documentation](https://developers.openai.com/api/docs/mcp), [Coinbase Advanced Trade endpoints](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api), and [Coinbase Products API](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/list-products).
