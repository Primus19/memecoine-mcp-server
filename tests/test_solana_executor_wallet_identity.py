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
