from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .multi_asset import PaperLedger


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health": self.send_error(404); return
        body=b'{"ok":true,"service":"multi-asset-position-supervisor","paper_only":true}'
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*_): return


def fetch(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept":"application/json"}), timeout=20) as response: return json.loads(response.read())


def main():
    if os.getenv("MULTI_ASSET_SUPERVISOR_ENABLED", "false").lower() != "true": raise SystemExit("MULTI_ASSET_SUPERVISOR_ENABLED is not true")
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT","8080"))),Handler).serve_forever,daemon=True).start()
    ledger=PaperLedger(os.getenv("MULTI_ASSET_LEDGER_PATH","/app/data/multi_asset.jsonl")); url=os.environ["MULTI_ASSET_FEED_URL"]
    interval=max(10,int(os.getenv("MULTI_ASSET_SUPERVISION_INTERVAL_SECONDS","20")))
    while True:
        try:
            payload=fetch(url); marks={str(s.get("symbol")):float(s.get("price",0)) for s in payload.get("snapshots",[])}; outcomes=[]
            for position in ledger.positions():
                price=marks.get(position["symbol"])
                if not price: continue
                side=position["side"]; stop=float(position["stop_price"]); target=float(position["target_price"])
                reason="STOP" if (side=="BUY" and price<=stop) or (side=="SELL" and price>=stop) else "TARGET" if (side=="BUY" and price>=target) or (side=="SELL" and price<=target) else ""
                if reason: outcomes.append(ledger.close(position["proposal_id"],price,reason))
            print(json.dumps({"event":"MULTI_ASSET_SUPERVISION","paper_only":True,"open_positions":len(ledger.positions()),"closes":len(outcomes)}),flush=True)
        except Exception as exc: print(json.dumps({"event":"MULTI_ASSET_SUPERVISION_ERROR","error":type(exc).__name__,"detail":str(exc)[:500]}),flush=True)
        time.sleep(interval)


if __name__=="__main__": main()
