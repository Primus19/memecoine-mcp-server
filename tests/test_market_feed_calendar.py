import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.market_feed import (Handler, LOCK, STATE, ThreadingHTTPServer,
                             calendar_evidence, configured_symbols,
                             _coinbase_execution, coinbase_crypto_market_universe,
                             crypto_market_universe, scan_symbols)


class MarketFeedCalendarTests(unittest.TestCase):
    def test_crypto_universe_publishes_market_history_without_inventing_safety(self):
        markets = [{"id": "alpha", "symbol": "alp", "name": "Alpha",
                    "current_price": 2, "market_cap": 5_000_000,
                    "total_volume": 1_000_000}]
        history = {"prices": [[1_700_000_000_000, 1.5], [1_700_086_400_000, 2.0]],
                   "total_volumes": [[1_700_000_000_000, 900_000],
                                     [1_700_086_400_000, 1_000_000]]}
        with patch.dict(os.environ, {"MULTI_WEEK_CRYPTO_UNIVERSE_SIZE": "5",
                                    "MULTI_WEEK_CRYPTO_PROVIDERS": "coingecko",
                                    "EMERGING_CRYPTO_DISCOVERY_ENABLED": "false",
                                    "MULTI_WEEK_CRYPTO_REQUEST_SPACING_SECONDS": "0"}, clear=False), \
             patch("app.market_feed.fetch_json", side_effect=[markets, history]):
            rows, health = crypto_market_universe()
        self.assertEqual("alpha", rows[0]["contract"])
        self.assertEqual(2, len(rows[0]["daily_candles"]))
        self.assertFalse(rows[0]["sell_route_ok"])
        self.assertFalse(rows[0]["security_verified"])
        self.assertEqual("coingecko", health["provider"])

    def test_coinbase_order_book_calculates_cost_stressed_recovery(self):
        result = _coinbase_execution(
            {"asks": [["100.00", "5"]], "bids": [["99.90", "5"]]}, 100, 20)
        self.assertTrue(result["sell_route_ok"])
        self.assertGreater(result["round_trip_recovery"], .99)
        self.assertLess(result["spread_bps"], 20)

    def test_coinbase_universe_has_execution_evidence_without_claiming_contract_safety(self):
        products = [{"id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD",
                     "status": "online"}]
        candles = [[1_700_000_000 + day * 86400, 90 + day, 101 + day,
                    95 + day, 100 + day, 1000] for day in range(30)]
        book = {"asks": [["130.1", "10"]], "bids": [["130", "10"]]}
        with patch.dict(os.environ, {"MULTI_WEEK_CRYPTO_UNIVERSE_SIZE": "5",
                                    "MULTI_WEEK_CRYPTO_REQUEST_SPACING_SECONDS": "0"}, clear=False), \
             patch("app.market_feed.fetch_json", side_effect=[products, candles, book]):
            rows = coinbase_crypto_market_universe()
        self.assertEqual("CEX_ORDER_BOOK", rows[0]["execution_evidence_mode"])
        self.assertTrue(rows[0]["venue_operational"])
        self.assertTrue(rows[0]["sell_route_ok"])
        self.assertFalse(rows[0]["security_verified"])

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
