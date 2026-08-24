import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.market_feed import calendar_evidence, scan_symbols


class MarketFeedCalendarTests(unittest.TestCase):
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
        self.assertEqual(0, result["minutes"])


if __name__ == "__main__": unittest.main()
