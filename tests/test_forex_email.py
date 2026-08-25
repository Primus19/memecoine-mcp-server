import base64
import json
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

    def test_gmail_api_refreshes_token_and_sends_mime_over_https(self):
        emailer = ForexReportEmailer(Ledger())
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        env = {
            **ENV,
            "FOREX_EMAIL_PROVIDER": "gmail_api",
            "FOREX_EMAIL_GMAIL_CLIENT_ID": "client-id",
            "FOREX_EMAIL_GMAIL_CLIENT_SECRET": "client-secret",
            "FOREX_EMAIL_GMAIL_REFRESH_TOKEN": "refresh-secret",
        }
        requests = []

        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return self.body

        def fake_open(request, timeout):
            requests.append((request, timeout))
            if request.full_url == "https://oauth2.googleapis.com/token":
                return Response(b'{"access_token":"short-lived-token"}')
            return Response(b'{"id":"gmail-message-1"}')

        with patch.dict(os.environ, env, clear=False), patch("app.forex_email.urllib.request.urlopen", fake_open):
            emailer._send({"mode": "LIVE_ARMED"}, now)

        self.assertEqual("https://oauth2.googleapis.com/token", requests[0][0].full_url)
        self.assertIn(b"refresh-secret", requests[0][0].data)
        self.assertEqual("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                         requests[1][0].full_url)
        self.assertEqual("Bearer short-lived-token", requests[1][0].headers["Authorization"])
        send_payload = json.loads(requests[1][0].data)
        mime = base64.urlsafe_b64decode(send_payload["raw"]).decode()
        self.assertIn("one@example.test", mime)
        self.assertNotIn("client-secret", mime)
        self.assertNotIn("refresh-secret", mime)

    def test_gmail_api_configuration_requires_oauth_secrets(self):
        env = {
            **ENV,
            "FOREX_EMAIL_PROVIDER": "gmail_api",
            "FOREX_EMAIL_GMAIL_CLIENT_ID": "",
            "FOREX_EMAIL_GMAIL_CLIENT_SECRET": "",
            "FOREX_EMAIL_GMAIL_REFRESH_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(ValueError, "client_id"):
                ForexReportEmailer._configuration()

    def test_resend_uses_https_api_without_exposing_key_in_payload(self):
        ledger = Ledger()
        emailer = ForexReportEmailer(ledger)
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        env = {
            **ENV,
            "FOREX_EMAIL_PROVIDER": "resend",
            "FOREX_EMAIL_RESEND_API_KEY": "resend-secret",
        }

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"id":"email-1"}'

        captured = {}
        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["body"] = request.data.decode()
            captured["timeout"] = timeout
            return Response()

        with patch.dict(os.environ, env, clear=False), patch("app.forex_email.urllib.request.urlopen", fake_open):
            emailer._send({"mode": "LIVE_ARMED"}, now)

        self.assertEqual("https://api.resend.com/emails", captured["url"])
        self.assertEqual("Bearer resend-secret", captured["authorization"])
        self.assertNotIn("resend-secret", captured["body"])
        self.assertIn("one@example.test", captured["body"])

    def test_resend_configuration_requires_api_key(self):
        env = {**ENV, "FOREX_EMAIL_PROVIDER": "resend", "FOREX_EMAIL_RESEND_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(ValueError, "resend_api_key"):
                ForexReportEmailer._configuration()

    def test_disabled_sender_does_nothing(self):
        with patch.dict(os.environ, {"FOREX_EMAIL_REPORT_ENABLED": "false"}, clear=False):
            self.assertEqual("DISABLED", ForexReportEmailer(Ledger()).maybe_send({})["status"])


if __name__ == "__main__":
    unittest.main()
