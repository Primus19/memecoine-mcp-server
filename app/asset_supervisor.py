from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .worker_observability import monitoring_health

class Handler(BaseHTTPRequestHandler):
    state={"ok":False,"last_check":"","last_error":"not checked","worker":{}}
    def do_GET(self):
        if self.path not in {"/health", "/status"}: self.send_error(404); return
        body=json.dumps({"service":"multi-asset-position-supervisor","paper_only":True,
                         **self.state}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*_): return


def fetch(url: str) -> dict:
    headers = {"Accept": "application/json"}
    token = os.getenv("MULTI_ASSET_REPORT_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response: return json.loads(response.read())


def main():
    if os.getenv("MULTI_ASSET_SUPERVISOR_ENABLED", "false").lower() != "true": raise SystemExit("MULTI_ASSET_SUPERVISOR_ENABLED is not true")
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT","8080"))),Handler).serve_forever,daemon=True).start()
    # The worker owns and supervises its ledger. This service independently
    # checks that worker over Railway private networking instead of opening a
    # second, divergent local ledger.
    url=os.getenv("MULTI_ASSET_WORKER_URL", "http://multi-asset-paper-worker.railway.internal:8080/report.json")
    interval=max(10,int(os.getenv("MULTI_ASSET_SUPERVISION_INTERVAL_SECONDS","20")))
    while True:
        try:
            payload=fetch(url)
            Handler.state={"ok":bool(payload.get("ok")) and monitoring_health(payload)["ok"],"last_check":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                           "last_error":"","worker":{"open_position_count":len(payload.get("open_positions") or []),
                           "open_positions":payload.get("open_positions") or [],
                           "closed":payload.get("closed",0),"realized_pnl_usd":payload.get("realized_pnl_usd",0),
                           "last_scan":payload.get("last_scan","")}}
            print(json.dumps({"event":"MULTI_ASSET_SUPERVISION","paper_only":True,**Handler.state["worker"]}),flush=True)
        except Exception as exc:
            Handler.state={**Handler.state,"ok":False,"last_check":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                           "last_error":f"{type(exc).__name__}: {str(exc)[:500]}"}
            print(json.dumps({"event":"MULTI_ASSET_SUPERVISION_ERROR","error":type(exc).__name__,"detail":str(exc)[:500]}),flush=True)
        time.sleep(interval)


if __name__=="__main__": main()
