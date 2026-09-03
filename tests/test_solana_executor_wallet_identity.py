from pathlib import Path


RAW_SOURCE = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()
# Retain readable text for message assertions and append a whitespace-normalized
# copy for structural assertions. The executor is intentionally formatted.
SOURCE = RAW_SOURCE + "\n" + "".join(RAW_SOURCE.split())


def test_live_executor_requires_exact_expected_wallet_identity():
    assert 'env("SOLANA_EXPECTED_WALLET_ADDRESS")' in SOURCE
    assert "walletIdentityVerified:walletMatches()" in SOURCE
    assert "signer wallet mismatch" in SOURCE
    assert "if(!walletMatches())" in SOURCE
    assert "throwError" in SOURCE


def test_balance_preflight_is_mainnet_native_usdc_and_fail_closed():
    assert 'network:"mainnet-beta"' in SOURCE
    assert 'USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"' in SOURCE
    assert "payload?.error" in SOURCE
    assert "returned no result" in SOURCE
    assert "wallet balances stale" in SOURCE
    assert "wallet balance unavailable" in SOURCE


def test_new_probe_fees_are_valued_and_reported_net_in_usd():
    assert "transactionFeeEvidence" in SOURCE
    assert "networkFeeSolUsd" in SOURCE
    assert "networkFeeUsd" in SOURCE
    assert "CAPTURED_ON_CHAIN_AND_VALUED_USD" in SOURCE
    assert "netPnlAfterNetworkFeesUsd" in SOURCE
    assert "netPnlAfterKnownNetworkFeesUsd" in SOURCE
    assert "backfillProbeFeeEvidence" in SOURCE
    assert "FEES_BACKFILL_PENDING" in SOURCE
    assert "feeEvidenceBackfilledAt" in SOURCE
    assert "lastMessageId" in SOURCE


def test_paper_exploration_is_separate_from_live_qualification():
    assert "x.paper_qualified===true" in SOURCE


def test_crypto_paper_strategies_have_one_aggregate_service():
    assert 'service: "UNIFIED_CRYPTO_PAPER"' in SOURCE
    assert 'version: "UNIFIED_CRYPTO_PAPER_V1"' in SOURCE
    assert "unifiedCryptoPaper: unifiedCryptoPaperPerformance()" in SOURCE
    assert "done.length >= 100 && stressed > 0" in SOURCE
    assert 'scope: "CURRENT_VERSION_ONLY"' in SOURCE
    assert ".filter(currentUnifiedCryptoFill)" in SOURCE
    assert ".filter(currentUnifiedCryptoHandoff)" in SOURCE
    assert "divineV2Performance:" not in SOURCE
    assert "x.qualified===true" in SOURCE
    assert "SOLANA_PAPER_MAX_HOLD_MINUTES" in SOURCE
    assert "live-strategy shadow cost-stressed expectancy is not positive" in SOURCE
    assert "liveShadowPositions" in SOURCE
    assert "liveShadowFills" in SOURCE
    assert "liveShadowPromotion" in SOURCE


def test_discovery_failure_does_not_skip_wallet_supervision():
    discovery_catch = SOURCE.index('state.discoveryError=e.message.slice(0,500)')
    balance_check = SOURCE.index('if(wallet&&cfg.helius)')
    assert discovery_catch < balance_check


def test_strategy_action_reporting_is_separate_and_trade_triggered():
    assert 'strategyStats("SOLANA_EARLY_CONTROL")' in SOURCE
    assert 'strategyStats("SOLANA_PUMPFUN_EV_EXPERIMENT")' in SOURCE
    assert 'Divine Strategy (Pump.fun EV)' in SOURCE
    assert 'strategyPerformance:strategyPerformance()' in SOURCE
    assert '"/report.json"' in SOURCE
    assert '"/report"' in SOURCE
    assert 'if(hasTradeEvent)state.email={...state.email,pendingTradeEvent:true' in SOURCE
    assert 'if(!state.email.pendingTradeEvent||emailBlockers().length)' in SOURCE
    assert 'returnfalse' in SOURCE
    assert 'pendingTradeEvent:false' in SOURCE
    assert 'mode:"TRADE_EVENTS_ONLY"' in SOURCE
    assert "NEW ACTION" in SOURCE
    assert "REASON FOR ENTRY / ACTION" in SOURCE
    assert "Reason for entry / action" in SOURCE
    assert "DIVINE_V2" in SOURCE
    assert "DIVINE_V3" in SOURCE
    assert "CONTROL_V2" in SOURCE
    assert "confirmCandidate" in SOURCE
    assert "postExitCounterfactuals" in SOURCE
    assert "FAILED_MOMENTUM_15M" in SOURCE
    assert "TRAILING_PROFIT" in SOURCE
    assert "Divine V2 forward sample" in SOURCE
    assert "res.end(reportV3())" in SOURCE
