from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

LOCK = threading.RLock()
STATE = {"ok": False, "observed_at": "", "source_url": "", "events": [], "error": "not scanned"}

TRADING_ECONOMICS_SOURCE = "https://api.tradingeconomics.com/calendar"
FRED_CALENDAR = "https://fred.stlouisfed.org/releases/calendar?od=asc&rid={rid}&ve={end}&view=month&vs={start}"
OFFICIAL_COMPOSITE_SOURCE = "https://fred.stlouisfed.org/releases/calendar"
BLS_ARCHIVE_SOURCE = "https://www.bls.gov/schedule/news_release/bls.ics"
OFFICIAL_SOURCES = {
    # FRED is operated by the Federal Reserve Bank of St. Louis and republishes
    # source-supplied release dates. These replace the BLS ICS endpoint, which
    # rejects Railway egress with HTTP 403.
    "fred_employment": ("USD", FRED_CALENDAR.format(rid=50, start="{start}", end="{end}")),
    "fred_cpi": ("USD", FRED_CALENDAR.format(rid=10, start="{start}", end="{end}")),
    "fred_ppi": ("USD", FRED_CALENDAR.format(rid=46, start="{start}", end="{end}")),
    "federal_reserve": ("USD", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    "ecb": ("EUR", "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"),
    "bank_of_england": ("GBP", "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"),
    "bank_of_japan": ("JPY", "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"),
}
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
    if provider == "official_composite":
        return "official-composite://calendar", headers, OFFICIAL_COMPOSITE_SOURCE, provider
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


def _utc(year: int, month: int, day: int, hour: int, minute: int, zone: str) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _event(currency: str, title: str, stamp: datetime, source: str,
           blackout_before: int = 30, blackout_after: int = 30) -> dict:
    return {"currency": currency, "impact": "HIGH", "event": title[:300],
            "time": stamp.astimezone(timezone.utc).isoformat(), "source_url": source,
            "blackout_before_minutes": blackout_before,
            "blackout_after_minutes": blackout_after}


def _parse_ics_datetime(value: str, params: str) -> datetime:
    zone_match = re.search(r"TZID=([^;:]+)", params)
    zone_name = zone_match.group(1) if zone_match else "UTC"
    # BLS uses the legacy US-Eastern alias, which is not installed in every
    # slim container even when the canonical IANA zone is available.
    if zone_name == "US-Eastern": zone_name = "America/New_York"
    zone = ZoneInfo(zone_name) if zone_match else timezone.utc
    raw = value.strip()
    if len(raw) == 8:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=zone)
    if raw.endswith("Z"):
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=zone)


def parse_bls_ics(body: str) -> list[dict]:
    body = re.sub(r"\r?\n[ \t]", "", body)
    high = re.compile(r"consumer price|producer price|employment situation|job openings|"
                      r"import and export price|employment cost|productivity and costs", re.I)
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", body, re.S):
        summary = re.search(r"^SUMMARY(?:;[^:]*)?:(.*)$", block, re.M)
        stamp = re.search(r"^DTSTART(?P<params>;[^:]*)?:(?P<value>.*)$", block, re.M)
        if not summary or not stamp or not high.search(summary.group(1)): continue
        try:
            when = _parse_ics_datetime(stamp.group("value"), stamp.group("params") or "")
        except (ValueError, KeyError):
            continue
        events.append(_event("USD", summary.group(1).replace("\\,", ","), when,
                             BLS_ARCHIVE_SOURCE))
    return events


def parse_fomc_html(body: str, year: int) -> list[dict]:
    panel = re.search(rf">{year} FOMC Meetings</a>(.*?)(?=>{year + 1} FOMC Meetings</a>|</main>)", body, re.S | re.I)
    if not panel: return []
    events = []
    pattern = re.compile(r'fomc-meeting__month[^>]*><strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)', re.S)
    for month_name, days in pattern.findall(panel.group(1)):
        nums = re.findall(r"\d+", days)
        if not nums: continue
        try:
            day = int(nums[-1]); month = datetime.strptime(month_name.strip(), "%B").month
            when = _utc(year, month, day, 14, 0, "America/New_York")
        except ValueError:
            continue
        events.append(_event("USD", "FOMC monetary policy decision", when,
                             OFFICIAL_SOURCES["federal_reserve"][1], 60, 60))
    return events


def parse_ecb_html(body: str) -> list[dict]:
    events = []
    for raw_date, description in re.findall(r"<dt[^>]*>\s*([0-9]{2}/[0-9]{2}/[0-9]{4})\s*</dt>\s*<dd[^>]*>(.*?)</dd>", body, re.S | re.I):
        title = _text(description)
        if "monetary policy" not in title.lower() or "press conference" not in title.lower(): continue
        try:
            day, month, year = map(int, raw_date.split("/"))
            when = _utc(year, month, day, 14, 15, "Europe/Berlin")
        except ValueError:
            continue
        events.append(_event("EUR", "ECB monetary policy decision and press conference", when,
                             OFFICIAL_SOURCES["ecb"][1], 60, 90))
    return events


def parse_boe_html(body: str, year: int) -> list[dict]:
    section = re.search(rf">{year} confirmed dates</h2>(.*?)(?=<h2|</section>)", body, re.S | re.I)
    if not section: return []
    events = []
    for cell in re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>", section.group(1), re.S | re.I):
        label = _text(cell).replace("\xa0", " ")
        match = re.search(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday)\s+(\d{1,2})\s+([A-Za-z]+)", label)
        if not match: continue
        try:
            month = datetime.strptime(match.group(2), "%B").month
            when = _utc(year, month, int(match.group(1)), 12, 0, "Europe/London")
        except ValueError:
            continue
        events.append(_event("GBP", "Bank of England MPC decision", when,
                             OFFICIAL_SOURCES["bank_of_england"][1], 60, 60))
    return events


def parse_boj_html(body: str, year: int) -> list[dict]:
    section = re.search(rf'id="p{year}"[^>]*>(.*?)(?=id="p{year + 1}"|$)', body, re.S | re.I)
    if not section: return []
    events = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section.group(1), re.S | re.I):
        first = re.search(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if not first: continue
        label = _text(first.group(1))
        dates = re.findall(r"([A-Za-z]{3,9})\.?.*?(\d{1,2})\s*\([^)]*\)", label)
        if not dates: continue
        month_name, day_text = dates[-1]
        try:
            month = datetime.strptime(month_name[:3], "%b").month
            when = _utc(year, month, int(day_text), 12, 0, "Asia/Tokyo")
        except ValueError:
            continue
        events.append(_event("JPY", "Bank of Japan monetary policy decision window", when,
                             OFFICIAL_SOURCES["bank_of_japan"][1], 720, 360))
    return events


def parse_fred_calendar_html(body: str) -> list[dict]:
    events, current_date = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I):
        date_match = re.search(
            r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
            r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", _text(row), re.I)
        if date_match:
            try:
                current_date = datetime.strptime(
                    " ".join(date_match.group(2, 3, 4)), "%B %d %Y")
            except ValueError:
                current_date = None
            continue
        if current_date is None:
            continue
        time_match = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", _text(row), re.I)
        name_match = re.search(r'<a[^>]+href="/release\?rid=\d+"[^>]*>(.*?)</a>', row, re.S | re.I)
        if not time_match or not name_match:
            continue
        hour = int(time_match.group(1)) % 12 + (12 if time_match.group(3).lower() == "pm" else 0)
        when = _utc(current_date.year, current_date.month, current_date.day,
                    hour, int(time_match.group(2)), "America/Chicago")
        events.append(_event("USD", _text(name_match.group(1)), when,
                             "https://fred.stlouisfed.org/releases/calendar", 60, 60))
    return events


def _fetch_official(name: str, now: datetime) -> tuple[dict, list[dict]]:
    currency, url = OFFICIAL_SOURCES[name]
    if "{start}" in url:
        url = url.format(start=(now - timedelta(days=1)).date().isoformat(),
                         end=(now + timedelta(days=45)).date().isoformat())
    request = urllib.request.Request(url, headers={"Accept": "text/html,text/calendar", "User-Agent": "primus-economic-calendar/1.2"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8", "replace")
            status = getattr(response, "status", 200)
    except Exception as exc:
        raise RuntimeError(f"official calendar source {name} failed: {exc}") from exc
    if status != 200 or not body.strip(): raise ValueError(f"{name} returned an empty or unsuccessful response")
    if name.startswith("fred_"): events = parse_fred_calendar_html(body)
    elif name == "bls": events = parse_bls_ics(body)
    elif name == "federal_reserve": events = sum((parse_fomc_html(body, year) for year in {now.year, now.year + 1}), [])
    elif name == "ecb": events = parse_ecb_html(body)
    elif name == "bank_of_england": events = sum((parse_boe_html(body, year) for year in {now.year, now.year + 1}), [])
    else: events = sum((parse_boj_html(body, year) for year in {now.year, now.year + 1}), [])
    if not events:
        raise ValueError(f"{name} response contained no recognized calendar entries")
    return {"name": name, "currency": currency, "url": url, "ok": True, "event_count": len(events)}, events


def scan_official_composite(now: datetime | None = None) -> dict:
    clock = now or datetime.now(timezone.utc)
    required = {item.strip().upper() for item in os.getenv("OFFICIAL_CALENDAR_REQUIRED_CURRENCIES", "USD,EUR,GBP,JPY").split(",") if item.strip()}
    selected = [name for name, (currency, _) in OFFICIAL_SOURCES.items() if currency in required]
    # FRED throttles concurrent calendar requests from a shared cloud egress IP.
    # Fetch its small rolling windows sequentially and fetch other institutions
    # concurrently.
    fred_names = [name for name in selected if name.startswith("fred_")]
    other_names = [name for name in selected if not name.startswith("fred_")]
    fred_results = [_fetch_official(name, clock) for name in fred_names]
    with ThreadPoolExecutor(max_workers=len(other_names)) as pool:
        other_results = list(pool.map(lambda name: _fetch_official(name, clock), other_names))
    results = fred_results + other_results
    sources = [result[0] for result in results]
    coverage = {source["currency"] for source in sources if source["ok"]}
    if not required.issubset(coverage): raise ValueError(f"official calendar coverage missing: {sorted(required - coverage)}")
    lookahead = min(31, max(1, int(os.getenv("ECONOMIC_CALENDAR_LOOKAHEAD_DAYS", "7"))))
    floor, ceiling = clock - timedelta(days=1), clock + timedelta(days=lookahead)
    events = [event for _, rows in results for event in rows
              if floor <= datetime.fromisoformat(event["time"]) <= ceiling]
    events.sort(key=lambda event: event["time"])
    for event in events:
        stamp = datetime.fromisoformat(event["time"])
        event["minutes_until"] = round((stamp - clock).total_seconds() / 60)
    return {"observed_at": clock.isoformat(), "source_url": OFFICIAL_COMPOSITE_SOURCE,
            "provider": "official_composite", "coverage": sorted(coverage),
            "sources": sources, "events": events}


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
    provider = os.getenv("ECONOMIC_CALENDAR_PROVIDER", "generic").strip().lower()
    if provider == "official_composite":
        value = scan_official_composite()
    else:
        url, headers, public_source, provider = build_request()
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
            value = normalize(json.loads(response.read().decode()), public_source, provider)
    require_events = os.getenv("ECONOMIC_CALENDAR_REQUIRE_EVENTS", "true").lower() == "true"
    if not value["events"] and require_events and provider != "official_composite":
        raise ValueError("upstream returned no recognized high-impact events; calendar remains unverified")
    with LOCK: STATE.update(ok=True, error="", **value)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/events"}: self.send_error(404); return
        with LOCK: value = dict(STATE)
        if self.path == "/health":
            body = json.dumps({"ok": True, "service": "forex-economic-calendar",
                               "calendar_ready": value["ok"], "observed_at": value["observed_at"],
                               "error": value["error"]}).encode()
        else:
            body = json.dumps(value).encode()
        self.send_response(200 if self.path == "/health" or value["ok"] else 503); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*_): return


def main() -> None:
    if os.getenv("ECONOMIC_CALENDAR_ENABLED","false").lower() != "true": raise SystemExit("ECONOMIC_CALENDAR_ENABLED is not true")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"event": "ECONOMIC_CALENDAR_HTTP_READY", "host": "0.0.0.0", "port": port}), flush=True)
    interval=max(60,int(os.getenv("ECONOMIC_CALENDAR_INTERVAL_SECONDS","300")))
    while True:
        try: scan(); print(json.dumps({"event":"ECONOMIC_CALENDAR_SCAN","ok":True,"event_count":len(STATE["events"])}),flush=True)
        except Exception as exc:
            with LOCK: STATE.update(ok=False,error=str(exc)[:500])
            print(json.dumps({"event":"ECONOMIC_CALENDAR_ERROR","detail":str(exc)[:500]}),flush=True)
        time.sleep(interval)


if __name__ == "__main__": main()
