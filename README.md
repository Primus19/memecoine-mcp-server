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

Some ChatGPT clients cache an older MCP tool list. Set `PREAUTHORIZED_AUTO_EXECUTION=true` to make the existing `issue_model_3_1_recommendation` tool invoke the exact same atomic fast path as `process_preauthorized_candidate`. This compatibility mode does not bypass any validation. With `LIVE_TRADING=false`, it performs a dry run only. `/health` reports schema version 3.2 and whether compatibility mode is enabled without exposing secrets.

The caller no longer supplies authoritative Coinbase execution facts. Before a
candidate is accepted, the server fetches the Coinbase product and order book
and overwrites `identity_verified`, `spot_available`, `spread_bps` and
`slippage_bps`. The frozen ticket includes timestamped `coinbase_evidence`.
Research still must supply fresh, auditable regime, momentum, market-cap,
volume, news, social, tokenomics and safety evidence; missing research evidence
is never converted into a passing score.

## Tests

```bash
python -m py_compile app/*.py tests/*.py
PYTHONPATH=. python -m unittest discover -s tests -v
```

References: [OpenAI MCP documentation](https://developers.openai.com/api/docs/mcp), [Coinbase Advanced Trade endpoints](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api), and [Coinbase Products API](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/list-products).
