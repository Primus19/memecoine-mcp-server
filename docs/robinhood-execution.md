# Robinhood execution implementation and remaining activation gates

The existing paper worker remains paper-only. This implementation provides trusted-terminal provisioning, quote retrieval, explicitly approved transaction signing/submission, and transaction-receipt recovery. It does not autonomously buy discovered tokens or bypass the profitability gate.

## Credentials

Install `requirements-robinhood.txt` in the operator environment. Run `python -m app.robinhood_provision --keystore /secure/wallet.json --expected-address <public-address>` in a trusted interactive terminal. It prompts privately for the single account private key and a new password, verifies the derived address, and creates an encrypted owner-only keystore without overwriting existing files. Never put a key or recovery phrase into chat, GitHub, command-line arguments or logs. The assistant cannot provision authority from a public address.

Provision the unlock password separately as an owner-only file through the deployment's secret mechanism. This is a software keystore, not an HSM: anyone controlling the process and both files can sign. Do not use this with a personal savings wallet. No credential collection web endpoint has been created.

Operator settings: `ROBINHOOD_RPC_URL`, `ROBINHOOD_WALLET_ADDRESS`, `ROBINHOOD_KEYSTORE_PATH`, `ROBINHOOD_PASSWORD_PATH`, `ROBINHOOD_JOURNAL_PATH`, `ROBINHOOD_ALLOWED_TARGETS`. Keep the journal on persistent owner-only storage. `ROBINHOOD_OPERATOR_EXECUTION_ENABLED` defaults off. The app's existing Alchemy key is not a 0x API key.

## Quotes and approval

`app.robinhood_swap.quote` obtains a 0x v2 firm quote using a separately provisioned 0x API key. It rejects incomplete simulations, missing liquidity, identity/amount mismatches, insufficient balance and outstanding token allowances. It does NOT grant ERC20 approvals automatically. A trusted operator/policy must validate the target deployment, recipient, decoded calldata, exact token amount, minimum output, and all costs before approving a normalized EIP-1559 transaction. Provider metadata alone is not proof that calldata is safe.

Approval JSON contains `digest` (canonical transaction SHA-256 from `transaction_digest`), `expires_at` (Unix seconds), and `max_network_fee_wei`. It must originate from the trusted operator, not an untrusted API caller. Native value and calldata are bound by that exact digest. The CLI does not automatically create approvals or convert opaque quotes into approved transactions.

`python -m app.robinhood_operator execute --intent <unique-id> --transaction <approved-tx.json> --approval <approval.json>` performs fresh chain, code, simulation, gas, balance and nonce checks; saves signed bytes before broadcasting; and polls once for the receipt. Re-run `reconcile --intent <unique-id>` after restart or a timeout. It never blindly buys again after an unknown result. A crashed-before-broadcast or externally replaced transaction intentionally blocks progress pending operator investigation; there is no automatic replacement/cancellation policy.

Receipts require matching transaction/wallet identity and canonical block hash plus 12 confirmations. A successful receipt is not yet token-fill/P&L verification (`trade_fill_verified=false`); token balance/log accounting and ongoing exit supervision remain required before autonomous activation. Separate journal files must never control the same wallet. Finality/reorg monitoring beyond the confirmation rule is not implemented.

## Validation and deployment limits

Tests use disposable encrypted keys and mocked RPC/provider responses. They exercise signing/recovery, tamper and identity rejection, provisioning, configuration rejection, restart reconciliation, unknown submissions, reverts, and receipt mismatch. They do not prove a mainnet swap or authenticate the user's wallet. An actual testnet or bounded mainnet round trip has not been executed. Production activation requires owner-provisioned signing authority, provider credential/verified routes, native gas, trusted admission policy, token-fill accounting and end-to-end validation. Do not describe these modules as an activated live strategy.

Sources: https://docs.0x.org/docs/introduction/quickstart/swap-tokens-with-0x-swap-api and https://docs.0x.org/docs/introduction/supported-chains
