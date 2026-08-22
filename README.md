# Primus Coinbase $25 MCP Pilot

A private remote MCP app for connecting ChatGPT to a dedicated Coinbase Advanced Trade portfolio. It is intentionally locked by default and exposes only four tools:

| Tool | Purpose |
|---|---|
| `pilot_status` | Read-only redacted status and audit history |
| `preflight_coinbase` | Verify View + Trade, Transfer disabled, and pilot balance |
| `execute_validated_ticket` | Preview and optionally submit one server-validated spot ticket |
| `emergency_pause` | Block every new order submission |

Coinbase credentials are entered on the server's private `/setup` page, encrypted with Fernet, and never returned through MCP.

## Risk behavior

- One open position maximum.
- Spot limit orders only; no leverage, margin, derivatives, borrowing, DEXs, presales, or averaging down.
- Maximum order is the lesser of available equity and $25.
- Maximum modeled loss is the lesser of $2.50 and 10% of current equity.
- Score ≥85, verified news score ≥4, RISING regime, positive 1h/24h momentum, ≤15% daily extension, ≥$50M market cap, ≥$10M volume, 5%-100% turnover, ≤50 bps spread/slippage, verified identity, and no veto.
- Duplicate and expired tickets are rejected.
- Coinbase key must have View + Trade and **Transfer disabled**.
- The initial dedicated USDC balance must not exceed $30.
- Every submitted entry includes an attached Coinbase stop/target bracket where supported. If Coinbase preview rejects the product/order configuration, nothing is submitted.

The current version keeps each trade capped at $25. Profits remain in the dedicated portfolio but are not automatically added to the next trade yet. This avoids silently expanding risk before exchange-fill reconciliation and profit accounting are implemented and tested.

## Deploy

Deploy the folder as a private GitHub repository to Railway or another Docker host. Add a persistent volume at `/app/data` and these server-side variables:

| Variable | Description |
|---|---|
| `PUBLIC_BASE_URL` | HTTPS origin, such as `https://your-service.up.railway.app` |
| `GITHUB_CLIENT_ID` | GitHub OAuth app client ID used to sign into the MCP app |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth app secret |
| `JWT_SIGNING_KEY` | Long random signing secret for MCP OAuth tokens |
| `SETUP_TOKEN` | Random 32+ character one-time setup token |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key generated with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `PRODUCT_ALLOWLIST` | Explicit Coinbase USDC spot pairs |
| `LIVE_TRADING` | `false` during testing |
| `LIVE_CONFIRMATION` | Empty during testing |

Create the GitHub OAuth app using the callback URL shown by the deployed MCP authorization flow. Keep all variables in the host's encrypted secret manager.

Then open this URL yourself in a browser:

```text
https://YOUR-SERVICE/setup?token=YOUR_SETUP_TOKEN
```

Enter the Coinbase CDP key name and EC private key in that form. Do not paste them into ChatGPT. After a successful preflight, rotate or delete `SETUP_TOKEN`.

## Connect to ChatGPT

1. On ChatGPT web, open **Settings → Security and login → Developer mode**.
2. Open **Plugins**, press **+**, and create a developer-mode app.
3. Enter `https://YOUR-SERVICE/mcp` as the remote MCP URL.
4. Complete GitHub OAuth and review the four tools.
5. Keep `execute_validated_ticket` and `emergency_pause` visible as write actions.
6. Test `pilot_status`, then `preflight_coinbase`, then a valid ticket while locked.

The server is live only when both variables are set exactly:

```text
LIVE_TRADING=true
LIVE_CONFIRMATION=I_ACCEPT_THE_25_USDC_LIVE_RISK
```

Set either back to a nonmatching value to lock submissions. Revoking the Coinbase key is the exchange-side emergency stop.

## Scheduling limitation

The MCP environment makes the trading tool callable from ChatGPT. Whether a scheduled task may call a write tool without an interactive confirmation depends on ChatGPT's current app confirmation controls and your workspace settings. Do not assume that an hourly prompt can bypass required confirmation. The server always applies its own risk checks even when ChatGPT permits the call.

## Tests

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
docker build -t primus-coinbase-mcp .
```

## Important

This is speculative software and cannot guarantee profit. The entire $25 can be lost. Stops may execute below their triggers during gaps, orders may not fill, and fees may dominate small positions. Production use also requires fill reconciliation, monitoring alerts, and venue-specific integration tests against the exact Coinbase account and products.

References: [OpenAI MCP server guide](https://developers.openai.com/plugins/build/mcp-server), [ChatGPT Developer Mode](https://developers.openai.com/api/docs/guides/developer-mode), [MCP authentication](https://developers.openai.com/plugins/build/auth), and [ChatGPT MCP connection flow](https://developers.openai.com/plugins/build/app-quickstart).
