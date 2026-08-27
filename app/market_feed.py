from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .broker_adapters import OandaAdapter
from .quant import average_true_range, ewma_volatility, horizon_return, liquidity_quality, multi_horizon_consensus

LOCK = threading.RLock()
STATE = {"ok": False, "scanned_at": "", "snapshots": [], "error": "not scanned"}
SPREAD_HISTORY: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=240))
CORE_FOREX_SYMBOLS = (
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
    "USD_CHF", "NZD_USD", "EUR_JPY", "GBP_JPY", "EUR_GBP", "XAU_USD", "XAG_USD",
)


def pct(a: float, b: float) -> float: return 0.0 if a == 0 else (b - a) / a * 100


def configured_symbols(value: str | None = None) -> list[str]:
    """Keep the approved liquid core and allow operators to append more pairs."""
    extras = [s.strip().upper() for s in (value if value is not None else os.getenv("FOREX_SYMBOLS", "")).split(",") if s.strip()]
    return list(dict.fromkeys((*CORE_FOREX_SYMBOLS, *extras)))


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
    m5 = [c for c in adapter.candles(symbol, "M5", 15) if c.get("complete")]
    h1 = [c for c in adapter.candles(symbol, "H1", 120) if c.get("complete")]
    h4 = [c for c in adapter.candles(symbol, "H4", 90) if c.get("complete")]
    d1 = [c for c in adapter.candles(symbol, "D", 35) if c.get("complete")]
    quote = adapter.price(symbol); bids = quote.get("bids", []); asks = quote.get("asks", [])
    if len(m5) < 6 or len(h1) < 100 or len(h4) < 30 or len(d1) < 20 or not bids or not asks: raise ValueError("insufficient broker market data")
    closes = [float(c["mid"]["c"]) for c in h1]
    h4_closes = [float(c["mid"]["c"]) for c in h4]
    d1_closes = [float(c["mid"]["c"]) for c in d1]
    bid, ask = float(bids[0]["price"]), float(asks[0]["price"]); mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000
    history = SPREAD_HISTORY[symbol]
    median_spread = statistics.median(history) if history else spread_bps
    history.append(spread_bps)
    bid_liquidity = sum(float(item.get("liquidity") or 0) for item in bids[:4])
    ask_liquidity = sum(float(item.get("liquidity") or 0) for item in asks[:4])
    try:
        quote_time = datetime.fromisoformat(str(quote["time"]).replace("Z", "+00:00"))
        quote_age = max(0.0, (datetime.now(timezone.utc) - quote_time).total_seconds())
    except Exception:
        quote_age = 9999.0
    change_1h = horizon_return(closes, 1); change_4h = horizon_return(h4_closes, 1)
    change_24h = horizon_return(closes, 24)
    change_5d = horizon_return(h4_closes, 30); change_20d = horizon_return(d1_closes, 20)
    consensus = multi_horizon_consensus((change_1h, change_4h, change_24h, change_5d, change_20d), (.10, .15, .25, .25, .25))
    trend = (closes[-1] - statistics.mean(closes[-20:])) / max(abs(closes[-1]) * .01, 1e-9)
    atr = average_true_range(h1, 14)
    ewma = ewma_volatility(closes[-60:]) * mid
    stop_distance = max(1.5 * atr, 2.0 * ewma, mid * .0015)
    instrument = adapter.instrument(symbol)
    financing = instrument.get("financing") or {}
    # Event distance is fail-closed unless an independently normalized calendar service attests it.
    calendar = calendar_evidence(symbol); event_minutes = calendar["minutes"]
    five_streak_candles = [{"time": str(c.get("time") or ""),
                            "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
                            "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in m5[-12:]]
    return {"asset_class": "FOREX", "symbol": symbol, "price": mid, "bid": bid, "ask": ask,
            "five_streak_candles": five_streak_candles,
            "spread_bps": spread_bps, "median_spread_bps": median_spread,
            "bid_liquidity": bid_liquidity, "ask_liquidity": ask_liquidity,
            "quote_age_seconds": quote_age, "tradable": quote.get("status") == "tradeable",
            "market_veto": event_minutes < 30, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_urls": [f"https://developer.oanda.com/rest-live-v20/pricing-ep/"],
            "change_1h_pct": change_1h, "change_4h_pct": change_4h, "change_24h_pct": change_24h,
            "change_5d_pct": change_5d, "change_20d_pct": change_20d,
            "horizon_direction": consensus["direction"], "horizon_agreement": consensus["agreement"],
            "trend_strength": max(-1, min(1, trend)),
            "liquidity_score": liquidity_quality(spread_bps=spread_bps, median_spread_bps=median_spread,
                                                 bid_liquidity=bid_liquidity, ask_liquidity=ask_liquidity,
                                                 quote_age_seconds=quote_age),
            "session_liquid": quote.get("status") == "tradeable" and quote_age <= 10,
            "economic_event_within_minutes": event_minutes,
            "calendar_verified": calendar["verified"], "economic_event_source": calendar["source"],
            "atr_14": atr, "ewma_volatility_price": ewma, "stop_distance": stop_distance,
            "long_financing_rate": float(financing.get("longRate") or 0),
            "short_financing_rate": float(financing.get("shortRate") or 0),
            "financing_days": financing.get("financingDaysOfWeek") or [],
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
    symbols = configured_symbols()
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
