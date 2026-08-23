from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.RLock()
STATE = {"ok": False, "observed_at": "", "source_url": "", "events": [], "error": "not scanned"}


def normalize(payload: object, source_url: str) -> dict:
    rows = payload.get("events", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    events = []
    now = datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, dict): continue
        currency = str(row.get("currency") or row.get("country") or "").upper().strip()
        impact = str(row.get("impact") or row.get("importance") or "").upper().strip()
        stamp = row.get("time") or row.get("date") or row.get("timestamp")
        try:
            event_time = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if event_time.tzinfo is None: event_time = event_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if len(currency) != 3 or impact not in {"HIGH", "3", "CRITICAL"}: continue
        events.append({"currency": currency, "impact": "HIGH", "minutes_until": round((event_time-now).total_seconds()/60),
                       "event": str(row.get("event") or row.get("title") or "")[:300], "time": event_time.isoformat()})
    return {"observed_at": now.isoformat(), "source_url": source_url, "events": events}


def scan() -> None:
    url = os.environ["ECONOMIC_CALENDAR_UPSTREAM_URL"]
    headers = {"Accept":"application/json", "User-Agent":"primus-economic-calendar/1.0"}
    token = os.getenv("ECONOMIC_CALENDAR_UPSTREAM_TOKEN", "")
    if token: headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        value = normalize(json.loads(response.read().decode()), url)
    with LOCK: STATE.update(ok=True, error="", **value)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/events"}: self.send_error(404); return
        with LOCK: value = dict(STATE)
        body = json.dumps(value if self.path == "/events" else {"ok":value["ok"],"observed_at":value["observed_at"],"error":value["error"]}).encode()
        self.send_response(200 if value["ok"] else 503); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*_): return


def main() -> None:
    if os.getenv("ECONOMIC_CALENDAR_ENABLED","false").lower() != "true": raise SystemExit("ECONOMIC_CALENDAR_ENABLED is not true")
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","8080"))),Handler).serve_forever,daemon=True).start()
    interval=max(60,int(os.getenv("ECONOMIC_CALENDAR_INTERVAL_SECONDS","300")))
    while True:
        try: scan(); print(json.dumps({"event":"ECONOMIC_CALENDAR_SCAN","ok":True,"event_count":len(STATE["events"])}),flush=True)
        except Exception as exc:
            with LOCK: STATE.update(ok=False,error=str(exc)[:500])
            print(json.dumps({"event":"ECONOMIC_CALENDAR_ERROR","detail":str(exc)[:500]}),flush=True)
        time.sleep(interval)


if __name__ == "__main__": main()
