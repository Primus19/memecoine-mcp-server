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
    def test_subject_and_hour_use_eastern_time(self):
        now = datetime(2026, 8, 25, 22, 20, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False):
            self.assertEqual("2026-08-25T18-0400", MemeReportEmailer.hour_key(now))
            self.assertEqual("[HOURLY] Meme Coin Live Trading Dashboard - 2026-08-25 18:00 ET",
                             MemeReportEmailer.subject(now))

    def test_success_is_restart_safe_and_duplicate_suppressed(self):
        ledger = Ledger()
        emailer = MemeReportEmailer(ledger)
        now = datetime(2026, 8, 25, 22, 20, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send"):
            emailer._deliver({"mode": "LIVE_ARMED"}, emailer.hour_key(now), now)
            result = emailer.maybe_send({"mode": "LIVE_ARMED"}, now)
        self.assertEqual("DUPLICATE_SUPPRESSED", result["status"])
        self.assertEqual("2026-08-25T18-0400", ledger.settings["meme_email_last_sent_hour"])
        self.assertEqual("MEME_EMAIL_SENT", ledger.events[-1][0])

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
        self.assertIn("AUTO EXECUTION ENABLED", body)
        self.assertNotIn("<script", body.lower())

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
