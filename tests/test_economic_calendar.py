import unittest
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.economic_calendar import build_request, normalize


class CalendarTests(unittest.TestCase):
    def test_normalizes_only_timestamped_high_impact_currency_events(self):
        future=(datetime.now(timezone.utc)+timedelta(minutes=90)).isoformat()
        result=normalize({"events":[{"currency":"USD","impact":"high","time":future,"title":"CPI"},
                                           {"currency":"EUR","impact":"low","time":future,"title":"minor"}]},
                         "https://calendar.example/events")
        self.assertEqual(1,len(result["events"]))
        self.assertEqual("USD",result["events"][0]["currency"])

    def test_normalizes_trading_economics_fields_and_country_currency(self):
        result = normalize([
            {"Date":"2026-08-24T12:30:00","Country":"United States","Event":"Durable Goods","Importance":3},
            {"Date":"2026-08-24T08:00:00","Country":"Euro Area","Event":"ECB Speech","Importance":3},
            {"Date":"2026-08-24T09:00:00","Country":"United Kingdom","Event":"Minor","Importance":1},
        ], "https://api.tradingeconomics.com/calendar", "trading_economics")
        self.assertEqual(["USD", "EUR"], [event["currency"] for event in result["events"]])
        self.assertTrue(all(event["impact"] == "HIGH" for event in result["events"]))

    def test_trading_economics_request_does_not_publish_key(self):
        env = {"ECONOMIC_CALENDAR_PROVIDER":"trading_economics",
               "TRADING_ECONOMICS_API_KEY":"client:secret",
               "TRADING_ECONOMICS_COUNTRIES":"united states,euro area",
               "ECONOMIC_CALENDAR_LOOKAHEAD_DAYS":"7"}
        with patch.dict(os.environ, env, clear=True):
            url, headers, source, provider = build_request(datetime(2026,8,24,tzinfo=timezone.utc))
        self.assertEqual("trading_economics", provider)
        self.assertIn("client%3Asecret", url)
        self.assertIn("importance=3", url)
        self.assertEqual("https://api.tradingeconomics.com/calendar", source)
        self.assertNotIn("secret", source)
        self.assertNotIn("Authorization", headers)

    def test_generic_request_redacts_query_credentials(self):
        with patch.dict(os.environ, {"ECONOMIC_CALENDAR_PROVIDER":"generic",
                                     "ECONOMIC_CALENDAR_UPSTREAM_URL":"https://calendar.example/events?token=secret"}, clear=True):
            url, _, source, _ = build_request()
        self.assertIn("secret", url)
        self.assertEqual("https://calendar.example/events", source)


if __name__ == "__main__": unittest.main()
