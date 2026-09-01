import base64
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.forex_email import ForexReportEmailer
from app.forex_executor import confirmed_trade_actions


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
    @staticmethod
    def action():
        return {
            "action_id": "tx-22:close:trade-10",
            "email_action": "CLOSED",
            "action": "Position closed",
            "pair": "EUR_JPY",
            "side": "CLOSE",
            "execution_time": "2026-08-25T13:42:00+00:00",
            "filled_quantity": 42,
            "execution_price": 186.677,
            "realized_pnl_usd": 0.25,
            "resulting_unrealized_pnl_usd": 0,
            "nav": 50.25,
            "margin_used": 0,
            "margin_available": 50.25,
            "trigger": "The protective take-profit was executed.",
            "entry_reason": "Multi Horizon BUY; score 82; trend and liquidity aligned.",
            "position_impact": "Exposure decreased by 42 units.",
            "remaining_positions": [],
            "signal_trigger": "Original signal score 82.",
            "calendar_state": "Verified; no active blackout.",
            "executor_state": "LIVE_ARMED and ready",
            "risk_summary": "1% per trade; 2% combined",
            "warnings": [],
        }

    def test_trade_subject_uses_action_and_execution_time(self):
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False):
            self.assertEqual("2026-08-25T09-0400", ForexReportEmailer.hour_key(now))
            self.assertEqual("[TRADE] Forex CLOSED - EUR/JPY - 2026-08-25 09:42 ET",
                             ForexReportEmailer.subject(self.action(), now))

    def test_delivery_status_exposes_provider_audit_fields(self):
        ledger = Ledger()
        ledger.set_setting("forex_email_last_message_id", "message-123")
        ledger.set_setting("forex_email_last_thread_id", "thread-456")
        ledger.set_setting("forex_email_last_subject", "[TRADE] Forex CLOSED")
        ledger.set_setting("forex_email_last_recipients", json.dumps(["one@example.test", "two@example.test"]))
        status = ForexReportEmailer(ledger).status()
        self.assertEqual("message-123", status["last_message_id"])
        self.assertEqual("thread-456", status["last_thread_id"])
        self.assertEqual("[TRADE] Forex CLOSED", status["last_subject"])
        self.assertEqual(["one@example.test", "two@example.test"], status["last_recipients"])

    def test_success_is_recorded_and_same_action_is_suppressed(self):
        ledger = Ledger()
        emailer = ForexReportEmailer(ledger)
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        ledger.set_setting("forex_email_event_mode_initialized", "1")
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send"):
            emailer._deliver([self.action()], now)
            result = emailer.maybe_send({"_trade_actions": [self.action()]}, now)
        self.assertEqual("NO_NEW_TRADE_ACTION", result["status"])
        self.assertIn(self.action()["action_id"], ledger.settings["forex_email_sent_action_ids"])
        self.assertEqual("FOREX_TRADE_EMAIL_SENT", ledger.events[-1][0])

    def test_first_scan_establishes_history_baseline_without_email(self):
        ledger=Ledger();emailer=ForexReportEmailer(ledger)
        with patch.dict(os.environ,ENV,clear=False),patch.object(emailer,"_send") as send:
            result=emailer.maybe_send({"_trade_actions":[self.action()]})
        self.assertEqual("HISTORY_BASELINE_ESTABLISHED",result["status"])
        send.assert_not_called()

    def test_failure_is_audited_and_action_remains_pending(self):
        ledger = Ledger()
        emailer = ForexReportEmailer(ledger)
        now = datetime(2026, 8, 25, 13, 42, tzinfo=timezone.utc)
        with patch.dict(os.environ, ENV, clear=False), patch.object(emailer, "_send", side_effect=RuntimeError("down")):
            ledger.set_setting("forex_email_pending_actions", json.dumps([self.action()]))
            emailer._deliver([self.action()], now)
        self.assertNotIn("forex_email_sent_action_ids", ledger.settings)
        self.assertIn(self.action()["action_id"], ledger.settings["forex_email_pending_actions"])
        self.assertIn("RuntimeError", ledger.settings["forex_email_last_error"])
        self.assertEqual("FOREX_TRADE_EMAIL_FAILED", ledger.events[-1][0])

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
            emailer._send(self.action(), now)

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
            emailer._send(self.action(), now)

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

    def test_no_trade_action_does_not_queue_email(self):
        with patch.dict(os.environ, ENV, clear=False):
            self.assertEqual(
                "NO_NEW_TRADE_ACTION",
                ForexReportEmailer(Ledger()).maybe_send({"_trade_actions": []})["status"],
            )

    def test_html_explains_trigger_positions_and_profit(self):
        body = ForexReportEmailer._trade_html(self.action())
        self.assertIn("NEW ACTION", body)
        self.assertIn("REASON FOR ENTRY / ACTION", body)
        self.assertIn("Multi Horizon BUY; score 82; trend and liquidity aligned.", body)
        self.assertIn("Why this report was triggered", body)
        self.assertIn("protective take-profit", body)
        self.assertIn("Position impact", body)
        self.assertIn("+$0.2500", body)

    def test_bryne_report_has_strategy_name_and_highlighted_new_columns(self):
        action = {**self.action(),
                  "strategy_name": "Bryne and Lot-Bill Strategy",
                  "email_action": "PAPER CLOSED", "side": "BUY",
                  "signal_time": "2026-08-27T21:25:00Z", "entry_price": 1.16554,
                  "stop_price": 1.164, "target_price": 1.16708,
                  "maximum_loss_usd": 0.10, "realized_pnl_usd": -0.0771,
                  "exit_reason": "Paper exit: MAX_HOLD."}
        subject = ForexReportEmailer.subject(action)
        body = ForexReportEmailer._trade_html(action)
        self.assertIn("[TRADE] Bryne and Lot-Bill Strategy | NEW PAPER CLOSED", subject)
        for label in ("BRYNE AND LOT-BILL STRATEGY", "NEW ACTION", ">New<", "Signal UTC",
                      "Reason for entry", "Exit reason", "CLOSED"):
            self.assertIn(label, body)
        self.assertIn("-$0.0771", body)
        self.assertNotIn("OPEN — profit/loss not final", body)

    def test_confirmed_take_profit_fill_becomes_detailed_close_action(self):
        transactions = [{
            "id": "22", "type": "ORDER_FILL", "time": "2026-08-25T13:42:00Z",
            "instrument": "EUR_JPY", "reason": "TAKE_PROFIT_ORDER", "price": "186.677",
            "tradesClosed": [{"tradeID": "10", "units": "-42", "realizedPL": "0.25",
                              "financing": "-0.01"}],
        }]
        summary = {"nav": 50.24, "unrealized_pl": 0, "margin_used": 0, "margin_available": 50.24}
        risk = {"minimum_score": 80, "risk_per_trade_pct": .01, "combined_risk_pct": .02,
                "maximum_open_positions": 2, "drawdown_pct": 0}
        actions = confirmed_trade_actions(
            transactions, summary, [], [], risk,
            [{"calendar_verified": True, "economic_event_within_minutes": 0}],
            [{"broker_trade_id": "10", "symbol": "EUR_JPY"}], [])
        self.assertEqual(1, len(actions))
        self.assertEqual("CLOSED", actions[0]["email_action"])
        self.assertIn("take-profit", actions[0]["trigger"])
        self.assertAlmostEqual(.24, actions[0]["realized_pnl_usd"])


if __name__ == "__main__":
    unittest.main()
