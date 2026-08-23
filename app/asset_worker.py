from __future__ import annotations

import json
import os
import time
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .multi_asset import MultiAssetEngine, MultiAssetRejected, PaperLedger


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404); return
        body = b'{"ok":true,"service":"multi-asset-paper-worker","paper_only":true}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): return


def fetch(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "primus-multi-asset-paper/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        value = json.loads(response.read().decode())
        return value if isinstance(value, dict) else {}


def main() -> None:
    if os.getenv("MULTI_ASSET_WORKER_ENABLED", "false").lower() != "true":
        raise SystemExit("MULTI_ASSET_WORKER_ENABLED is not true; refusing to start")
    feed_url = os.environ["MULTI_ASSET_FEED_URL"]
    token = os.getenv("MULTI_ASSET_FEED_TOKEN", "")
    interval = max(15, min(300, int(os.getenv("MULTI_ASSET_SCAN_INTERVAL_SECONDS", "60"))))
    engine = MultiAssetEngine(PaperLedger(os.getenv("MULTI_ASSET_LEDGER_PATH", "/app/data/multi_asset.jsonl")))
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            payload = fetch(feed_url, token)
            outcomes = []
            for snapshot in payload.get("snapshots", []):
                try:
                    result = engine.process(snapshot)
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "PAPER_FILL", "event_id": result["event_id"]})
                except MultiAssetRejected as exc:
                    outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)})
            print(json.dumps({"event": "MULTI_ASSET_SCAN", "paper_only": True, "outcomes": outcomes}), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "MULTI_ASSET_SCAN_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
