from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.RLock()
STATE = {"ok": False, "observed_at": "", "source_url": "", "events": [], "error": "not scanned"}

TRADING_ECONOMICS_SOURCE = "https://api.tradingeconomics.com/calendar"
COUNTRY_CURRENCY = {
    "australia": "AUD", "canada": "CAD", "euro area": "EUR",
    "european union": "EUR", "france": "EUR", "germany": "EUR",
    "italy": "EUR", "japan": "JPY", "new zealand": "NZD",
    "switzerland": "CHF", "united kingdom": "GBP", "united states": "USD",
}


def _row_value(row: dict, *names: str) -> object:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return ""


def build_request(now: datetime | None = None) -> tuple[str, dict[str, str], str, str]:
    """Build an upstream request while keeping credentials out of published evidence."""
    provider = os.getenv("ECONOMIC_CALENDAR_PROVIDER", "generic").strip().lower()
    headers = {"Accept": "application/json", "User-Agent": "primus-economic-calendar/1.1"}
    if provider == "trading_economics":
        key = os.environ["TRADING_ECONOMICS_API_KEY"].strip()
        if not key:
            raise ValueError("TRADING_ECONOMICS_API_KEY is empty")
        countries = [item.strip().lower() for item in os.getenv(
            "TRADING_ECONOMICS_COUNTRIES",
            "united states,euro area,united kingdom,japan,canada,australia",
        ).split(",") if item.strip()]
        if not countries:
            raise ValueError("TRADING_ECONOMICS_COUNTRIES is empty")
        clock = now or datetime.now(timezone.utc)
        lookahead = min(14, max(1, int(os.getenv("ECONOMIC_CALENDAR_LOOKAHEAD_DAYS", "7"))))
        start, end = clock.date().isoformat(), (clock.date() + timedelta(days=lookahead)).isoformat()
        encoded = urllib.parse.quote(",".join(countries), safe=",")
        query = urllib.parse.urlencode({"c": key, "importance": "3", "f": "json"})
        url = f"https://api.tradingeconomics.com/calendar/country/{encoded}/{start}/{end}?{query}"
        return url, headers, TRADING_ECONOMICS_SOURCE, provider
    url = os.environ["ECONOMIC_CALENDAR_UPSTREAM_URL"].strip()
    if not url.startswith("https://"):
        raise ValueError("ECONOMIC_CALENDAR_UPSTREAM_URL must use HTTPS")
    token = os.getenv("ECONOMIC_CALENDAR_UPSTREAM_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    parsed = urllib.parse.urlsplit(url)
    public_source = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return url, headers, public_source, provider


def normalize(payload: object, source_url: str, provider: str = "generic") -> dict:
    if isinstance(payload, dict):
        rows = payload.get("events", payload.get("economicCalendar", []))
    else:
        rows = payload if isinstance(payload, list) else []
    events = []
    now = datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, dict): continue
        raw_currency = str(_row_value(row, "currency", "Currency")).upper().strip()
        raw_country = str(_row_value(row, "country", "Country")).lower().strip()
        currency = raw_currency if len(raw_currency) == 3 else COUNTRY_CURRENCY.get(raw_country, "")
        impact = str(_row_value(row, "impact", "importance", "Importance")).upper().strip()
        stamp = _row_value(row, "time", "date", "timestamp", "Date", "datetime")
        try:
            event_time = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if event_time.tzinfo is None: event_time = event_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if len(currency) != 3 or impact not in {"HIGH", "3", "CRITICAL"}: continue
        events.append({"currency": currency, "impact": "HIGH", "minutes_until": round((event_time-now).total_seconds()/60),
                       "event": str(_row_value(row, "event", "title", "Event", "Category"))[:300],
                       "time": event_time.astimezone(timezone.utc).isoformat()})
    return {"observed_at": now.isoformat(), "source_url": source_url, "events": events}


def scan() -> None:
    url, headers, public_source, provider = build_request()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        value = normalize(json.loads(response.read().decode()), public_source, provider)
    if not value["events"] and os.getenv("ECONOMIC_CALENDAR_REQUIRE_EVENTS", "true").lower() == "true":
        raise ValueError("upstream returned no recognized high-impact events; calendar remains unverified")
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
