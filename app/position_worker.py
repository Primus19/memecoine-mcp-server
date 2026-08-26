from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def request_json(url: str, token: str, *, payload: dict | None = None) -> dict:
    headers={"Authorization":f"Bearer {token}","Accept":"application/json","User-Agent":"primus-position-supervisor/1.0"};data=None
    if payload is not None:data=json.dumps(payload).encode();headers["Content-Type"]="application/json"
    request=urllib.request.Request(url,data=data,headers=headers,method="POST" if data else "GET")
    with urllib.request.urlopen(request,timeout=20) as response:return json.loads(response.read().decode())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!="/health":self.send_error(404);return
        body=b'{"ok":true,"service":"memecoin-position-supervisor"}';self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*_):return


def main() -> None:
    if os.getenv("POSITION_SUPERVISOR_ENABLED","false").lower()!="true":raise SystemExit("POSITION_SUPERVISOR_ENABLED is not true; refusing to start")
    executor=os.environ["EXECUTOR_BASE_URL"].rstrip("/");executor_token=os.environ["REST_API_TOKEN"]
    research=os.environ["RESEARCH_FEED_URL"].rstrip("/");feed_token=os.environ["SIGNAL_FEED_BEARER_TOKEN"]
    interval=min(60,max(5,int(os.getenv("POSITION_SUPERVISION_INTERVAL_SECONDS","15"))))
    server=ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","8080"))),HealthHandler);threading.Thread(target=server.serve_forever,daemon=True).start()
    while True:
        try:
            feed=request_json(research+"/status",feed_token);feed_status=feed.get("status",{});stamp=str(feed_status.get("scanned_at", ""))
            observed=datetime.fromisoformat(stamp.replace("Z","+00:00"));age=(datetime.now(timezone.utc)-observed).total_seconds()
            regime=str(feed_status.get("regime",{}).get("classification", "")) if -5<=age<=120 else ""
            position=request_json(executor+"/api/pilot-status",executor_token).get("portfolio",{}).get("open_position") or {}
            product_id=str(position.get("product_id") or "")
            ranked=feed_status.get("ranked_candidates") or []
            match=next((item for item in ranked if item.get("product_id")==product_id),None)
            momentum=None if not match else match.get("change_1h_pct")
            result=request_json(executor+"/api/position-supervision",executor_token,payload={"regime":regime,"momentum_1h_pct":momentum,"observed_at":datetime.now(timezone.utc).isoformat()})
            print(json.dumps({"event":"POSITION_SUPERVISION","regime":regime,"result":result},default=str),flush=True)
        except Exception as exc:print(json.dumps({"event":"POSITION_SUPERVISION_ERROR","error":type(exc).__name__,"detail":str(exc)[:500]}),flush=True)
        time.sleep(interval)


if __name__=="__main__":main()
