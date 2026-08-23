import unittest
from datetime import datetime, timedelta, timezone

from app.economic_calendar import normalize


class CalendarTests(unittest.TestCase):
    def test_normalizes_only_timestamped_high_impact_currency_events(self):
        future=(datetime.now(timezone.utc)+timedelta(minutes=90)).isoformat()
        result=normalize({"events":[{"currency":"USD","impact":"high","time":future,"title":"CPI"},
                                           {"currency":"EUR","impact":"low","time":future,"title":"minor"}]},
                         "https://calendar.example/events")
        self.assertEqual(1,len(result["events"]))
        self.assertEqual("USD",result["events"][0]["currency"])


if __name__ == "__main__": unittest.main()
