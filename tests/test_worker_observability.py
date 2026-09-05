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
def test_late_checkpoint_is_not_a_counterfactual():
    from app.multi_asset import checkpoint_view
    row = {"checkpoint_minutes": 15, "age_minutes": 699,
           "executable_pnl_usd": -12.5}
    result = checkpoint_view(row)
    assert result["counterfactual_valid"] is False
    assert result["executable_pnl_usd"] is None
    assert checkpoint_view(result)["late_observation"]["executable_pnl_usd"] == -12.5
    assert row["executable_pnl_usd"] == -12.5
    assert checkpoint_view({"checkpoint_minutes": 15, "age_minutes": 18})["counterfactual_valid"]


def test_daily_pnl_uses_utc_day_and_strategy():
    import tempfile
    from pathlib import Path
    from app.multi_asset import PaperLedger
    with tempfile.TemporaryDirectory() as directory:
        ledger = PaperLedger(Path(directory) / "ledger.jsonl")
        records = [
            {"type": "PAPER_CLOSE", "strategy": "FOREX", "realized_pnl_usd": 2,
             "recorded_at": "2026-09-04T23:30:00+00:00"},
            {"type": "PAPER_CLOSE", "strategy": "CRYPTO", "realized_pnl_usd": -10,
             "recorded_at": "2026-09-05T01:00:00+00:00"}]
        with patch.object(ledger, "records", return_value=records), patch(
                "app.multi_asset.now_utc", return_value=datetime(2026, 9, 5, 4, tzinfo=timezone.utc)):
            report = ledger.report()
        assert report["daily_realized_pnl_usd"] == -10
        assert report["by_strategy"]["FOREX"]["daily_realized_pnl_usd"] == 0
        assert report["by_strategy"]["CRYPTO"]["daily_realized_pnl_usd"] == -10
