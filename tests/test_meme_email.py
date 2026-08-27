import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.meme_email import MemeReportEmailer
from app.meme_report import render_meme_report


class Ledger:
    def __init__(self):
        self.settings = {}
        self.events = []

    def setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def event(self, kind, payload):
        self.events.append((kind, payload))


ENV = {
    "MEME_EMAIL_REPORT_ENABLED": "true",
    "MEME_EMAIL_PROVIDER": "gmail_api",
    "MEME_EMAIL_FROM": "sender@example.test",
    "MEME_EMAIL_RECIPIENTS": "one@example.test,two@example.test",
    "MEME_EMAIL_GMAIL_CLIENT_ID": "client-id",
    "MEME_EMAIL_GMAIL_CLIENT_SECRET": "client-secret",
    "MEME_EMAIL_GMAIL_REFRESH_TOKEN": "refresh-token",
    "MEME_EMAIL_TIMEZONE": "America/New_York",
}


class MemeEmailTests(unittest.TestCase):
    def test_subject_is_trade_event_and_uses_eastern_time(self):
        now = datetime(2026, 8, 25, 22, 20, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False):
            self.assertEqual("[TRADE] Meme Coin ENTRY FILLED - 2026-08-25 18:20 ET",
                             MemeReportEmailer.subject(now,{"kind":"ENTRY_FILLED"}))

    def test_success_is_restart_safe_and_duplicate_suppressed(self):
        ledger = Ledger()
        emailer = MemeReportEmailer(ledger)
        now = datetime(2026, 8, 25, 22, 20, tzinfo=timezone.utc)
        ledger.set_setting("meme_email_event_mode_initialized","1")
        report={"mode":"LIVE_ARMED","notification_events":[{"seq":42,"kind":"ENTRY_FILLED","payload":{}}]}
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send"):
            emailer._deliver([{"event_id":"42","kind":"ENTRY_FILLED","event":report["notification_events"][0],"report":report}],now)
            result = emailer.maybe_send(report, now)
        self.assertEqual("NO_NEW_TRADE_OR_CRITICAL_EVENT", result["status"])
        self.assertEqual(["42"], __import__("json").loads(ledger.settings["meme_email_sent_event_ids"]))
        self.assertEqual("MEME_TRADE_ALERT_SENT", ledger.events[-1][0])

    def test_routine_events_never_queue_email(self):
        ledger=Ledger();emailer=MemeReportEmailer(ledger)
        report={"notification_events":[{"seq":1,"kind":"MODEL_REVIEW","payload":{}},{"seq":2,"kind":"PREFLIGHT_OK","payload":{}}]}
        with patch.dict(os.environ,ENV,clear=False):result=emailer.maybe_send(report)
        self.assertEqual("NO_NEW_TRADE_OR_CRITICAL_EVENT",result["status"])

    def test_report_contains_live_state_without_scripts(self):
        body = render_meme_report({
            "timestamp": "2026-08-25T22:20:00+00:00", "mode": "LIVE_ARMED",
            "paused": False, "permitted_capital_usdc": 23.98,
            "realized_pnl_usdc": -1.02,
            "portfolio": {"open_position": None, "controls": {"drawdown_pct": 10.1,
                                                               "circuit_breakers": []}},
            "model_review": {"sample_size": 3, "wins": 2, "losses": 1,
                             "status": "MODEL LOCKED - COLLECTING EVIDENCE"},
        })
        self.assertIn("Production Dashboard", body)
        self.assertIn("LIVE_ARMED", body)
        self.assertIn("AUTO EXECUTION", body)
        self.assertIn("ENABLED", body)
        self.assertIn("Capital and risk controls", body)
        self.assertIn("Recent decisions and completed trades", body)
        self.assertIn("Recent audit events and errors", body)
        self.assertIn("background:#111827", body)
        self.assertNotIn("<script", body.lower())

    def test_report_renders_error_detail_and_escapes_untrusted_values(self):
        body = render_meme_report({
            "mode": "LIVE_ARMED", "paused": False,
            "portfolio": {"open_position": None, "controls": {}},
            "notification_events": [{"at": "now", "kind": "POSITION_SUPERVISION_ERROR",
                                      "payload": {"detail": "bad <script>alert(1)</script>"}}],
        })
        self.assertIn("POSITION_SUPERVISION_ERROR", body)
        self.assertIn("bad &lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("bad <script>", body)

    def test_triggering_trade_is_marked_new_and_reason_is_visible(self):
        body=render_meme_report({"mode":"LIVE_ARMED","paused":False,
            "portfolio":{"open_position":None,"controls":{}},
            "_meme_alert":{"kind":"ENTRY_FILLED","event":{"ticket_id":"t-new","payload":{}}},
            "recommendations":[{"ticket_id":"t-new","product_id":"LINK-USDC","status":"FILLED",
                "payload":{"ticket_id":"t-new","score":88,"regime":"RISING","component_scores":{"momentum":14}}}]})
        self.assertIn("NEW ACTION",body)
        self.assertIn(">NEW</span>",body)
        self.assertIn("Score 88.00; regime RISING; strongest evidence: momentum 14.0",body)

    def test_forex_gmail_configuration_is_reused_when_meme_values_are_absent(self):
        forex_env = {
            "FOREX_EMAIL_REPORT_ENABLED": "true",
            "FOREX_EMAIL_PROVIDER": "gmail_api",
            "FOREX_EMAIL_FROM": "shared@example.test",
            "FOREX_EMAIL_RECIPIENTS": "one@example.test,two@example.test",
            "FOREX_EMAIL_GMAIL_CLIENT_ID": "shared-client-id",
            "FOREX_EMAIL_GMAIL_CLIENT_SECRET": "shared-client-secret",
            "FOREX_EMAIL_GMAIL_REFRESH_TOKEN": "shared-refresh-token",
            "FOREX_EMAIL_TIMEZONE": "America/New_York",
        }
        meme_keys = [key for key in os.environ if key.startswith("MEME_EMAIL_")]
        saved = {key: os.environ.pop(key) for key in meme_keys}
        try:
            with patch.dict(os.environ, forex_env, clear=False):
                config = MemeReportEmailer._configuration()
                self.assertTrue(MemeReportEmailer.enabled())
                self.assertEqual("shared@example.test", config["from_address"])
                self.assertEqual("shared-client-id", config["client_id"])
                self.assertEqual(["one@example.test", "two@example.test"], config["recipients"])
        finally:
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
