from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "services/solana-executor/index.mjs").read_text()


def test_live_executor_requires_exact_expected_wallet_identity():
    assert 'env("SOLANA_EXPECTED_WALLET_ADDRESS")' in SOURCE
    assert "walletIdentityVerified:walletMatches()" in SOURCE
    assert "signer wallet mismatch" in SOURCE
    assert "if(!walletMatches())throw Error" in SOURCE


def test_balance_preflight_is_mainnet_native_usdc_and_fail_closed():
    assert 'network:"mainnet-beta"' in SOURCE
    assert 'USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"' in SOURCE
    assert "payload?.error" in SOURCE
    assert "returned no result" in SOURCE
    assert "wallet balances stale" in SOURCE
    assert "wallet balance unavailable" in SOURCE


def test_paper_exploration_is_separate_from_live_qualification():
    assert "x.paper_qualified===true" in SOURCE
    assert "x.qualified===true" in SOURCE
    assert "SOLANA_PAPER_MAX_HOLD_MINUTES" in SOURCE
    assert "paper cost-stressed expectancy is not positive" in SOURCE


def test_discovery_failure_does_not_skip_wallet_supervision():
    discovery_catch = SOURCE.index('state.discoveryError=e.message.slice(0,500)')
    balance_check = SOURCE.index('if(wallet&&cfg.helius)try{await balances()}')
    assert discovery_catch < balance_check
