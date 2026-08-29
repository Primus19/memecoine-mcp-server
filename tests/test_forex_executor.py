import os
import json
import threading
import urllib.request
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.forex_executor import (Handler, LOCK, STATE, Ledger, ThreadingHTTPServer,
                                BrokerError, Executor, broker_client_id, closed_trade_pnl, live_armed, practice_armed, safe_quantity,
                                FIVE_STREAK_STRATEGY,
                                five_streak_email_actions, five_streak_profit_floor_r,
                                five_streak_signals, live_profit_protection_shadow, recoverable_managed_trade,
                                historical_managed_trade_outcomes,
                                transaction_managed_intent_id, validated_snapshots)


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
    def test_live_checkpoint_retains_executable_audit_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            inserted = ledger.record_live_checkpoint({
                "trade_id": "37", "instrument": "NZD_USD", "current_price": .59165,
                "unrealized_pnl_usd": .0973, "current_spread_bps": 10.14,
                "financing_usd": -.0001, "current_price_observed_at": "2026-08-29T12:00:00Z",
            }, 15)
            self.assertTrue(inserted)
            row = ledger.live_trade_checkpoints()[0]
            self.assertEqual("37", row["trade_id"])
            self.assertEqual(15, row["checkpoint_minutes"])
            self.assertAlmostEqual(.0973, row["pnl_usd"])
            self.assertFalse(ledger.record_live_checkpoint({
                "trade_id": "37", "instrument": "NZD_USD", "current_price": .59160,
            }, 15))

    def test_five_streak_profit_floor_ratchets_without_moving_backward(self):
        self.assertEqual(0, five_streak_profit_floor_r(.49))
        self.assertEqual(.20, five_streak_profit_floor_r(.50))
        self.assertEqual(.50, five_streak_profit_floor_r(.75))
        self.assertEqual(.75, five_streak_profit_floor_r(1.0))
        self.assertEqual(.75, five_streak_profit_floor_r(1.49))

    def test_bryne_ratchet_transfer_is_shadow_only(self):
        trade = {"trade_id":"10", "instrument":"EUR_JPY", "side":"BUY",
                 "entry_price":185.748, "stop_price":185.284, "current_price":185.9}
        result = live_profit_protection_shadow(trade, .8)
        self.assertTrue(result["shadow_only"])
        self.assertEqual("BRYNE_RATCHET_TRANSFER_V1", result["challenger"])
        self.assertEqual(.5, result["protected_floor_r"])
        self.assertTrue(result["would_exit_now"])

    def test_bryne_ratchet_transfer_never_uses_incomplete_trade(self):
        result = live_profit_protection_shadow({"trade_id":"x"}, 1.0)
        self.assertFalse(result["eligible"])
        self.assertTrue(result["shadow_only"])

    def test_five_streak_v3_fires_first_completion_with_filters(self):
        candles = []
        last_open = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=10)
        for index, close in enumerate((100.0, 100.4, 100.8, 101.2, 101.6, 102.0)):
            opened = close - .3 if index else 99.8
            candles.append({"time": (last_open - timedelta(minutes=5 * (5-index))).isoformat(), "open": opened, "high": close + .1,
                            "low": opened - .1, "close": close})
        signals = five_streak_signals({"symbol":"USD_JPY", "bid":102.0, "ask":102.01, "price":102.005,
                                       "session_liquid":True,"horizon_agreement":.8,
                                       "trend_strength":.2,"change_1h_pct":.2,
                                       "five_streak_candles":candles})
        self.assertEqual(1, len(signals))
        self.assertEqual(last_open.isoformat(), signals[0]["signal_time"])
        self.assertLess(signals[0]["evaluation_latency_seconds"], 30)
        self.assertTrue(all(item["side"] == "BUY" for item in signals))
        self.assertAlmostEqual(1.5 * (signals[0]["reference_price"] - signals[0]["stop_price"]),
                               signals[0]["target_price"] - signals[0]["reference_price"])

    def test_five_streak_neutral_candle_resets_progress(self):
        candles = [{"time":"t0","open":100,"high":101,"low":99,"close":100.4},
                   {"time":"t1","open":100.4,"high":101,"low":100,"close":100.8},
                   {"time":"t2","open":100.8,"high":101,"low":100,"close":100.8}]
        self.assertEqual([], five_streak_signals({"symbol":"USD_JPY", "bid":100.7, "ask":100.71,"price":100.705,
                                                  "session_liquid":True,"horizon_agreement":.8,
                                                  "trend_strength":.2,"change_1h_pct":.2,
                                                  "five_streak_candles":candles}))

    def test_five_streak_report_history_is_strategy_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"five-1","expires_at":"2999-01-01T00:00:00Z",
                        "signal_time":"2026-08-27T14:00:00Z", "symbol":"USD_JPY", "reference_price":150,
                        "side":"BUY","quantity":1,"stop_price":149.8,"target_price":150.2,
                        "maximum_loss_usd":.125,"strategy":"FOREX_FIVE_STREAK_EXPERIMENT"}
            ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            rows = ledger.strategy_intents("FOREX_FIVE_STREAK_EXPERIMENT")
            self.assertEqual("2026-08-27T14:00:00Z", rows[0]["signal_time"])
            self.assertEqual("USD_JPY", rows[0]["symbol"])

    def test_five_streak_uses_executable_exit_and_no_time_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"five-old","expires_at":"2020-01-01T00:00:00Z",
                        "signal_time":"2020-01-01T00:00:00Z", "symbol":"EUR_USD",
                        "reference_price":1.1000,"side":"BUY","quantity":1,
                        "stop_price":1.0990,"target_price":1.1010,"maximum_loss_usd":.125,
                        "strategy":FIVE_STREAK_STRATEGY}
            ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            with ledger.db:
                ledger.db.execute("UPDATE intents SET created_at=? WHERE id=?",
                                  ("2020-01-01T00:00:00+00:00", "five-old"))
            executor = object.__new__(Executor)
            executor.ledger = ledger
            closes = executor.supervise_paper([{"symbol":"EUR_USD","bid":1.1005,"ask":1.1007,"price":1.1006}])
            self.assertEqual([], closes)
            closes = executor.supervise_paper([{"symbol":"EUR_USD","bid":1.0989,"ask":1.0991,"price":1.0990}])
            self.assertEqual("STOP", closes[0]["reason"])
            self.assertEqual(1.0989, closes[0]["fill_price"])

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

    def test_tagged_closed_trade_history_backfills_learning_ledger(self):
        transactions = [
            {"id":"4", "type":"MARKET_ORDER", "time":"2026-08-24T10:00:00Z",
             "instrument":"AUD_USD", "units":"-69",
             "clientExtensions":{"id":"intent-closed", "tag":"primus-forex-v1"},
             "stopLossOnFill":{"price":"0.71608"}, "takeProfitOnFill":{"price":"0.71072"}},
            {"id":"5", "type":"ORDER_FILL", "orderID":"4", "time":"2026-08-24T10:00:01Z",
             "instrument":"AUD_USD", "units":"-69", "price":"0.71429",
             "tradeOpened":{"tradeID":"10", "units":"-69"}},
            {"id":"8", "type":"ORDER_FILL", "time":"2026-08-24T11:00:00Z",
             "tradesClosed":[{"tradeID":"10", "realizedPL":"-0.12", "financing":"0.01"}]},
        ]
        outcomes = historical_managed_trade_outcomes(transactions, "LIVE")
        self.assertEqual(1, len(outcomes))
        self.assertEqual("intent-closed", outcomes[0]["proposal_id"])
        self.assertAlmostEqual(-.11, outcomes[0]["realized_pnl_usd"])
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            self.assertTrue(ledger.import_closed_broker_intent(outcomes[0]))
            self.assertFalse(ledger.import_closed_broker_intent(outcomes[0]))
            self.assertEqual(1, ledger.model_review(80)["sample_size"])

    def test_untagged_broker_history_is_not_imported(self):
        transactions = [{"id":"4", "type":"MARKET_ORDER", "instrument":"AUD_USD", "units":"-69",
                         "clientExtensions":{"id":"manual", "tag":"external"}},
                        {"id":"5", "type":"ORDER_FILL", "orderID":"4", "price":"0.71429",
                         "tradeOpened":{"tradeID":"10", "units":"-69"}},
                        {"id":"8", "type":"ORDER_FILL",
                         "tradesClosed":[{"tradeID":"10", "realizedPL":"2"}]}]
        self.assertEqual([], historical_managed_trade_outcomes(transactions, "LIVE"))

    def test_cancelled_market_order_is_not_counted_open(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":10,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":0.5}
            ledger.add_intent(proposal, "LIVE", "SUBMITTING")
            self.assertEqual(1, ledger.open_count())
            self.assertEqual(.5, ledger.open_risk())
            ledger.update_intent("p1", "CANCELLED")
            self.assertEqual(0, ledger.open_count())

    def test_oanda_client_id_maps_back_to_submitting_uuid_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            intent_id = "12345678-1234-1234-1234-123456789abc"
            proposal = {"proposal_id":intent_id,"expires_at":"2999-01-01T00:00:00Z","symbol":"EUR_USD","reference_price":1.1,
                        "side":"BUY","quantity":10,"stop_price":1.0,"target_price":1.2,"maximum_loss_usd":0.5}
            ledger.add_intent(proposal, "LIVE", "SUBMITTING")
            client_id=broker_client_id(intent_id)
            self.assertEqual("12345678123412341234123456789abc", client_id)
            self.assertEqual(intent_id, ledger.intent_for_broker_client_id(client_id)["id"])

    def test_open_risk_and_currency_symbols_support_guarded_second_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory + "/forex.sqlite3")
            proposal = {"proposal_id":"p1","expires_at":"2999-01-01T00:00:00Z","symbol":"AUD_USD","reference_price":.7,
                        "side":"SELL","quantity":10,"stop_price":.71,"target_price":.68,"maximum_loss_usd":.5}
            ledger.add_intent(proposal,"LIVE","OPEN")
            self.assertEqual(.5,ledger.open_risk())
            self.assertEqual(["AUD_USD"],ledger.open_symbols())

    def test_forex_position_limit_defaults_to_two_and_cannot_exceed_two(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREX_MAX_OPEN_POSITIONS", None)
            self.assertEqual(2, Executor.max_positions())
        with patch.dict(os.environ, {"FOREX_MAX_OPEN_POSITIONS":"1"}, clear=False):
            self.assertEqual(1, Executor.max_positions())
        with patch.dict(os.environ, {"FOREX_MAX_OPEN_POSITIONS":"8"}, clear=False):
            self.assertEqual(2, Executor.max_positions())

    def test_nav_compounding_and_drawdown_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = object.__new__(Executor)
            executor.ledger = Ledger(directory + "/forex.sqlite3")
            executor.base_risk_pct = 0.01
            normal = executor.risk_limits(100.0)
            self.assertEqual(1.0, normal["risk_per_trade_usd"])
            self.assertEqual(2.0, normal["combined_risk_usd"])
            self.assertEqual(2, normal["maximum_open_positions"])
            self.assertEqual(175.0, normal["combined_notional_usd"])
            reduced = executor.risk_limits(95.0)
            self.assertAlmostEqual(0.475, reduced["risk_per_trade_usd"])
            self.assertEqual(2, reduced["maximum_open_positions"])
            severe = executor.risk_limits(92.0)
            self.assertAlmostEqual(0.23, severe["risk_per_trade_usd"])
            self.assertEqual(1, severe["maximum_open_positions"])
            halted = executor.risk_limits(90.0)
            self.assertTrue(halted["new_entries_halted"])
            self.assertEqual(0, halted["maximum_open_positions"])

    def test_dynamic_notional_budget_controls_sizing(self):
        proposal = {"symbol":"EUR_USD","reference_price":1.1,"stop_price":1.095}
        self.assertEqual(
            20.0,
            safe_quantity(Adapter(), proposal, 2.5, margin_budget_usd=10.0,
                          notional_budget_usd=22.0),
        )

    def test_impossible_score_threshold_fails_readiness(self):
        executor = object.__new__(Executor)
        executor.engine = type("Engine", (), {"policy": type("Policy", (), {"minimum_score": 101.0})()})()
        with self.assertRaisesRegex(BrokerError, "between 0 and 100"):
            executor.scan()

    def test_bryne_paper_fill_becomes_new_email_action_with_reason(self):
        outcomes=[{"status":"PAPER_FILL","intent_id":"five-1","symbol":"EUR_USD",
                   "side":"BUY","signal_time":"2026-08-27T21:25:00Z","entry":1.16554,
                   "entry_reason":"Five consecutive bullish M5 candles triggered a BUY."}]
        intents=[{"id":"five-1","quantity":100,"entry_reason":outcomes[0]["entry_reason"]}]
        actions=five_streak_email_actions(outcomes,[],intents,{"nav":50,"unrealized_pl":0})
        self.assertEqual("PAPER BUY",actions[0]["email_action"])
        self.assertEqual("Bryne and Lot-Bill Strategy",actions[0]["strategy_name"])
        self.assertEqual("PAPER_OPEN",actions[0]["status"])
        self.assertTrue(actions[0]["action_id"].startswith("five-streak:v3:open:"))
        self.assertIn("Five consecutive bullish",actions[0]["entry_reason"])
        self.assertIn("paper-only",actions[0]["warnings"][0].lower())


if __name__ == "__main__": unittest.main()
