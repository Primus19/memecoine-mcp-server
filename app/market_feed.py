from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .broker_adapters import OandaAdapter

LOCK = threading.RLock()
STATE = {"ok": False, "scanned_at": "", "snapshots": [], "error": "not scanned"}


def pct(a: float, b: float) -> float: return 0.0 if a == 0 else (b - a) / a * 100


def calendar_evidence(symbol: str) -> dict:
    url = os.getenv("ECONOMIC_CALENDAR_URL", "").strip()
    if not url:
        return {"minutes": int(os.getenv("FOREX_DEFAULT_EVENT_DISTANCE_MINUTES", "0")), "verified": False, "source": ""}
    headers = {"Accept": "application/json", "User-Agent": "primus-forex-calendar/1.0"}
    token = os.getenv("ECONOMIC_CALENDAR_BEARER_TOKEN", "")
    if token: headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        payload = json.loads(response.read().decode())
    observed = datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - observed).total_seconds() > int(os.getenv("ECONOMIC_CALENDAR_MAX_AGE_SECONDS", "300")):
        raise ValueError("economic calendar stale")
    source = str(payload.get("source_url", ""))
    if not source.startswith("https://"): raise ValueError("economic calendar source missing")
    currencies = set(symbol.split("_")); distances = []
    for event in payload.get("events", []):
        if str(event.get("currency", "")).upper() in currencies and str(event.get("impact", "")).upper() == "HIGH":
            minutes = int(event.get("minutes_until", 0))
            before = max(0, int(event.get("blackout_before_minutes", 0)))
            after = max(0, int(event.get("blackout_after_minutes", 0)))
            if -after <= minutes <= before: distances.append(0)
            elif minutes > before: distances.append(minutes - before)
            else: distances.append(abs(minutes + after))
    return {"minutes": min(distances) if distances else 10080, "verified": True, "source": source}


def forex_snapshot(adapter: OandaAdapter, symbol: str) -> dict:
    h1 = [c for c in adapter.candles(symbol, "H1", 30) if c.get("complete")]
    d1 = [c for c in adapter.candles(symbol, "D", 3) if c.get("complete")]
    quote = adapter.price(symbol); bids = quote.get("bids", []); asks = quote.get("asks", [])
    if len(h1) < 25 or len(d1) < 2 or not bids or not asks: raise ValueError("insufficient broker market data")
    closes = [float(c["mid"]["c"]) for c in h1]
    bid, ask = float(bids[0]["price"]), float(asks[0]["price"]); mid = (bid + ask) / 2
    change_1h = pct(closes[-2], closes[-1]); change_24h = pct(closes[-25], closes[-1])
    trend = (closes[-1] - statistics.mean(closes[-20:])) / max(abs(closes[-1]) * .01, 1e-9)
    # Event distance is fail-closed unless an independently normalized calendar service attests it.
    calendar = calendar_evidence(symbol); event_minutes = calendar["minutes"]
    return {"asset_class": "FOREX", "symbol": symbol, "price": mid,
            "spread_bps": (ask - bid) / mid * 10000, "tradable": quote.get("status") == "tradeable",
            "market_veto": event_minutes < 30, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_urls": [f"https://developer.oanda.com/rest-live-v20/pricing-ep/"],
            "change_1h_pct": change_1h, "change_24h_pct": change_24h,
            "trend_strength": max(-1, min(1, trend)), "liquidity_score": 1.0,
            "session_liquid": quote.get("status") == "tradeable", "economic_event_within_minutes": event_minutes,
            "calendar_verified": calendar["verified"], "economic_event_source": calendar["source"],
            "stop_distance": max(abs(closes[-1] - statistics.mean(closes[-10:])), mid * .0025),
            "maximum_loss_usd": float(os.getenv("FOREX_PAPER_MAX_LOSS_USD", "2.50")),
            "reward_multiple": 2.0, "expiry_seconds": 300,
            "thesis": "Broker-attested liquid-session trend continuation",
            "invalidation": "Trend alignment, spread, session liquidity or economic-event gate fails"}


def scan_symbols(adapter: OandaAdapter, symbols: list[str]) -> tuple[list[dict], list[dict]]:
    snapshots, rejected = [], []
    for symbol in symbols:
        try:
            snapshots.append(forex_snapshot(adapter, symbol))
        except Exception as exc:
            rejected.append({"symbol": symbol, "reason": str(exc)[:300]})
    if not snapshots:
        detail = "; ".join(f"{item['symbol']}: {item['reason']}" for item in rejected)
        raise RuntimeError(f"no valid forex snapshots ({detail or 'no symbols configured'})")
    return snapshots, rejected


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/snapshots", "/status"}: self.send_error(404); return
        with LOCK: value = dict(STATE)
        if self.path == "/health": value = {"ok": value["ok"], "service": "multi-asset-market-feed", "scanned_at": value["scanned_at"], "error": value["error"]}
        elif self.path == "/snapshots": value = {"snapshots": value["snapshots"], "scanned_at": value["scanned_at"]}
        body = json.dumps(value).encode(); self.send_response(200 if value.get("ok", True) else 503)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): return


def main():
    if os.getenv("MULTI_ASSET_FEED_ENABLED", "false").lower() != "true": raise SystemExit("MULTI_ASSET_FEED_ENABLED is not true")
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever, daemon=True).start()
    interval = max(30, int(os.getenv("MULTI_ASSET_FEED_INTERVAL_SECONDS", "60")))
    symbols = [s.strip().upper() for s in os.getenv("FOREX_SYMBOLS", "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CAD").split(",") if s.strip()]
    while True:
        try:
            adapter = OandaAdapter()
            snapshots, rejected = scan_symbols(adapter, symbols)
            for item in rejected:
                print(json.dumps({"event": "FOREX_SYMBOL_REJECTED", **item}), flush=True)
            with LOCK: STATE.update(ok=True, scanned_at=datetime.now(timezone.utc).isoformat(), snapshots=snapshots, error="")
            print(json.dumps({"event": "MULTI_ASSET_FEED_SCAN", "paper_only": True, "snapshot_count": len(snapshots)}), flush=True)
        except Exception as exc:
            with LOCK: STATE.update(ok=False, error=str(exc)[:500])
            print(json.dumps({"event": "MULTI_ASSET_FEED_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__": main()
