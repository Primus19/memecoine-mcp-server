from __future__ import annotations

import json
import os
import time
import urllib.request
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .multi_asset import MultiAssetEngine, MultiAssetRejected, PaperLedger


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


def supervise(ledger: PaperLedger, snapshots: list[dict], max_hold_minutes: int = 1440) -> list[dict]:
    """Close the worker's own positions so supervision cannot split-brain.

    Railway services do not share their local /app/data directories. Keeping
    closing logic in the process that owns the ledger makes the paper sleeve
    durable and self-consistent even if the legacy supervisor is restarted.
    """
    marks = {str(item.get("symbol") or "").upper(): float(item.get("price") or 0)
             for item in snapshots if float(item.get("price") or 0) > 0}
    now = datetime.now(timezone.utc)
    closes = []
    for position in ledger.positions():
        price = marks.get(str(position.get("symbol") or "").upper())
        if not price:
            continue
        side = str(position["side"])
        stop, target = float(position["stop_price"]), float(position["target_price"])
        reason = "STOP" if (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop) else \
                 "TARGET" if (side == "BUY" and price >= target) or (side == "SELL" and price <= target) else ""
        if not reason:
            try:
                opened = datetime.fromisoformat(str(position["recorded_at"]).replace("Z", "+00:00"))
                if (now - opened).total_seconds() >= max_hold_minutes * 60:
                    reason = "MAX_HOLD"
            except (KeyError, ValueError):
                pass
        if reason:
            closes.append(ledger.close(str(position["proposal_id"]), price, reason))
    return closes


def main() -> None:
    if os.getenv("MULTI_ASSET_WORKER_ENABLED", "false").lower() != "true":
        raise SystemExit("MULTI_ASSET_WORKER_ENABLED is not true; refusing to start")
    feed_url = os.environ["MULTI_ASSET_FEED_URL"]
    token = os.getenv("MULTI_ASSET_FEED_TOKEN", "")
    interval = max(15, min(300, int(os.getenv("MULTI_ASSET_SCAN_INTERVAL_SECONDS", "60"))))
    ledger = PaperLedger(os.getenv("MULTI_ASSET_LEDGER_PATH", "/app/data/multi_asset.jsonl"))
    engine = MultiAssetEngine(ledger)
    HealthHandler.ledger = ledger
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            payload = fetch(feed_url, token)
            snapshots = list(payload.get("snapshots", []))
            closes = supervise(ledger, snapshots, max(15, int(os.getenv("ASSET_MAX_HOLD_MINUTES", "1440"))))
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
