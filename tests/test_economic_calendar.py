import unittest
import os
import json
import threading
import urllib.request
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.economic_calendar import (build_request, normalize, parse_bls_ics,
                                   Handler, LOCK, STATE, ThreadingHTTPServer,
                                   parse_boe_html, parse_boj_html,
                                   parse_ecb_html, parse_fomc_html, parse_fred_calendar_html,
                                   scan_official_composite, load_official_snapshot)


class CalendarTests(unittest.TestCase):
    def test_validated_snapshot_fallback_refreshes_observed_at(self):
        clock = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        payload = {"snapshot_generated_at": (clock - timedelta(hours=1)).isoformat(),
                   "source_url": "https://fred.stlouisfed.org/releases/calendar",
                   "coverage": ["USD", "EUR", "GBP", "JPY"], "sources": [],
                   "events": [{"currency":"USD", "impact":"HIGH", "event":"CPI",
                               "time": (clock + timedelta(hours=2)).isoformat(),
                               "source_url":"https://fred.stlouisfed.org/releases/calendar",
                               "blackout_before_minutes":60, "blackout_after_minutes":60}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(payload))
            result = load_official_snapshot(clock, path)
        self.assertEqual(clock.isoformat(), result["observed_at"])
        self.assertEqual("official_snapshot", result["provider"])
        self.assertEqual(120, result["events"][0]["minutes_until"])

    def test_stale_snapshot_fails_closed(self):
        clock = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        payload = {"snapshot_generated_at": (clock - timedelta(days=3)).isoformat(),
                   "source_url":"https://fred.stlouisfed.org/releases/calendar",
                   "coverage":["USD", "EUR", "GBP", "JPY"], "events":[]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "snapshot stale"):
                load_official_snapshot(clock, path)

    def test_official_fetch_error_names_the_failed_source(self):
        with patch("urllib.request.urlopen", side_effect=OSError("blocked")):
            with self.assertRaisesRegex(RuntimeError, "official calendar source fred_employment failed"):
                from app.economic_calendar import _fetch_official
                _fetch_official("fred_employment", datetime(2026, 8, 24, tzinfo=timezone.utc))

    def test_health_is_liveness_even_before_calendar_is_ready(self):
        with LOCK:
            original = dict(STATE)
            STATE.update(ok=False, observed_at=None, error="upstream unavailable")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["calendar_ready"])
            self.assertEqual("forex-economic-calendar", payload["service"])
        finally:
            server.shutdown()
            server.server_close()
            with LOCK:
                STATE.clear()
                STATE.update(original)

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

    def test_official_composite_request_needs_no_paid_key(self):
        with patch.dict(os.environ, {"ECONOMIC_CALENDAR_PROVIDER":"official_composite"}, clear=True):
            url, _, source, provider = build_request()
        self.assertEqual("official-composite://calendar", url)
        self.assertEqual("official_composite", provider)
        self.assertTrue(source.startswith("https://fred.stlouisfed.org/"))

    def test_parses_official_source_shapes(self):
        bls = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=America/New_York:20260904T083000\nSUMMARY:Employment Situation\nEND:VEVENT\nEND:VCALENDAR"""
        fed = """>2026 FOMC Meetings</a><div class="fomc-meeting__month"><strong>September</strong></div><div class="fomc-meeting__date">15-16*</div></main>"""
        ecb = """<dt>10/09/2026</dt><dd>Governing Council: monetary policy meeting, followed by press conference</dd>"""
        boe = """<h2>2026 confirmed dates</h2><table><tr><td>Thursday 17 September</td><td>Summary</td></tr></table></section>"""
        boj = """<h2 id="p2026">2026</h2><table><tr><td>Sept. 17 (Thurs.), 18 (Fri.)</td><td>-</td></tr></table><h2 id="p2027">2027</h2>"""
        self.assertEqual("USD", parse_bls_ics(bls)[0]["currency"])
        self.assertEqual("USD", parse_fomc_html(fed, 2026)[0]["currency"])
        self.assertEqual("EUR", parse_ecb_html(ecb)[0]["currency"])
        self.assertEqual("GBP", parse_boe_html(boe, 2026)[0]["currency"])
        jpy = parse_boj_html(boj, 2026)[0]
        self.assertEqual("JPY", jpy["currency"])
        self.assertEqual(720, jpy["blackout_before_minutes"])

    def test_parses_fred_release_calendar_in_central_time(self):
        body = '''<table><tr class="odd"><td><span>Friday September 04, 2026</span></td></tr>
        <tr><td>7:30 am</td><td><a href="/release?rid=50">Employment Situation</a></td></tr></table>'''
        event = parse_fred_calendar_html(body)[0]
        self.assertEqual("USD", event["currency"])
        self.assertEqual("2026-09-04T12:30:00+00:00", event["time"])

    def test_fomc_parser_does_not_leak_adjacent_year_panels(self):
        body = '''<div class="panel panel-default"><a>2026 FOMC Meetings</a>
        <div class="fomc-meeting__month"><strong>September</strong></div>
        <div class="fomc-meeting__date">15-16*</div></div>
        <div class="panel panel-default"><a>2025 FOMC Meetings</a>
        <div class="fomc-meeting__month"><strong>September</strong></div>
        <div class="fomc-meeting__date">17-18*</div></div>'''
        events = parse_fomc_html(body, 2026)
        self.assertEqual(1, len(events))
        self.assertEqual("2026-09-16T18:00:00+00:00", events[0]["time"])

    def test_composite_verifies_coverage_even_when_window_is_quiet(self):
        clock = datetime(2026, 8, 24, tzinfo=timezone.utc)
        def fake_fetch(name, _):
            currencies = {"fred_employment":"USD", "fred_cpi":"USD", "fred_ppi":"USD",
                          "federal_reserve":"USD", "ecb":"EUR", "bank_of_england":"GBP",
                          "bank_of_japan":"JPY"}
            return ({"name":name, "currency":currencies[name], "url":"https://official.example", "ok":True, "event_count":0}, [])
        with patch("app.economic_calendar._fetch_official", side_effect=fake_fetch), patch.dict(
                os.environ, {"OFFICIAL_CALENDAR_REQUIRED_CURRENCIES":"USD,EUR,GBP,JPY"}, clear=True):
            result = scan_official_composite(clock)
        self.assertEqual(["EUR", "GBP", "JPY", "USD"], result["coverage"])
        self.assertEqual([], result["events"])


if __name__ == "__main__": unittest.main()
