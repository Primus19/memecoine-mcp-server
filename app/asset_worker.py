from __future__ import annotations

import json
import os
import time
import urllib.request
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .multi_asset import MultiAssetEngine, MultiAssetRejected, PaperLedger
from .multi_week_crypto import STRATEGY as MULTI_WEEK_CRYPTO_STRATEGY, manage_position
from .multi_week_discovery import ConfirmationLedger, discover


class HealthHandler(BaseHTTPRequestHandler):
    ledger: PaperLedger | None = None
    runtime: dict = {"last_scan": "", "last_error": "", "last_outcomes": [], "last_closes": []}

    def do_GET(self):
        if self.path not in {"/health", "/status", "/report.json"}:
            self.send_error(404); return
        report = self.ledger.report() if self.ledger else {}
        payload = {"ok": True, "service": "multi-asset-paper-worker", "paper_only": True,
                   **self.runtime}
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
        price = float((snapshot or {}).get("price") or 0)
        try:
            opened = datetime.fromisoformat(str(position["recorded_at"]).replace("Z", "+00:00"))
            position_max_hold = max_hold_minutes
            if position.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY:
                position_max_hold = max(90 * 24 * 60,
                                        float(position.get("expected_holding_days") or 21) * 24 * 60)
            expired = (now - opened).total_seconds() >= position_max_hold * 60
        except (KeyError, ValueError):
            expired = False
        price_source = "CURRENT_EXECUTABLE_MARK"
        if price:
            ledger.mark(position, price, price_source)
        elif expired:
            retained = ledger.latest_marks().get(str(position.get("proposal_id") or ""), {})
            price = float(retained.get("mark_price") or position.get("fill_price") or 0)
            price_source = "LAST_RETAINED_MARK" if retained else "ENTRY_FALLBACK_NO_MARK"
        else:
            continue
        if position.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY and snapshot:
            stage = int(position.get("entry_stage") or 1)
            if (stage == 1 and int(snapshot.get("confirmation_count") or 0) >= 3 and
                    snapshot.get("daily_higher_lows") is True and
                    float(snapshot.get("volume_7d_vs_prior_ratio") or 0) >= 1.10):
                ledger.add_stage(str(position["proposal_id"]), price, 2)
                position = next(item for item in ledger.positions()
                                if item.get("proposal_id") == position.get("proposal_id"))
                stage = 2
            if (stage == 2 and snapshot.get("breakout_confirmed") is True and
                    snapshot.get("sell_route_ok") is True and
                    float(snapshot.get("round_trip_recovery") or 0) >= .97):
                ledger.add_stage(str(position["proposal_id"]), price, 3)
                position = next(item for item in ledger.positions()
                                if item.get("proposal_id") == position.get("proposal_id"))
            management = manage_position(position, {**snapshot, "executable_price": price})
            if management["action"] == "EXIT":
                closes.append(ledger.close(str(position["proposal_id"]), price,
                                           management["reason"], price_source=price_source))
            elif management["action"] == "TAKE_PROFIT":
                closes.append(ledger.partial_close(
                    str(position["proposal_id"]), price, float(management["fraction"]),
                    management["reason"], price_source=price_source))
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


def main() -> None:
    if os.getenv("MULTI_ASSET_WORKER_ENABLED", "false").lower() != "true":
        raise SystemExit("MULTI_ASSET_WORKER_ENABLED is not true; refusing to start")
    feed_url = os.environ["MULTI_ASSET_FEED_URL"]
    token = os.getenv("MULTI_ASSET_FEED_TOKEN", "")
    interval = max(15, min(300, int(os.getenv("MULTI_ASSET_SCAN_INTERVAL_SECONDS", "60"))))
    ledger = PaperLedger(os.getenv("MULTI_ASSET_LEDGER_PATH", "/app/data/multi_asset.jsonl"))
    confirmations = ConfirmationLedger(os.getenv(
        "MULTI_WEEK_CONFIRMATION_PATH", "/app/data/multi_week_confirmations.json"))
    engine = MultiAssetEngine(ledger)
    HealthHandler.ledger = ledger
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            payload = fetch(feed_url, token)
            snapshots = list(payload.get("snapshots", []))
            snapshots.extend(discover(payload, confirmations))
            closes = supervise(ledger, snapshots, max(15, int(os.getenv("ASSET_MAX_HOLD_MINUTES", "240"))))
            outcomes = []
            for snapshot in snapshots:
                try:
                    result = engine.process(snapshot)
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "PAPER_FILL", "event_id": result["event_id"]})
                except MultiAssetRejected as exc:
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)})
            HealthHandler.runtime = {"last_scan": datetime.now(timezone.utc).isoformat(), "last_error": "",
                                     "last_outcomes": outcomes[-25:], "last_closes": closes[-25:]}
            print(json.dumps({"event": "MULTI_ASSET_SCAN", "paper_only": True,
                              "closes": closes, "outcomes": outcomes}), flush=True)
        except Exception as exc:
            HealthHandler.runtime = {**HealthHandler.runtime, "last_error": f"{type(exc).__name__}: {str(exc)[:500]}"}
            print(json.dumps({"event": "MULTI_ASSET_SCAN_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
