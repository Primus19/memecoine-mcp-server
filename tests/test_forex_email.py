import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.forex_email import ForexReportEmailer


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
    "FOREX_EMAIL_REPORT_ENABLED": "true",
    "FOREX_EMAIL_SMTP_HOST": "smtp.example.test",
    "FOREX_EMAIL_SMTP_PORT": "587",
    "FOREX_EMAIL_SMTP_USERNAME": "sender@example.test",
    "FOREX_EMAIL_SMTP_PASSWORD": "secret",
    "FOREX_EMAIL_FROM": "sender@example.test",
    "FOREX_EMAIL_RECIPIENTS": "one@example.test,two@example.test",
    "FOREX_EMAIL_TIMEZONE": "America/New_York",
}


class ForexEmailTests(unittest.TestCase):
    def test_et_hour_and_subject_are_stable(self):
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False):
            self.assertEqual("2026-08-25T09-0400", ForexReportEmailer.hour_key(now))
            self.assertEqual("[HOURLY] Forex Live Trading Dashboard - 2026-08-25 09:00 ET",
                             ForexReportEmailer.subject(now))

    def test_success_is_recorded_and_duplicate_is_suppressed(self):
        ledger = Ledger()
        emailer = ForexReportEmailer(ledger)
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send"):
            emailer._deliver({"mode": "LIVE_ARMED"}, emailer.hour_key(now), now)
            result = emailer.maybe_send({"mode": "LIVE_ARMED"}, now)
        self.assertEqual("DUPLICATE_SUPPRESSED", result["status"])
        self.assertEqual("2026-08-25T09-0400", ledger.settings["forex_email_last_sent_hour"])
        self.assertEqual("FOREX_EMAIL_SENT", ledger.events[-1][0])

    def test_failure_is_audited_without_marking_hour_sent(self):
        ledger = Ledger()
        emailer = ForexReportEmailer(ledger)
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send", side_effect=RuntimeError("down")):
            emailer._deliver({}, emailer.hour_key(now), now)
        self.assertNotIn("forex_email_last_sent_hour", ledger.settings)
        self.assertIn("RuntimeError", ledger.settings["forex_email_last_error"])
        self.assertEqual("FOREX_EMAIL_FAILED", ledger.events[-1][0])

    def test_disabled_sender_does_nothing(self):
        with patch.dict(os.environ, {"FOREX_EMAIL_REPORT_ENABLED": "false"}, clear=False):
            self.assertEqual("DISABLED", ForexReportEmailer(Ledger()).maybe_send({})["status"])


if __name__ == "__main__":
    unittest.main()
