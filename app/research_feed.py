from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
except ImportError:  # Keeps the pure scoring engine testable without server extras.
    Starlette = Request = JSONResponse = Route = None  # type: ignore[assignment,misc]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:] if value.lower().startswith("bearer ") else value


class ResearchFeed:
    """Continuously produces complete, auditable Model 3.1 candidates.

    Market facts are collected automatically. Safety and news facts must come
    from a fresh authenticated evidence attestation; absent evidence scores zero
    and suppresses the candidate rather than being guessed.
    """

    def __init__(self) -> None:
        self.token = os.environ["SIGNAL_FEED_BEARER_TOKEN"]
        self.executor_url = os.environ["EXECUTOR_BASE_URL"].rstrip("/")
        self.executor_token = os.environ["REST_API_TOKEN"]
        self.cg_base = os.getenv("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3").rstrip("/")
        self.cg_key = os.getenv("COINGECKO_API_KEY", "")
        self.pages = max(1, min(3, int(os.getenv("RESEARCH_MARKET_PAGES", "2"))))
        self.interval = max(15, min(300, int(os.getenv("RESEARCH_SCAN_INTERVAL_SECONDS", "30"))))
        self.notional = clamp(float(os.getenv("PILOT_NOTIONAL_USDC", "23.75")), 1.0, 25.0)
        self.max_loss = min(2.5, float(os.getenv("PILOT_MAX_LOSS_USDC", "2.50")))
        self.state_path = Path(os.getenv("RESEARCH_FEED_STATE_PATH", "/app/data/research_feed.json"))
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {"evidence": {}, "candidates": [], "status": {"ok": False}}
        self._load()

    def authorized(self, request: Request) -> bool:
        return bool(self.token) and hmac.compare_digest(bearer_token(request), self.token)

    def _load(self) -> None:
        try:
            loaded = json.loads(self.state_path.read_text())
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True, indent=2))
        temporary.replace(self.state_path)

    def fetch(self, url: str, token: str = "") -> Any:
        headers = {"Accept": "application/json", "User-Agent": "primus-research-feed/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.cg_key and url.startswith(self.cg_base):
            headers["x-cg-demo-api-key"] = self.cg_key
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return json.loads(response.read().decode())

    def market_page(self, page: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "vs_currency": "usd", "category": "meme-token", "order": "market_cap_desc",
            "per_page": 250, "page": page, "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        })
        result = self.fetch(f"{self.cg_base}/coins/markets?{query}")
        return result if isinstance(result, list) else []

    @staticmethod
    def unique_by_symbol(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for market in markets:
            grouped.setdefault(str(market.get("symbol", "")).upper(), []).append(market)
        return {symbol: values[0] for symbol, values in grouped.items() if symbol and len(values) == 1}

    @staticmethod
    def regime(markets: list[dict[str, Any]]) -> dict[str, Any]:
        liquid = [m for m in markets if float(m.get("market_cap") or 0) >= 50_000_000 and float(m.get("total_volume") or 0) >= 10_000_000]
        changes = lambda key: [float(m.get(key) or 0) for m in liquid]
        one, day, week = changes("price_change_percentage_1h_in_currency"), changes("price_change_percentage_24h_in_currency"), changes("price_change_percentage_7d_in_currency")
        breadth = sum(v > 0 for v in day) / len(day) if day else 0
        medians = {"1h": statistics.median(one) if one else 0, "24h": statistics.median(day) if day else 0, "7d": statistics.median(week) if week else 0}
        rising = len(liquid) >= 5 and breadth >= .60 and all(value > 0 for value in medians.values())
        return {"classification": "RISING" if rising else "MIXED", "sample_size": len(liquid), "positive_24h_breadth": breadth, "median_changes": medians}

    @staticmethod
    def valid_evidence(evidence: dict[str, Any], product_id: str, at: datetime) -> tuple[bool, list[str]]:
        failures: list[str] = []
        if evidence.get("product_id") != product_id: failures.append("evidence product mismatch")
        if evidence.get("identity_verified") is not True: failures.append("identity not attested")
        if evidence.get("no_safety_veto") is not True: failures.append("safety veto not cleared")
        if float(evidence.get("safety_score") or 0) < 12: failures.append("safety score below 12")
        if float(evidence.get("news_score") or 0) < 4: failures.append("verified news score below 4")
        urls = evidence.get("source_urls")
        if not isinstance(urls, list) or not urls or any(not str(url).startswith("https://") for url in urls): failures.append("HTTPS evidence sources missing")
        try:
            observed, expires = parse_time(str(evidence["observed_at"])), parse_time(str(evidence["expires_at"]))
            if observed > at or (at - observed).total_seconds() > 72 * 3600: failures.append("evidence stale")
            if expires <= at: failures.append("evidence expired")
        except Exception: failures.append("invalid evidence timestamps")
        return not failures, failures

    def score(self, market: dict[str, Any], evidence: dict[str, Any], regime: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
        cap, volume, fdv = (float(market.get(k) or 0) for k in ("market_cap", "total_volume", "fully_diluted_valuation"))
        turnover = volume / cap if cap else 999
        one = float(market.get("price_change_percentage_1h_in_currency") or 0)
        day = float(market.get("price_change_percentage_24h_in_currency") or 0)
        week = float(market.get("price_change_percentage_7d_in_currency") or 0)
        dilution = cap / fdv if fdv else 0
        failures = []
        if regime["classification"] != "RISING": failures.append("regime not RISING")
        if cap < 50_000_000: failures.append("market cap below $50M")
        if volume < 10_000_000: failures.append("volume below $10M")
        if not .05 <= turnover <= 1: failures.append("turnover outside 5%-100%")
        if one <= 0: failures.append("1h momentum not positive")
        if day <= 0 or day > 15: failures.append("24h momentum outside (0%,15%]")
        if dilution < .70: failures.append("severe dilution")
        components = {
            "regime": 15 if regime["classification"] == "RISING" else 0,
            "liquidity": 20 if volume >= 25_000_000 else 18,
            "momentum": 13 if one > 0 and 0 < day <= 15 and week > 0 else (9 if one > 0 and 0 < day <= 15 else 0),
            "volume_quality": 12 if .05 <= turnover <= 1 else 0,
            "tokenomics": 10 if dilution >= .9 else (8 if dilution >= .7 else 0),
            "safety": int(clamp(float(evidence.get("safety_score") or 0), 0, 15)),
            "news": int(clamp(float(evidence.get("news_score") or 0), 0, 10)),
            "social": int(clamp(float(evidence.get("social_score") or 0), 0, 5)),
        }
        if sum(components.values()) < 85: failures.append("Model 3.1 score below 85")
        return components, failures

    def build_candidate(self, market: dict[str, Any], product: dict[str, Any], evidence: dict[str, Any], regime: dict[str, Any], at: datetime) -> tuple[dict[str, Any] | None, list[str]]:
        product_id = str(product["product_id"])
        evidence_ok, failures = self.valid_evidence(evidence, product_id, at)
        components, score_failures = self.score(market, evidence, regime)
        failures.extend(score_failures)
        price = float(product.get("price") or market.get("current_price") or 0)
        if price <= 0: failures.append("invalid reference price")
        if not evidence_ok or failures: return None, failures
        evidence_stamp = str(evidence["observed_at"])
        signal_id = hashlib.sha256(f"{product_id}|{evidence_stamp}|{at.strftime('%Y-%m-%dT%H')}".encode()).hexdigest()
        sources = [f"https://www.coingecko.com/en/coins/{market['id']}", *evidence["source_urls"]]
        candidate = {
            "signal_id": signal_id, "product_id": product_id, "regime": "RISING",
            "component_scores": components,
            "change_1h_pct": float(market.get("price_change_percentage_1h_in_currency") or 0),
            "change_24h_pct": float(market.get("price_change_percentage_24h_in_currency") or 0),
            "change_7d_pct": float(market.get("price_change_percentage_7d_in_currency") or 0),
            "market_cap_usd": float(market.get("market_cap") or 0),
            "volume_24h_usd": float(market.get("total_volume") or 0),
            "turnover": float(market.get("total_volume") or 0) / float(market.get("market_cap") or 1),
            "identity_verified": True, "no_safety_veto": True,
            "notional_usdc": self.notional, "max_loss_usdc": self.max_loss,
            "reference_price": price, "limit_price": price * 1.0035,
            "stop_price": price * .92, "target_price": price * 1.15,
            "thesis": str(evidence.get("thesis") or "Fresh verified catalyst with positive liquid-market momentum"),
            "invalidation": str(evidence.get("invalidation") or "Safety veto, catalyst invalidation, spread/slippage failure, or momentum reversal"),
            "evidence_urls": list(dict.fromkeys(sources)), "source_timestamp": at.isoformat(), "expiry_seconds": 90,
        }
        return candidate, []

    def scan_once(self) -> dict[str, Any]:
        started = now_utc()
        products_payload = self.fetch(f"{self.executor_url}/api/eligible-products", self.executor_token)
        products = products_payload.get("products", [])
        markets: list[dict[str, Any]] = []
        for page in range(1, self.pages + 1): markets.extend(self.market_page(page))
        by_symbol = self.unique_by_symbol(markets)
        regime = self.regime(markets)
        candidates, rejected = [], []
        with self.lock:
            evidence_store = dict(self.state.get("evidence", {}))
        for product in products:
            product_id = str(product.get("product_id", ""))
            if not product_id.endswith("-USDC"): continue
            symbol = product_id[:-5].upper()
            market = by_symbol.get(symbol)
            if not market: continue
            evidence = evidence_store.get(str(market["id"]), {})
            candidate, failures = self.build_candidate(market, product, evidence, regime, started)
            if candidate: candidates.append(candidate)
            elif evidence: rejected.append({"product_id": product_id, "coin_id": market["id"], "failures": failures})
        candidates.sort(key=lambda item: sum(item["component_scores"].values()), reverse=True)
        status = {"ok": True, "scanned_at": iso_now(), "duration_ms": round((now_utc() - started).total_seconds() * 1000), "market_count": len(markets), "coinbase_product_count": len(products), "regime": regime, "candidate_count": len(candidates), "rejected_attested": rejected[:50]}
        with self.lock:
            self.state["candidates"], self.state["status"] = candidates, status
            self._save()
        return status

    def submit_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        coin_id = str(payload.get("coin_id", "")).strip()
        if not coin_id: raise ValueError("coin_id is required")
        if not str(payload.get("product_id", "")).endswith("-USDC"): raise ValueError("product_id must be a USDC spot product")
        with self.lock:
            self.state.setdefault("evidence", {})[coin_id] = payload
            self._save()
        return {"accepted": True, "coin_id": coin_id, "stored_at": iso_now()}

    def run(self) -> None:
        while True:
            try:
                print(json.dumps({"event": "RESEARCH_SCAN", **self.scan_once()}), flush=True)
            except Exception as exc:
                with self.lock:
                    self.state["status"] = {"ok": False, "scanned_at": iso_now(), "error": type(exc).__name__, "detail": str(exc)[:1000]}
                    self._save()
                print(json.dumps({"event": "RESEARCH_SCAN_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
            time.sleep(self.interval)


feed: ResearchFeed | None = None


def get_feed() -> ResearchFeed:
    global feed
    if feed is None: feed = ResearchFeed()
    return feed


async def health(_: Request) -> JSONResponse:
    instance = get_feed()
    with instance.lock: status = dict(instance.state.get("status", {}))
    return JSONResponse({"ok": True, "service": "primus-research-feed", "scanner": status})


async def candidates(request: Request) -> JSONResponse:
    instance = get_feed()
    if not instance.authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    with instance.lock: return JSONResponse({"generated_at": iso_now(), "candidates": list(instance.state.get("candidates", [])), "scanner": instance.state.get("status", {})})


async def status(request: Request) -> JSONResponse:
    instance = get_feed()
    if not instance.authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    with instance.lock: return JSONResponse({"status": instance.state.get("status", {}), "evidence_count": len(instance.state.get("evidence", {})), "candidate_count": len(instance.state.get("candidates", []))})


async def evidence(request: Request) -> JSONResponse:
    instance = get_feed()
    if not instance.authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    try: return JSONResponse(instance.submit_evidence(await request.json()), status_code=202)
    except (ValueError, TypeError) as exc: return JSONResponse({"error": str(exc)}, status_code=422)


app = Starlette(routes=[Route("/health", health), Route("/candidates", candidates), Route("/status", status), Route("/evidence", evidence, methods=["POST"])]) if Starlette else None


def main() -> None:
    if os.getenv("RESEARCH_FEED_ENABLED", "false").lower() != "true": raise SystemExit("RESEARCH_FEED_ENABLED is not true; refusing to start")
    if app is None: raise SystemExit("Starlette server dependencies are not installed")
    instance = get_feed()
    threading.Thread(target=instance.run, name="research-scanner", daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__": main()
