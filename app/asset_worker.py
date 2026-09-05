from __future__ import annotations

import json
import os
import time
import urllib.request
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .multi_asset import MultiAssetEngine, MultiAssetRejected, PaperLedger
from .multi_week_crypto import (STRATEGY as MULTI_WEEK_CRYPTO_STRATEGY,
                                evaluate_candidate, manage_position)
from .multi_week_discovery import ConfirmationLedger, discover
from .multi_asset_email import MultiWeekCryptoEmailer
from .emerging_crypto import refresh_held_position_quotes
from .worker_observability import monitoring_health, decision_funnel
from .version import deployment_info


class HealthHandler(BaseHTTPRequestHandler):
    ledger: PaperLedger | None = None
    runtime: dict = {"last_scan": "", "last_error": "", "last_outcomes": [], "last_closes": []}

    def do_GET(self):
        if self.path not in {"/health", "/status", "/report.json"}:
            self.send_error(404); return
        token = os.getenv("MULTI_ASSET_REPORT_TOKEN", "").strip()
        if self.path != "/health" and token and self.headers.get("Authorization") != f"Bearer {token}":
            self.send_error(401); return
        report = self.ledger.report() if self.ledger else {}
        payload = {"service": "multi-asset-paper-worker", "paper_only": True,
                   **self.runtime, **monitoring_health(self.runtime), "deployment": deployment_info()}
        if self.path != "/health":
            payload.update(report)
        else:
            payload.update({"open_position_count": len(report.get("open_positions") or []),
                            "realized_pnl_usd": report.get("realized_pnl_usd", 0)})
        body = json.dumps(payload, default=str).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): return


def fetch(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "primus-multi-asset-paper/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        value = json.loads(response.read().decode())
        return value if isinstance(value, dict) else {}


def supervise(ledger: PaperLedger, snapshots: list[dict], max_hold_minutes: int = 240) -> list[dict]:
    """Close the worker's own positions so supervision cannot split-brain.

    Railway services do not share their local /app/data directories. Keeping
    closing logic in the process that owns the ledger makes the paper sleeve
    durable and self-consistent even if the legacy supervisor is restarted.
    """
    current = {str(item.get("contract") or item.get("symbol") or "").lower(): item
               for item in snapshots if float(item.get("price") or 0) > 0}
    now = datetime.now(timezone.utc)
    closes = []
    for position in ledger.positions():
        snapshot = current.get(str(position.get("contract") or position.get("symbol") or "").lower())
        price = float((snapshot or {}).get("executable_sell_price") or
                      (snapshot or {}).get("price") or 0)
        try:
            opened = datetime.fromisoformat(str(position["recorded_at"]).replace("Z", "+00:00"))
            position_max_hold = max_hold_minutes
            if position.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY:
                position_max_hold = max(90 * 24 * 60,
                                        float(position.get("expected_holding_days") or 21) * 24 * 60)
            expired = (now - opened).total_seconds() >= position_max_hold * 60
        except (KeyError, ValueError):
            expired = False
        price_source = str((snapshot or {}).get("price_source") or "CURRENT_EXECUTABLE_MARK")
        if price:
            ledger.mark(position, price, price_source, snapshot)
            ledger.record_intraday_checkpoints(position, price, snapshot)
            ledger.record_due_horizon_checkpoints(position, price, snapshot)
        elif expired:
            retained = ledger.latest_marks().get(str(position.get("proposal_id") or ""), {})
            price = float(retained.get("mark_price") or position.get("fill_price") or 0)
            price_source = "LAST_RETAINED_MARK" if retained else "ENTRY_FALLBACK_NO_MARK"
        else:
            continue
        if position.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY and snapshot:
            stage = int(position.get("entry_stage") or 1)
            if (position.get("research_only") is not True and stage == 1 and
                    int(snapshot.get("confirmation_count") or 0) >= 3 and
                    snapshot.get("daily_higher_lows") is True and
                    float(snapshot.get("volume_7d_vs_prior_ratio") or 0) >= 1.10):
                closes.append(ledger.add_stage(str(position["proposal_id"]), price, 2))
                position = next(item for item in ledger.positions()
                                if item.get("proposal_id") == position.get("proposal_id"))
                stage = 2
            if (position.get("research_only") is not True and stage == 2 and
                    snapshot.get("breakout_confirmed") is True and
                    snapshot.get("sell_route_ok") is True and
                    float(snapshot.get("round_trip_recovery") or 0) >= .97):
                closes.append(ledger.add_stage(str(position["proposal_id"]), price, 3))
                position = next(item for item in ledger.positions()
                                if item.get("proposal_id") == position.get("proposal_id"))
            if expired:
                closes.append(ledger.close(str(position["proposal_id"]), price,
                                           "MAX_MULTI_WEEK_HOLD", price_source=price_source))
                continue
            management = manage_position(position, {**snapshot, "executable_price": price})
            if management["action"] == "EXIT":
                closes.append(ledger.close(str(position["proposal_id"]), price,
                                           management["reason"], price_source=price_source))
            elif management["action"] == "TAKE_PROFIT":
                closes.append(ledger.partial_close(
                    str(position["proposal_id"]), price, float(management["fraction"]),
                    management["reason"], price_source=price_source,
                    profit_tier=str(management["profit_tier"])))
            continue
        side = str(position["side"])
        stop, target = float(position["stop_price"]), float(position["target_price"])
        reason = "STOP" if (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop) else \
                 "TARGET" if (side == "BUY" and price >= target) or (side == "SELL" and price <= target) else ""
        if not reason and expired:
            reason = "MAX_HOLD" if price_source == "CURRENT_EXECUTABLE_MARK" else "MAX_HOLD_STALE_MARK"
        if reason:
            closes.append(ledger.close(str(position["proposal_id"]), price, reason,
                                       price_source=price_source))
    return closes


def rotate_for_stronger_research_candidate(ledger: PaperLedger,
                                            crypto_snapshots: list[dict]) -> tuple[list[dict], set[str]]:
    """At most one daily paper rotation; never displace a profitable runner."""
    capacity = max(1, int(os.getenv("MULTI_WEEK_TOTAL_MAX_OPEN_POSITIONS", "3")))
    today = datetime.now(timezone.utc).date()
    for event in ledger.records():
        if event.get("type") != "PAPER_CLOSE" or not str(event.get("reason") or "").startswith(
                "DAILY_OPPORTUNITY_ROTATION"):
            continue
        try:
            if datetime.fromisoformat(str(event.get("recorded_at") or "").replace(
                    "Z", "+00:00")).date() == today:
                return [], set()
        except ValueError:
            continue
    positions = [item for item in ledger.position_diagnostics()
                 if item.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY]
    if len(positions) < capacity:
        return [], set()
    open_symbols = {str(item.get("symbol") or "").upper() for item in positions}
    candidates = [(evaluate_candidate(item), item) for item in crypto_snapshots
                  if str(item.get("symbol") or "").upper() not in open_symbols]
    candidates = [(decision, item) for decision, item in candidates
                  if decision.get("qualified") or decision.get("research_eligible")]
    if not candidates:
        return [], set()
    best_decision, _ = max(candidates, key=lambda pair: float(
        pair[0].get("score") if pair[0].get("qualified") else
        pair[0].get("research_score") or 0))
    weakest = min(positions, key=lambda item: float(item.get("score") or 0))
    best_score = float(best_decision.get("score") if best_decision.get("qualified") else
                       best_decision.get("research_score") or 0)
    if (float(weakest.get("age_minutes") or 0) < 1440 or
            float(weakest.get("current_unrealized_pnl_usd") or 0) > 0 or
            best_score < float(weakest.get("score") or 0) + 15):
        return [], set()
    mark = float(weakest.get("current_mark_price") or 0)
    if mark <= 0:
        return [], set()
    event = ledger.close(str(weakest["proposal_id"]), mark,
                         "DAILY_OPPORTUNITY_ROTATION: stronger candidate by at least 15 points")
    return [event], {str(weakest.get("symbol") or "").upper()}


def main() -> None:
    if os.getenv("MULTI_ASSET_WORKER_ENABLED", "false").lower() != "true":
        raise SystemExit("MULTI_ASSET_WORKER_ENABLED is not true; refusing to start")
    feed_url = os.environ["MULTI_ASSET_FEED_URL"]
    token = os.getenv("MULTI_ASSET_FEED_TOKEN", "")
    interval = max(15, min(300, int(os.getenv("MULTI_ASSET_SCAN_INTERVAL_SECONDS", "60"))))
    ledger_path = os.getenv("MULTI_ASSET_LEDGER_PATH", "/app/data/multi_asset.jsonl")
    ledger = PaperLedger(ledger_path)
    emailer = MultiWeekCryptoEmailer(os.getenv(
        "MULTI_WEEK_EMAIL_STATE_PATH", "/app/data/multi_week_email_state.json"))
    confirmations = ConfirmationLedger(os.getenv(
        "MULTI_WEEK_CONFIRMATION_PATH", "/app/data/multi_week_confirmations.json"))
    engine = MultiAssetEngine(ledger)
    held_quote_interval = max(300, min(900, int(os.getenv(
        "MULTI_WEEK_HELD_QUOTE_INTERVAL_SECONDS", "300"))))
    last_held_quote_refresh = 0.0
    last_held_quote_refresh_at = ""
    held_quotes: list[dict] = []
    held_quote_errors: list[dict] = []
    service_started_at = datetime.now(timezone.utc)
    restored_records = len(ledger.records())
    restored_open_positions = len(ledger.positions())
    cycle_count = 0
    failed_cycle_count = 0
    HealthHandler.ledger = ledger
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        cycle_started_at = datetime.now(timezone.utc)
        try:
            # Continue held-position management when discovery is unavailable.
            try:
                payload = fetch(feed_url, token)
            except Exception as feed_exc:
                payload = {"snapshots": [], "crypto_universe": [], "crypto_health": {
                    "status": "DEGRADED", "last_error": type(feed_exc).__name__}}
            snapshots = list(payload.get("snapshots", []))
            crypto_snapshots = discover(payload, confirmations)
            if time.monotonic() - last_held_quote_refresh >= held_quote_interval:
                held_quotes, held_quote_errors = refresh_held_position_quotes(ledger.positions())
                last_held_quote_refresh = time.monotonic()
                last_held_quote_refresh_at = datetime.now(timezone.utc).isoformat()
            held_by_identity = {(str(item.get("chain") or "").lower(),
                                 str(item.get("contract") or "").lower()): item for item in held_quotes}
            crypto_snapshots = [held_by_identity.get((str(item.get("chain") or "").lower(),
                                                      str(item.get("contract") or "").lower()), item)
                                for item in crypto_snapshots]
            known = {(str(item.get("chain") or "").lower(), str(item.get("contract") or "").lower())
                     for item in crypto_snapshots}
            crypto_snapshots.extend(item for key, item in held_by_identity.items() if key not in known)
            emerging_candidates = []
            for item in crypto_snapshots:
                if str(item.get("chain") or "") == "coinbase-spot":
                    continue
                decision = evaluate_candidate(item)
                emerging_candidates.append({
                    "chain": item.get("chain"), "symbol": item.get("symbol"),
                    "contract": item.get("contract"), "score": decision["score"],
                    "research_score": decision.get("research_score"),
                    "research_eligible": decision.get("research_eligible"),
                    "qualified": decision["qualified"], "liquidity_usd": item.get("liquidity_usd"),
                    "volume_24h_usd": item.get("volume_24h_usd"),
                    "confirmation_count": item.get("confirmation_count"),
                    "security_verified": item.get("security_verified"),
                    "failures": decision["hard_gate_failures"][:8],
                    "source_urls": item.get("source_urls") or [],
                })
            emerging_candidates.sort(key=lambda item: float(item.get("research_score") or 0), reverse=True)
            def crypto_rank(item: dict) -> tuple[int, float]:
                decision = evaluate_candidate(item)
                if decision.get("qualified") and decision.get("cohort") == "LIQUID_TSMOM_FORWARD_PAPER":
                    return 3, float(decision.get("score") or 0)
                if decision.get("qualified"):
                    return 2, float(decision.get("score") or 0)
                if decision.get("research_eligible"):
                    return 1, float(decision.get("research_score") or 0)
                return 0, float(decision.get("research_score") or 0)

            crypto_snapshots.sort(key=crypto_rank, reverse=True)
            snapshots.extend(crypto_snapshots)
            closes = supervise(ledger, snapshots, max(15, int(os.getenv("ASSET_MAX_HOLD_MINUTES", "240"))))
            rotations, rotation_exclusions = rotate_for_stronger_research_candidate(ledger, crypto_snapshots)
            closes.extend(rotations)
            outcomes = []
            events = list(closes)
            for snapshot in snapshots:
                if payload.get("crypto_health", {}).get("status") == "DEGRADED":
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED",
                                     "reason": "discovery feed degraded; entries blocked"})
                    continue
                if str(snapshot.get("symbol") or "").upper() in rotation_exclusions:
                    continue
                try:
                    result = engine.process(snapshot)
                    events.append(result)
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "PAPER_FILL", "event_id": result["event_id"]})
                except MultiAssetRejected as exc:
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)})
            report = ledger.report()
            crypto_bucket = dict((report.get("by_strategy") or {}).get(MULTI_WEEK_CRYPTO_STRATEGY) or {})
            crypto_positions = [item for item in report.get("open_positions", [])
                                if item.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY]
            crypto_events = [item for item in reversed(ledger.records())
                             if item.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY and
                             item.get("type") in {"PAPER_FILL", "PAPER_ADD", "PAPER_PARTIAL_CLOSE", "PAPER_CLOSE"}]
            crypto_bucket.update(open=len(crypto_positions), open_positions=crypto_positions,
                                 realized_pnl_usd=crypto_bucket.get("net_pnl_usd", 0),
                                 daily_realized_pnl_usd=report.get("daily_realized_pnl_usd", 0),
                                 recent_actions=crypto_events[:25],
                                 research_open=sum(item.get("research_only") is True for item in crypto_positions),
                                 qualified_open=sum(item.get("research_only") is not True for item in crypto_positions))
            crypto_bucket["chain_performance"] = report.get("multi_week_chain_performance") or {}
            crypto_bucket["cohort_performance"] = report.get("multi_week_cohort_performance") or {}
            crypto_bucket["horizon_checkpoints"] = report.get("multi_week_horizon_checkpoints") or []
            crypto_bucket["intraday_checkpoints"] = report.get("multi_week_intraday_checkpoints") or []
            crypto_bucket["robinhood_chain"] = (report.get("multi_week_chain_performance") or {}).get(
                "robinhood", {"opened": 0, "open": 0, "closed": 0, "wins": 0,
                              "losses": 0, "realized_pnl_usd": 0, "unrealized_pnl_usd": 0,
                              "checkpoint_count": 0, "win_rate_pct": None})
            feed_health = dict(payload.get("crypto_health") or {})
            feed_health.setdefault("universe_count", len(payload.get("crypto_universe") or []))
            feed_health.setdefault("status", "READY" if feed_health["universe_count"] else "DEGRADED")
            if not feed_health["universe_count"]:
                feed_health.setdefault("last_error", "crypto universe is empty")
            cycle_count += 1
            runtime = {"last_scan": datetime.now(timezone.utc).isoformat(), "last_error": "",
                       "decision_funnel": decision_funnel(outcomes),
                       "last_outcomes": outcomes[-25:], "last_closes": closes[-25:],
                       "worker_state": {
                           "service_started_at": service_started_at.isoformat(),
                           "uptime_seconds": round((datetime.now(timezone.utc) - service_started_at).total_seconds(), 1),
                           "cycle_count": cycle_count,
                           "failed_cycle_count": failed_cycle_count,
                           "last_cycle_started_at": cycle_started_at.isoformat(),
                           "last_successful_cycle_at": datetime.now(timezone.utc).isoformat(),
                           "ledger_file": Path(ledger_path).name,
                           "restored_record_count": restored_records,
                           "restored_open_position_count": restored_open_positions,
                           "persistence_configured": bool(ledger_path),
                       },
                       "position_monitoring": ledger.position_diagnostics(),
                       "multi_week_crypto": crypto_bucket, "feed_health": feed_health,
                       "held_position_monitor": {
                           "status": "READY" if not held_quote_errors and len(held_quotes) >= len([
                               item for item in ledger.positions()
                               if item.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY]) else "DEGRADED",
                           "refresh_interval_seconds": held_quote_interval,
                           "last_refresh_at": last_held_quote_refresh_at,
                           "fresh_quote_count": len(held_quotes), "errors": held_quote_errors[-10:],
                       },
                       "emerging_discovery": {"candidate_count": len(emerging_candidates),
                           "qualified_count": sum(bool(item["qualified"]) for item in emerging_candidates),
                           "research_eligible_count": sum(bool(item["research_eligible"])
                                                          for item in emerging_candidates),
                           "candidates": emerging_candidates[:20]}}
            HealthHandler.runtime = runtime
            runtime["email_delivery"] = emailer.maybe_send(events, {**report, "multi_week_crypto": crypto_bucket}, runtime)
            print(json.dumps({"event": "MULTI_ASSET_SCAN", "paper_only": True,
                              "closes": closes, "outcomes": outcomes}), flush=True)
        except Exception as exc:
            failed_cycle_count += 1
            HealthHandler.runtime = {**HealthHandler.runtime,
                "last_error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "worker_state": {**HealthHandler.runtime.get("worker_state", {}),
                    "service_started_at": service_started_at.isoformat(),
                    "cycle_count": cycle_count, "failed_cycle_count": failed_cycle_count,
                    "last_cycle_started_at": cycle_started_at.isoformat(),
                    "last_failed_cycle_at": datetime.now(timezone.utc).isoformat(),
                    "ledger_file": Path(ledger_path).name,
                    "restored_record_count": restored_records,
                    "restored_open_position_count": restored_open_positions,
                    "persistence_configured": bool(ledger_path)}}
            print(json.dumps({"event": "MULTI_ASSET_SCAN_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
