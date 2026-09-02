import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.market_feed import Handler, LOCK, STATE, ThreadingHTTPServer, calendar_evidence, configured_symbols, scan_symbols


class MarketFeedCalendarTests(unittest.TestCase):
    def test_health_is_process_liveness_while_status_remains_fail_closed(self):
        with LOCK:
            original = dict(STATE)
            STATE.update(ok=False, scanned_at="", snapshots=[], error="upstream unavailable")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
                self.assertEqual(200, response.status)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f"{base}/status", timeout=2)
            self.assertEqual(503, raised.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            with LOCK:
                STATE.clear()
                STATE.update(original)

    def test_legacy_symbol_override_retains_expanded_liquid_core(self):
        symbols = configured_symbols("EUR_USD,GBP_USD,USD_JPY")
        self.assertEqual(12, len(symbols))
        self.assertIn("USD_CHF", symbols)
        self.assertIn("XAU_USD", symbols)
        self.assertIn("XAG_USD", symbols)
        self.assertIn("EUR_GBP", symbols)

    def test_scan_fails_closed_when_every_symbol_is_rejected(self):
        with patch("app.market_feed.forex_snapshot", side_effect=ValueError("calendar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "no valid forex snapshots"):
                scan_symbols(object(), ["EUR_USD", "GBP_USD"])

    def test_scan_retains_valid_symbols_and_reports_rejections(self):
        with patch("app.market_feed.forex_snapshot", side_effect=[{"symbol": "EUR_USD"}, ValueError("bad quote")]):
            snapshots, rejected = scan_symbols(object(), ["EUR_USD", "GBP_USD"])
        self.assertEqual([{"symbol": "EUR_USD"}], snapshots)
        self.assertEqual("GBP_USD", rejected[0]["symbol"])

    def test_honors_event_specific_blackout_window(self):
        payload = {"observed_at": datetime.now(timezone.utc).isoformat(),
                   "source_url": "https://official.example/calendar",
                   "events": [{"currency":"JPY", "impact":"HIGH", "minutes_until":600,
                               "blackout_before_minutes":720, "blackout_after_minutes":360}]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response.__exit__.return_value = False
        with patch.dict(os.environ, {"ECONOMIC_CALENDAR_URL":"https://calendar.internal/events"}, clear=True), \
             patch("urllib.request.urlopen", return_value=response):
            result = calendar_evidence("USD_JPY")
        self.assertTrue(result["verified"])
        self.assertEqual(600, result["minutes"])
        self.assertTrue(result["blackout"])
        self.assertEqual(0, result["blackout_distance_minutes"])

    def test_distant_event_is_reported_without_activating_blackout(self):
        payload = {"observed_at": datetime.now(timezone.utc).isoformat(),
                   "source_url": "https://official.example/calendar",
                   "events": [{"currency":"AUD", "impact":"HIGH", "minutes_until":1250,
                               "blackout_before_minutes":60, "blackout_after_minutes":30}]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response.__exit__.return_value = False
        with patch.dict(os.environ, {"ECONOMIC_CALENDAR_URL":"https://calendar.internal/events"}, clear=True), \
             patch("urllib.request.urlopen", return_value=response):
            result = calendar_evidence("AUD_USD")
        self.assertEqual(1250, result["minutes"])
        self.assertFalse(result["blackout"])
        self.assertEqual(1190, result["blackout_distance_minutes"])

    def test_any_active_pair_event_wins_over_a_nearer_non_blackout_event(self):
        payload = {"observed_at": datetime.now(timezone.utc).isoformat(),
                   "source_url": "https://official.example/calendar",
                   "events": [
                       {"currency":"JPY", "impact":"HIGH", "minutes_until":100,
                        "blackout_before_minutes":30, "blackout_after_minutes":30},
                       {"currency":"USD", "impact":"HIGH", "minutes_until":600,
                        "blackout_before_minutes":720, "blackout_after_minutes":360},
                   ]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response.__exit__.return_value = False
        with patch.dict(os.environ, {"ECONOMIC_CALENDAR_URL":"https://calendar.internal/events"}, clear=True), \
             patch("urllib.request.urlopen", return_value=response):
            result = calendar_evidence("USD_JPY")
        self.assertTrue(result["blackout"])
        self.assertEqual(600, result["minutes"])


if __name__ == "__main__": unittest.main()
