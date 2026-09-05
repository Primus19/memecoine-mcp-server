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
