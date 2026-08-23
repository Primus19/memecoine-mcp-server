from __future__ import annotations

import email.utils
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


UTC = timezone.utc
EVM_CHAINS = {
    "ethereum": "1",
    "binance-smart-chain": "56",
    "polygon-pos": "137",
    "arbitrum-one": "42161",
    "avalanche": "43114",
    "base": "8453",
}
DEFAULT_NEWS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
)
POSITIVE_EVENTS = {
    "listing", "lists", "listed", "launch", "upgrade", "integration",
    "partnership", "approval", "adoption", "buyback", "burn", "mainnet",
}
VETO_EVENTS = {
    "exploit", "exploited", "hack", "hacked", "rug pull", "honeypot",
    "delist", "delisted", "compromise", "breach", "chain halt",
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return now_utc().isoformat()


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def parse_date(value: str) -> datetime | None:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.astimezone(UTC)
    except Exception:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return None


def article_mentions(article: dict[str, Any], *, name: str, symbol: str) -> bool:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    if name and name.lower() in text:
        return True
    return len(symbol) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])", text) is not None


def score_news(articles: list[dict[str, Any]], *, name: str, symbol: str) -> tuple[int, bool, list[str]]:
    relevant = [item for item in articles if article_mentions(item, name=name, symbol=symbol)]
    positive_hosts, veto_hosts, urls = set(), set(), []
    for item in relevant:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        host = urlparse(str(item.get("link", ""))).hostname or str(item.get("source", ""))
        if any(term in text for term in VETO_EVENTS):
            veto_hosts.add(host)
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in POSITIVE_EVENTS):
            positive_hosts.add(host)
            if str(item.get("link", "")).startswith("https://"):
                urls.append(str(item["link"]))
    veto = len(veto_hosts) >= 2
    score = 0 if veto else (6 if len(positive_hosts) >= 3 else 4 if len(positive_hosts) >= 2 else 0)
    return score, veto, list(dict.fromkeys(urls))


def evaluate_goplus(payload: dict[str, Any]) -> tuple[bool, int, list[str]]:
    critical = ("is_honeypot", "is_blacklisted", "cannot_sell_all", "is_mintable", "hidden_owner")
    missing = [key for key in critical if key not in payload]
    failures = list(missing)
    for key in critical:
        if truthy(payload.get(key, "0")):
            failures.append(key)
    for key in ("buy_tax", "sell_tax"):
        try:
            if float(payload.get(key, 0) or 0) > 0.05:
                failures.append(key)
        except (TypeError, ValueError):
            failures.append(key)
    holders = payload.get("holders")
    if not isinstance(holders, list) or not holders:
        failures.append("holders")
    else:
        concentration = sum(float(item.get("percent") or 0) for item in holders[:10])
        if concentration > 0.50:
            failures.append("top10_concentration")
    clean = not failures
    return clean, 15 if clean else 0, failures


def evaluate_goplus_solana(payload: dict[str, Any]) -> tuple[bool, int, list[str]]:
    """Fail closed on the documented Solana token-control and holder risks."""
    failures: list[str] = []
    for key in ("mintable", "freezable", "closable", "balance_mutable_authority"):
        value = payload.get(key)
        if not isinstance(value, dict) or "status" not in value:
            failures.append(key)
        elif truthy(value.get("status")):
            failures.append(key)
    if truthy(payload.get("non_transferable", "0")):
        failures.append("non_transferable")
    if str(payload.get("default_account_state", "1")) != "1":
        failures.append("default_account_state")
    transfer_hook = payload.get("transfer_hook")
    if isinstance(transfer_hook, dict) and transfer_hook:
        failures.append("transfer_hook")
    transfer_fee = payload.get("transfer_fee")
    if isinstance(transfer_fee, dict):
        current = transfer_fee.get("current_fee_rate") or {}
        try:
            if float(current.get("fee_rate") or 0) > 500:
                failures.append("transfer_fee")
        except (TypeError, ValueError):
            failures.append("transfer_fee")
    creators = payload.get("creators") or payload.get("creator") or []
    if any(truthy(item.get("malicious_address")) for item in creators if isinstance(item, dict)):
        failures.append("malicious_creator")
    holders = payload.get("holders")
    if not isinstance(holders, list) or not holders:
        failures.append("holders")
    else:
        try:
            concentration = sum(float(item.get("percent") or 0) for item in holders[:10])
            if concentration > 0.50:
                failures.append("top10_concentration")
        except (TypeError, ValueError):
            failures.append("holders")
    failures = list(dict.fromkeys(failures))
    return not failures, 15 if not failures else 0, failures


def canonical_contract(detail: dict[str, Any]) -> tuple[str, str]:
    """Return only CoinGecko's canonical platform/address, never a bridged guess."""
    platform = str(detail.get("asset_platform_id") or "")
    platforms = detail.get("platforms") or {}
    address = str(platforms.get(platform) or "") if isinstance(platforms, dict) else ""
    if platform and address:
        return platform, address
    supported = [
        (str(candidate), str(value))
        for candidate, value in (platforms.items() if isinstance(platforms, dict) else [])
        if candidate in {*EVM_CHAINS, "solana"} and value
    ]
    return supported[0] if len(supported) == 1 else ("", "")


class EvidenceAdapter:
    def __init__(self) -> None:
        self.executor_url = os.environ["EXECUTOR_BASE_URL"].rstrip("/")
        self.executor_token = os.environ["REST_API_TOKEN"]
        self.research_url = os.environ["RESEARCH_FEED_URL"].rstrip("/")
        self.feed_token = os.environ["SIGNAL_FEED_BEARER_TOKEN"]
        self.cg_base = os.getenv("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3").rstrip("/")
        self.cg_key = os.getenv("COINGECKO_API_KEY", "")
        configured = [value.strip() for value in os.getenv("EVIDENCE_NEWS_RSS_URLS", "").split(",") if value.strip()]
        self.news_feeds = tuple(configured) or DEFAULT_NEWS_FEEDS
        self.interval = min(900, max(60, int(os.getenv("EVIDENCE_SCAN_INTERVAL_SECONDS", "300"))))
        self.request_spacing = max(0.5, float(os.getenv("EVIDENCE_REQUEST_SPACING_SECONDS", "2.5")))
        self.max_retries = min(5, max(1, int(os.getenv("EVIDENCE_HTTP_MAX_RETRIES", "3"))))
        self.status: dict[str, Any] = {"ok": False}
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.last_request_by_host: dict[str, float] = {}
        self.detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def throttle(self, url: str) -> None:
        host = urlparse(url).hostname or "unknown"
        with self.request_lock:
            wait = self.request_spacing - (time.monotonic() - self.last_request_by_host.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_request_by_host[host] = time.monotonic()

    def fetch(self, url: str, token: str = "") -> bytes:
        headers = {"Accept": "application/json, application/rss+xml, application/xml", "User-Agent": "primus-evidence-adapter/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.cg_key and url.startswith(self.cg_base):
            headers["x-cg-demo-api-key"] = self.cg_key
        for attempt in range(self.max_retries):
            self.throttle(url)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt + 1 >= self.max_retries:
                    raise
                try:
                    retry_after = float(exc.headers.get("Retry-After", "0") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                time.sleep(min(60, max(retry_after, 5 * (attempt + 1))))
        raise RuntimeError("HTTP retry loop exited unexpectedly")

    def json(self, url: str, token: str = "") -> Any:
        return json.loads(self.fetch(url, token).decode())

    def post(self, url: str, payload: dict[str, Any], token: str) -> Any:
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
        }, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())

    def market_page(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "vs_currency": "usd", "category": "meme-token", "order": "market_cap_desc",
            "per_page": 250, "page": 1, "sparkline": "false", "price_change_percentage": "1h,24h,7d",
        })
        result = self.json(f"{self.cg_base}/coins/markets?{query}")
        return result if isinstance(result, list) else []

    def news(self) -> list[dict[str, Any]]:
        cutoff = now_utc() - timedelta(hours=72)
        result = []
        for feed_url in self.news_feeds:
            try:
                root = ET.fromstring(self.fetch(feed_url))
                for item in root.findall(".//item"):
                    published = parse_date(item.findtext("pubDate", ""))
                    if not published or published < cutoff:
                        continue
                    result.append({
                        "title": item.findtext("title", ""), "summary": item.findtext("description", ""),
                        "link": item.findtext("link", ""), "source": urlparse(feed_url).hostname or feed_url,
                    })
            except Exception:
                continue
        return result

    def coin_detail(self, coin_id: str) -> dict[str, Any]:
        cached = self.detail_cache.get(coin_id)
        if cached and time.time() - cached[0] <= 6 * 3600:
            return cached[1]
        query = urllib.parse.urlencode({"localization": "false", "tickers": "false", "market_data": "false", "community_data": "false", "developer_data": "false"})
        value = self.json(f"{self.cg_base}/coins/{urllib.parse.quote(coin_id)}?{query}")
        result = value if isinstance(value, dict) else {}
        if result:
            self.detail_cache[coin_id] = (time.time(), result)
        return result

    def security(self, detail: dict[str, Any]) -> tuple[bool, int, list[str], str]:
        platform, address = canonical_contract(detail)
        if not platform or not address:
            return False, 0, ["canonical contract platform unavailable"], ""
        query = urllib.parse.urlencode({"contract_addresses": address})
        if platform == "solana":
            response = self.json(f"https://api.gopluslabs.io/api/v1/solana/token_security?{query}")
        elif platform in EVM_CHAINS:
            response = self.json(f"https://api.gopluslabs.io/api/v1/token_security/{EVM_CHAINS[platform]}?{query}")
        else:
            return False, 0, [f"unsupported canonical contract platform: {platform}"], address
        result = response.get("result") or {}
        facts = result.get(address.lower()) or result.get(address) or {}
        if not facts and len(result) == 1:
            facts = next(iter(result.values()))
        clean, score, failures = evaluate_goplus_solana(facts) if platform == "solana" else evaluate_goplus(facts)
        return clean, score, failures, address

    def scan_once(self) -> dict[str, Any]:
        started = now_utc()
        products = self.json(f"{self.executor_url}/api/eligible-products", self.executor_token).get("products", [])
        eligible = {str(item.get("product_id", "")) for item in products}
        markets = self.market_page()
        symbol_counts: dict[str, int] = {}
        for market in markets:
            symbol = str(market.get("symbol", "")).upper()
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        articles = self.news()
        submitted, rejected = [], []
        for market in markets:
            symbol = str(market.get("symbol", "")).upper()
            product_id = f"{symbol}-USDC"
            cap, volume = float(market.get("market_cap") or 0), float(market.get("total_volume") or 0)
            if product_id not in eligible or symbol_counts.get(symbol) != 1 or cap < 50_000_000 or volume < 10_000_000:
                continue
            try:
                detail = self.coin_detail(str(market["id"]))
                identity = str(detail.get("id", "")) == str(market["id"]) and str(detail.get("symbol", "")).upper() == symbol
                clean, safety_score, safety_failures, contract = self.security(detail)
                news_score, news_veto, news_urls = score_news(articles, name=str(detail.get("name") or market.get("name") or ""), symbol=symbol)
            except urllib.error.HTTPError as exc:
                rejected.append({"product_id": product_id, "source_error": f"HTTP {exc.code} from {urlparse(exc.url).hostname or 'upstream'}"})
                continue
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                rejected.append({"product_id": product_id, "source_error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            if not identity or not clean or news_score < 4 or news_veto:
                rejected.append({"product_id": product_id, "identity": identity, "safety": safety_failures, "news_score": news_score, "news_veto": news_veto})
                continue
            observed = now_utc()
            explorer_urls = [str(value) for value in (detail.get("links", {}).get("blockchain_site") or []) if str(value).startswith("https://")]
            payload = {
                "coin_id": market["id"], "product_id": product_id, "contract": contract,
                "identity_verified": True, "no_safety_veto": True, "safety_score": safety_score,
                "news_score": news_score, "social_score": 0,
                "source_urls": list(dict.fromkeys([*news_urls, *explorer_urls[:1], "https://docs.gopluslabs.io/reference/api-overview"])),
                "observed_at": observed.isoformat(), "expires_at": (observed + timedelta(hours=2)).isoformat(),
                "thesis": "Two-source catalyst corroboration, verified token identity, and clean contract-security checks.",
                "invalidation": "Catalyst reversal, safety warning, identity mismatch, liquidity deterioration, or momentum reversal.",
            }
            self.post(f"{self.research_url}/evidence", payload, self.feed_token)
            submitted.append(product_id)
            time.sleep(0.25)
        result = {"ok": True, "scanned_at": iso_now(), "duration_ms": round((now_utc() - started).total_seconds() * 1000), "market_count": len(markets), "article_count": len(articles), "submitted": submitted, "rejected": rejected[:30]}
        with self.lock:
            self.status = result
        return result

    def run(self) -> None:
        while True:
            try:
                print(json.dumps({"event": "EVIDENCE_SCAN", **self.scan_once()}), flush=True)
            except Exception as exc:
                with self.lock:
                    self.status = {"ok": False, "scanned_at": iso_now(), "error": type(exc).__name__, "detail": str(exc)[:1000]}
                print(json.dumps({"event": "EVIDENCE_SCAN_ERROR", **self.status}), flush=True)
            time.sleep(self.interval)


adapter: EvidenceAdapter | None = None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        with adapter.lock:
            content = json.dumps({"ok": True, "service": "primus-evidence-adapter", "scanner": adapter.status}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    global adapter
    if os.getenv("EVIDENCE_ADAPTER_ENABLED", "false").lower() != "true":
        raise SystemExit("EVIDENCE_ADAPTER_ENABLED is not true; refusing to start")
    adapter = EvidenceAdapter()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, name="evidence-health", daemon=True).start()
    adapter.run()


if __name__ == "__main__":
    main()
