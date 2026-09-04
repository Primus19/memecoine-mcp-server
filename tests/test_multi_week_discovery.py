import tempfile
from datetime import datetime, timedelta, timezone

from app.multi_week_discovery import ConfirmationLedger, derive_snapshot, discover


def asset(observed_at: str):
    candles = []
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for day in range(28):
        close = 1 + day * .02
        candles.append({
            "date": (start + timedelta(days=day)).date().isoformat(),
            "close": close, "high": close * 1.02, "low": close * .99,
            "volume_usd": 1_000_000 + day * 50_000,
        })
    return {
        "chain": "base", "contract": "0xabc", "symbol": "DURABLE",
        "observed_at": observed_at, "price": candles[-1]["close"],
        "daily_candles": candles, "benchmark_return_7d_pct": 1,
        "holder_growth_7d_pct": 4, "sell_route_ok": True,
        "security_verified": True, "liquidity_usd": 2_000_000,
    }


def test_derives_daily_trend_without_fixed_contract_watchlist():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        result = derive_snapshot(asset("2026-09-03T00:00:00Z"), ledger)
        assert result["price_above_20d_average"] is True
        assert result["daily_higher_highs"] is True
        assert result["daily_higher_lows"] is True
        assert result["relative_strength_7d_pct"] > 0
        assert result["confirmation_count"] == 1


def test_repeated_minute_scans_do_not_create_fake_confirmations():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        derive_snapshot(asset("2026-09-03T00:00:00Z"), ledger)
        result = derive_snapshot(asset("2026-09-03T00:05:00Z"), ledger)
        assert result["confirmation_count"] == 1


def test_setup_becomes_confirmed_after_twelve_hours():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        derive_snapshot(asset("2026-09-03T00:00:00Z"), ledger)
        result = derive_snapshot(asset("2026-09-03T13:00:00Z"), ledger)
        assert result["confirmation_count"] == 2
        assert result["confirmation_span_hours"] == 13
        assert result["confirmation_return_pct"] == 0


def test_emerging_pool_without_historical_candles_earns_honest_forward_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        base = {
            "chain": "robinhood", "contract": "0xemerging", "symbol": "EARLY",
            "token_age_days": 20, "price": 1.0, "daily_candles": [],
            "liquidity_usd": 1_000_000, "volume_24h_usd": 2_000_000,
            "sell_route_ok": True, "round_trip_recovery": .98,
            "sell_impact_bps": 50, "security_verified": False,
        }
        first = derive_snapshot({**base, "observed_at": "2026-09-03T00:00:00Z"}, ledger)
        second = derive_snapshot({**base, "price": 1.08,
                                  "observed_at": "2026-09-03T13:00:00Z"}, ledger)
        assert first["confirmation_count"] == 1
        assert second["confirmation_count"] == 2
        assert second["confirmation_return_pct"] == 8


def test_discovers_every_asset_in_upstream_universe():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        one = asset("2026-09-03T00:00:00Z")
        two = {**one, "chain": "solana", "contract": "mint-two", "symbol": "SECOND"}
        values = discover({"crypto_universe": [one, two]}, ledger)
        assert {row["contract"] for row in values} == {"0xabc", "mint-two"}


def test_cex_confirmation_uses_execution_evidence_not_unavailable_holder_metrics():
    with tempfile.TemporaryDirectory() as directory:
        ledger = ConfirmationLedger(directory + "/confirmations.json")
        value = {**asset("2026-09-03T00:00:00Z"),
                 "chain": "coinbase-spot", "contract": "BTC-USD",
                 "execution_evidence_mode": "CEX_ORDER_BOOK", "venue_operational": True,
                 "security_verified": False, "holder_growth_7d_pct": 0}
        result = derive_snapshot(value, ledger)
        assert result["confirmation_count"] == 1
