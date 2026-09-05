from datetime import datetime, timezone
from unittest.mock import patch
from app.worker_observability import monitoring_health, worker_evidence, decision_funnel


def test_stale_cycle_is_unhealthy_even_without_error():
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    assert not monitoring_health({"last_scan": "2026-09-05T11:00:00+00:00"}, now)["ok"]
    assert monitoring_health({"last_scan": now.isoformat()}, now)["ok"]
    assert not monitoring_health({"last_scan": now.isoformat(), "last_error": "failed"}, now)["ok"]


def test_unreachable_ledger_is_unknown_not_empty():
    with patch("app.worker_observability.urlopen", side_effect=TimeoutError):
        result = worker_evidence()
    assert result["verification"] == "UNVERIFIED"
    assert result["open_positions"] is None
    assert result["realized_pnl_usd"] is None


def test_funnel_accounts_for_each_evaluation():
    result = decision_funnel([{"status": "PAPER_FILL"},
                              {"status": "REJECTED", "reason": "budget"}])
    assert result == {"evaluated": 2, "filled": 1, "rejected": 1,
                      "rejection_reasons": {"budget": 1}}


def test_restored_ledger_prices_trigger_stop_without_division_by_zero():
    from app.multi_week_crypto import manage_position
    result = manage_position(
        {"fill_price": 100, "stop_price": 88, "research_only": True},
        {"executable_price": 80, "sell_route_ok": True, "round_trip_recovery": .99,
         "market_cap_usd": 2_000_000, "liquidity_usd": 500_000,
         "volume_24h_usd": 1_000_000})
    assert result["action"] == "EXIT"
    assert "stop" in result["reason"]
