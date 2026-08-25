import os
import json
import threading
import urllib.request
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.forex_executor import (Handler, LOCK, STATE, Ledger, ThreadingHTTPServer,
                                BrokerError, Executor, closed_trade_pnl, live_armed, practice_armed, safe_quantity,
                                recoverable_managed_trade, transaction_managed_intent_id, validated_snapshots)


class Adapter:
    environment = "live"
    account = "001-001-1234567-001"

    def price(self, _symbol):
        return {
            "status": "tradeable",
            "time": datetime.now(timezone.utc).isoformat(),
            "quoteHomeConversionFactors": {"negativeUnits": "1.0"},
            "bids": [{"price":"1.1"}], "asks": [{"price":"1.1"}],
        }

    def instrument(self, _symbol): return {"marginRate":"0.02"}


class ForexExecutorTests(unittest.TestCase):
    def test_only_tagged_and_protected_trade_can_be_recovered_after_deploy(self):
        trade = {"id":"5", "instrument":"AUD_USD", "currentUnits":"-69", "price":"0.71429",
                 "openTime":"2026-08-24T17:57:54Z",
                 "clientExtensions":{"id":"intent-1", "tag":"primus-forex-v1"},
                 "stopLossOrder":{"price":"0.71608"}, "takeProfitOrder":{"price":"0.71072"}}
        proposal = recoverable_managed_trade(trade, .5)
        self.assertEqual("intent-1", proposal["proposal_id"])
        self.assertEqual("SELL", proposal["side"])
        self.assertEqual(69, proposal["quantity"])
        self.assertIsNone(recoverable_managed_trade({**trade, "clientExtensions":{"id":"intent-1", "tag":"external"}}, .5))
        self.assertIsNone(recoverable_managed_trade({**trade, "stopLossOrder":{}}, .5))

    def test_existing_trade_can_be_recovered_from_tagged_order_fill_chain(self):
        transactions = [
            {"id":"4", "type":"MARKET_ORDER",
             "clientExtensions":{"id":"intent-1", "tag":"primus-forex-v1"}},
            {"id":"6", "type":"ORDER_FILL", "orderID":"4", "tradeOpened":{"tradeID":"5"}},
        ]
        self.assertEqual("intent-1", transaction_managed_intent_id("5", transactions))
        trade = {"id":"5", "instrument":"AUD_USD", "currentUnits":"-69", "price":"0.71429",
                 "openTime":"2026-08-24T17:57:54Z", "stopLossOrder":{"price":"0.71608"},
                 "takeProfitOrder":{"price":"0.71072"}}
        proposal = recoverable_managed_trade(trade, .5, transactions)
        self.assertEqual("intent-1", proposal["proposal_id"])
        untagged = [{**transactions[0], "clientExtensions":{"id":"intent-1", "tag":"external"}}, transactions[1]]
        self.assertIsNone(recoverable_managed_trade(trade, .5, untagged))

    def test_feed_readiness_requires_nonempty_fresh_calendar_verified_snapshots(self):
        now = datetime.now(timezone.utc)
        valid = {"scanned_at": now.isoformat(),
                 "snapshots": [{"symbol": "EUR_USD", "calendar_verified": True}]}
        self.assertEqual("EUR_USD", validated_snapshots(valid, now)[0]["symbol"])
        with self.assertRaisesRegex(Exception, "no tradable snapshots"):
            validated_snapshots({"scanned_at": now.isoformat(), "snapshots": []}, now)
        stale = dict(valid, scanned_at="2020-01-01T00:00:00+00:00")
        with self.assertRaisesRegex(Exception, "stale"):
            validated_snapshots(stale, now)
        unverified = dict(valid, snapshots=[{"symbol": "EUR_USD", "calendar_verified": False}])
        with self.assertRaisesRegex(Exception, "verified economic calendar"):
            validated_snapshots(unverified, now)

    def test_health_is_liveness_when_executor_is_not_ready(self):
        with LOCK:
            original = dict(STATE)
            STATE.update(ok=False, mode="PRACTICE_ARMED", last_scan="", last_error="feed unavailable", open_positions=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["executor_ready"])
            self.assertEqual("PRACTICE_ARMED", payload["mode"])
        finally:
            server.shutdown(); server.server_close()
            with LOCK:
                STATE.clear(); STATE.update(original)

    def test_report_routes_disable_caching_and_raw_broker_data_is_not_required(self):
        with LOCK:
            original = dict(STATE)
            STATE.update(ok=True, mode="PRACTICE_ARMED", last_scan="2026-08-24T03:00:00+00:00",
                         last_error="", open_positions=0, report={"broker": {"nav": 1000}, "snapshots": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/report.json", timeout=2) as response:
                payload = json.loads(response.read())
                self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertNotIn("transactions", payload)
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/report", timeout=2) as response:
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        finally:
            server.shutdown(); server.server_close()
            with LOCK:
                STATE.clear(); STATE.update(original)

    def test_live_requires_all_four_independent_gates(self):
        values = {
            "FOREX_LIVE_ENABLED": "true",
            "FOREX_LIVE_ACK": "I_ACCEPT_REAL_MONEY_RISK",
            "FOREX_ALLOWED_ACCOUNT_ID": Adapter.account,
        }
        with patch.dict(os.environ, values, clear=False):
            self.assertTrue(live_armed(Adapter()))
            os.environ["FOREX_LIVE_ACK"] = "wrong"
            self.assertFalse(live_armed(Adapter()))

    def test_practice_execution_has_separate_acknowledgement(self):
        adapter = Adapter(); adapter.environment = "practice"
        with patch.dict(os.environ, {"FOREX_PRACTICE_EXECUTION_ENABLED":"true",
                                     "FOREX_PRACTICE_ACK":"I_ACCEPT_PRACTICE_ORDER_EXECUTION"}, clear=False):
            self.assertTrue(practice_armed(adapter))
            self.assertFalse(live_armed(adapter))

    def test_append_only_hash_chain_and_position_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":100,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":2.5}
            ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            ledger.event("PAPER_FILL", proposal)
            ledger.event("CHECK", {"ok": True})
            self.assertEqual(1, ledger.open_count())
            rows = ledger.db.execute("SELECT previous_hash,record_hash FROM events ORDER BY seq").fetchall()
            self.assertEqual("GENESIS", rows[0]["previous_hash"])
            self.assertEqual(rows[0]["record_hash"], rows[1]["previous_hash"])

    def test_home_currency_risk_sizing(self):
        proposal = {"symbol":"EUR_USD","reference_price":1.1,"stop_price":1.095}
        # The $50 notional cap is stricter than the stop-risk calculation.
        self.assertEqual(45.0, safe_quantity(Adapter(), proposal, 2.5))

    def test_closed_broker_trade_releases_position_limit_and_records_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":10,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":0.5}
            ledger.add_intent(proposal, "LIVE", "SUBMITTED")
            ledger.update_intent("p1", "OPEN", "order-1", "trade-1")
            ledger.update_excursions([{"id":"trade-1", "unrealizedPL":"0.40", "financing":"-0.01"}])
            ledger.update_excursions([{"id":"trade-1", "unrealizedPL":"-0.20", "financing":"-0.01"}])
            transactions = [{"type":"ORDER_FILL", "tradesClosed":[{"tradeID":"trade-1", "realizedPL":"0.75",
                                                                      "financing":"-0.02"}]}]
            pnl = closed_trade_pnl(transactions)
            ledger.close_broker_intent("trade-1", pnl["trade-1"])
            self.assertEqual(0, ledger.open_count())
            self.assertEqual(0.73, ledger.realized_pnl())
            review = ledger.model_review(80)
            self.assertEqual(1, review["sample_size"])
            self.assertAlmostEqual(0.39, review["average_max_favorable_excursion_usd"])
            self.assertAlmostEqual(-0.21, review["average_max_adverse_excursion_usd"])
            self.assertEqual("MODEL LOCKED - COLLECTING EVIDENCE", review["status"])
            self.assertFalse(review["parameters_changed"])

    def test_cancelled_market_order_is_not_counted_open(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":10,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":0.5}
            ledger.add_intent(proposal, "LIVE", "SUBMITTING")
            ledger.update_intent("p1", "CANCELLED")
            self.assertEqual(0, ledger.open_count())

    def test_open_risk_and_currency_symbols_support_guarded_second_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"AUD_USD","reference_price":.7,
                        "side":"SELL","quantity":10,"stop_price":.71,"target_price":.68,"maximum_loss_usd":.5}
            ledger.add_intent(proposal,"LIVE","OPEN")
            self.assertEqual(.5,ledger.open_risk())
            self.assertEqual(["AUD_USD"],ledger.open_symbols())

    def test_default_forex_limit_is_one_and_hard_cap_is_two(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREX_MAX_OPEN_POSITIONS",None)
            self.assertEqual(1,Executor.max_positions())
        with patch.dict(os.environ,{"FOREX_MAX_OPEN_POSITIONS":"8"},clear=False):
            self.assertEqual(2,Executor.max_positions())

    def test_impossible_score_threshold_fails_readiness(self):
        executor = object.__new__(Executor)
        executor.engine = type("Engine", (), {"policy": type("Policy", (), {"minimum_score": 101.0})()})()
        with self.assertRaisesRegex(BrokerError, "between 0 and 100"):
            executor.scan()


if __name__ == "__main__": unittest.main()
