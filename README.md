# Primus Coinbase Compounding MCP Pilot

A private remote MCP app for a dedicated Coinbase Advanced Trade portfolio. It starts from one immutable $5-$30 USDC baseline, may compound only reconciled net trading profit, and never treats later deposits as trading capital.

## MCP tools

| Tool | Purpose |
|---|---|
| `pilot_status` | Reconcile Coinbase state and report live position, P&L, capital and audit events |
| `preflight_coinbase` | Verify View + Trade, Transfer disabled, and initialize the immutable baseline |
| `list_eligible_spot_products` | Discover current tradable Coinbase USDC spot markets |
| `execute_validated_ticket` | Preview or submit one server-validated spot ticket |
| `emergency_pause` | Block every new submission |

## Risk and capital rules

- One open position maximum; spot only; no leverage, margin, derivatives, transfers, DEXs, presales or averaging down.
- The first successful preflight freezes the dedicated portfolio baseline between $5 and $30.
- Permitted capital is `baseline + reconciled cumulative realized P&L`. Additional deposits do not increase it.
- Default entry allocation is at most 95% of the lesser of available USDC and permitted capital, leaving a fee reserve.
- Maximum modeled loss remains the lesser of $2.50 and 10% of permitted capital.
- The fixed symbol allowlist is replaced by live Coinbase verification. Only currently tradable, USDC-quoted Coinbase spot products can pass.
- Research gates remain: score ≥85, verified news ≥4, RISING regime, positive 1h/24h, ≤15% daily extension, ≥$50M market cap, ≥$10M volume, 5%-100% turnover, ≤50 bps spread/slippage, verified identity and no veto.
- Coinbase product status and identity are rechecked server-side immediately before preview and submission.
- Every accepted entry includes a stop and profit target. Duplicate and expired tickets are rejected.

This software cannot guarantee profit. The pilot balance can be lost, stops can gap, and fees can dominate small positions.

## Security

- Use a dedicated portfolio-scoped CDP key with **View + Trade only** and **Transfer disabled**.
- Enter credentials only through `/setup?token=...`; they are encrypted at rest and never returned through MCP.
- Rotate `SETUP_TOKEN` after setup.
- `/mcp` uses GitHub OAuth.
- Every `/api/*` REST route requires `Authorization: Bearer $REST_API_TOKEN`. If `REST_API_TOKEN` is absent, REST access remains denied.
- Keep `LIVE_TRADING=false` until preflight, dynamic-product discovery, dry-run preview, reconciliation and email delivery have all been tested.

ChatGPT currently requires confirmation for MCP write actions. This server does not bypass that safeguard.

## Email trade and P&L reporting

Configure SMTP in the host secret manager. The server emails on order submission, first detected fill, position close with gain/loss, and emergency pause. Email failure is recorded in the append-only audit log and never weakens risk validation.

For Gmail, use an app password or a dedicated SMTP provider credential; never use the normal account password.

## Railway variables

```text
PUBLIC_BASE_URL=https://YOUR-SERVICE.up.railway.app
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
JWT_SIGNING_KEY=...
SETUP_TOKEN=...
CREDENTIAL_ENCRYPTION_KEY=...
REST_API_TOKEN=...
LIVE_TRADING=false
LIVE_CONFIRMATION=
MAX_CAPITAL_ALLOCATION_PCT=95
TRADE_REPORT_EMAIL=primus.vekuh@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=...
DATA_DIR=/app/data
```

The server is armed only when both values match exactly:

```text
LIVE_TRADING=true
LIVE_CONFIRMATION=I_ACCEPT_THE_25_USDC_LIVE_RISK
```

## Safe rollout

1. Deploy with `LIVE_TRADING=false`.
2. Save the portfolio-scoped Coinbase key and rotate the setup token.
3. Call `preflight_coinbase`; confirm the immutable baseline and Transfer disabled.
4. Call `list_eligible_spot_products` and `pilot_status`.
5. Submit a valid ticket while locked; verify `DRY_RUN_ONLY`.
6. Confirm trade-report email configuration.
7. Arm only after the above checks pass.
8. Review and confirm each write action in ChatGPT.

## Tests

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
python -m py_compile app/*.py tests/*.py
docker build -t primus-coinbase-mcp .
```

References: [OpenAI MCP server guide](https://developers.openai.com/api/docs/mcp), [Coinbase Advanced Trade API](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api), and [Coinbase Products API](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/list-products).
