import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.market_feed import calendar_evidence


class MarketFeedCalendarTests(unittest.TestCase):
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
