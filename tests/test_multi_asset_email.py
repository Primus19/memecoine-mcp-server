import json
import os
import tempfile
import time
from unittest.mock import patch

from app.multi_asset_email import MultiWeekCryptoEmailer, STRATEGY


def test_disabled_emailer_is_explicit():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        status = MultiWeekCryptoEmailer(directory + "/state.json").maybe_send([], {}, {})
    assert status["enabled"] is False
    assert status["status"] == "DISABLED"


def test_trade_event_and_summary_are_restart_safe():
    event = {"event_id": "event-1", "type": "PAPER_FILL", "strategy": STRATEGY,
             "symbol": "RUN", "fill_price": 1.0, "quantity": 10, "thesis": "persistent trend"}
    report = {"multi_week_crypto": {"open": 1, "closed": 0, "realized_pnl_usd": 0,
                                     "open_positions": []}}
    runtime = {"last_scan": "2026-09-03T20:00:00Z", "feed_health": {"status": "READY", "universe_count": 20}}
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "MULTI_WEEK_EMAIL_ENABLED": "true", "MULTI_WEEK_SUMMARY_INTERVAL_SECONDS": "86400"}):
        emailer = MultiWeekCryptoEmailer(directory + "/state.json")
        sent = []
        with patch.object(emailer, "_send", side_effect=lambda content: sent.append(content) or {"subject": content["subject"]}):
            assert emailer.maybe_send([event], report, runtime)["status"] == "QUEUED"
            for _ in range(100):
                if not emailer.inflight:
                    break
                time.sleep(.01)
        assert len(sent) == 1
        assert "1 ACTIONS" in sent[0]["subject"]
        state = json.loads(open(directory + "/state.json", encoding="utf-8").read())
        assert state["sent_event_ids"] == ["event-1"]
        restarted = MultiWeekCryptoEmailer(directory + "/state.json")
        with patch.object(restarted, "_send") as send:
            assert restarted.maybe_send([event], report, runtime)["status"] == "NO_NEW_ACTION"
            send.assert_not_called()


def test_email_content_contains_position_and_paper_warning():
    content = MultiWeekCryptoEmailer._content(None, {"multi_week_crypto": {
        "open": 1, "closed": 0, "realized_pnl_usd": 0,
        "open_positions": [{"symbol": "RUN", "entry_price": 1, "current_mark_price": 1.2,
                            "current_unrealized_pnl_usd": 2, "mfe_usd": 3, "mae_usd": -1,
                            "entry_value_usd": 50, "current_value_usd": 52, "return_pct": 4,
                            "age_minutes": 2880}]}}, {
        "feed_health": {"status": "READY", "universe_count": 20},
        "emerging_discovery": {"candidate_count": 1, "qualified_count": 0,
            "candidates": [{"chain": "robinhood", "symbol": "RUN", "score": 55,
                            "research_score": 82,
                            "research_eligible": True,
                            "confirmation_count": 1, "security_verified": False,
                            "failures": ["chain safety adapter unavailable"]}]},
    }, True)
    assert "RUN" in content["html"]
    assert "PAPER ONLY" in content["html"]
    assert "Universe: 20" in content["text"]
    assert "emerging tracked: 1" in content["text"]
    assert "chain safety adapter unavailable" in content["html"]
    assert "Budget" in content["html"]
    assert "+4.00%" in content["html"]
    assert "Selected emerging research candidates" in content["html"]


def test_monitor_degradation_sends_one_critical_alert_per_incident():
    report = {"multi_week_crypto": {"open": 1, "open_positions": []}}
    runtime = {"held_position_monitor": {"status": "DEGRADED", "errors": [
        {"symbol": "RUN", "error": "held-position quote unavailable"}]}}
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "MULTI_WEEK_EMAIL_ENABLED": "true", "MULTI_WEEK_SUMMARY_INTERVAL_SECONDS": "86400"}):
        emailer = MultiWeekCryptoEmailer(directory + "/state.json")
        emailer.state["last_summary_epoch"] = time.time()
        sent = []
        with patch.object(emailer, "_send", side_effect=lambda content: sent.append(content) or {"subject": content["subject"]}):
            assert emailer.maybe_send([], report, runtime)["status"] == "QUEUED"
            for _ in range(100):
                if not emailer.inflight:
                    break
                time.sleep(.01)
            assert "[CRITICAL]" in sent[0]["subject"]
            assert emailer.maybe_send([], report, runtime)["status"] == "NO_NEW_ACTION"
