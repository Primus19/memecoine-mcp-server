# Primus Unified Coinbase Pilot

This MCP server is the authoritative decision and execution ledger for the Coinbase pilot. It does not guarantee profit.

## One report-to-execution chain

1. Market research supplies normalized evidence to `issue_model_3_1_recommendation`.
2. The server calculates the Model 3.1 total from bounded component scores, applies live Coinbase and risk checks, freezes the complete recommendation, and returns a ticket ID plus SHA-256 hash.
3. The hourly report must display that exact stored record.
4. `execute_issued_ticket` accepts only the stored ticket ID and matching hash. It rejects edited, expired, unknown, previously used, or non-qualifying tickets.
5. Coinbase product state, cash and risk gates are checked again immediately before preview and submission.

The reporting agent must never invent a live ticket outside this flow.

## Tools

| Tool | Purpose |
|---|---|
| `preflight_coinbase` | Verify portfolio permissions and synchronize capital |
| `list_eligible_spot_products` | Discover current Coinbase USDC spot markets |
| `issue_model_3_1_recommendation` | Score and freeze the exact report/execution record |
| `pilot_status` | Reconcile fills and return the authoritative hourly snapshot, P&L, reviews and notification events |
| `execute_issued_ticket` | Preview or execute only an issued ticket ID/hash |
| `emergency_pause` | Block new entries |
| `emergency_flatten` | Cancel the tracked entry and market-sell the tracked asset after explicit confirmation |
| `resume_trading` | Resume only with the required explicit acknowledgement |

## Capital and P&L

- The first valid $5-$30 USDC balance is recorded as the initial baseline.
- Later deposits and withdrawals are recorded separately from trading P&L and become part of permitted capital.
- Realized and unrealized P&L are calculated from Coinbase fills, quantities, prices and commissions—not from balance changes.
- Up to 95% of available permitted capital may be allocated by default, leaving a fee reserve.
- One open position maximum; USDC spot only; no leverage, derivatives, transfers, DEXs, presales or averaging down.

## Circuit breakers

New entries pause automatically after any of:

- three consecutive losing trades;
- 10% daily drawdown;
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

Required variables are documented in `.env.example`. Keep live execution locked while refreshing and testing the new MCP schema:

```text
LIVE_TRADING=false
LIVE_CONFIRMATION=
```

Arm only after preflight, product discovery, recommendation issuance, dry-run ticket execution, fill reconciliation, circuit-breaker and Gmail-report tests pass:

```text
LIVE_TRADING=true
LIVE_CONFIRMATION=I_ACCEPT_THE_25_USDC_LIVE_RISK
```

ChatGPT currently requires manual confirmation for MCP write actions; this server does not bypass that protection.

## Tests

```bash
python -m py_compile app/*.py tests/*.py
PYTHONPATH=. python -m unittest discover -s tests -v
```

References: [OpenAI MCP documentation](https://developers.openai.com/api/docs/mcp), [Coinbase Advanced Trade endpoints](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api), and [Coinbase Products API](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/list-products).
