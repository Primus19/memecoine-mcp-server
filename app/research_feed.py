from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import OpportunityPolicy
from .quant import conservative_probability, expected_net_value, multi_horizon_consensus

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
        # Account credits are opt-in. By default /coins/markets is requested
        # keylessly, so an exhausted Demo key cannot interrupt research.
        self.cg_key = (os.getenv("COINGECKO_API_KEY", "")
                       if os.getenv("COINGECKO_USE_ACCOUNT_KEY", "false").lower() == "true" else "")
        # Scan the broad crypto market instead of CoinGecko's meme-only category.
        # Four pages covers the leading 1,000 assets; operators can extend this
        # without changing code when Coinbase adds deeper USDC markets.
        self.pages = max(4, min(10, int(os.getenv("RESEARCH_MARKET_PAGES", "4"))))
        self.interval = max(300, min(1800, int(os.getenv("RESEARCH_SCAN_INTERVAL_SECONDS", "300"))))
        self.http_max_retries = max(1, min(5, int(os.getenv("RESEARCH_HTTP_MAX_RETRIES", "3"))))
        self.http_retry_backoff = max(.25, min(10.0, float(os.getenv("RESEARCH_HTTP_RETRY_BACKOFF_SECONDS", "1"))))
        self.request_spacing = max(0.0, min(10.0, float(os.getenv("RESEARCH_REQUEST_SPACING_SECONDS", "1"))))
        self.notional = clamp(float(os.getenv("PILOT_NOTIONAL_USDC", "23.75")), 1.0, 25.0)
        self.max_loss = min(2.5, float(os.getenv("PILOT_MAX_LOSS_USDC", "2.50")))
        self.emerging_notional = clamp(float(os.getenv("PILOT_EMERGING_NOTIONAL_USDC", "5.00")), 5.0, 5.0)
        self.emerging_max_loss = min(.25, float(os.getenv("PILOT_EMERGING_MAX_LOSS_USDC", "0.25")))
        self.policy = OpportunityPolicy.from_env()
        self.state_path = Path(os.getenv("RESEARCH_FEED_STATE_PATH", "/app/data/research_feed.json"))
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {"evidence": {}, "candidates": [], "shadow_outcomes": [], "status": {"ok": False}}
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
        request = urllib.request.Request(url, headers=headers)
        for attempt in range(self.http_max_retries):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt + 1 >= self.http_max_retries:
                    raise
                retry_after = str(exc.headers.get("Retry-After", "")).strip()
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = self.http_retry_backoff * (2 ** attempt)
                time.sleep(min(30.0, max(self.http_retry_backoff, delay)))
        raise RuntimeError("HTTP retry loop exited unexpectedly")

    def market_page(self, page: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "vs_currency": "usd", "order": "market_cap_desc",
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
        falling = len(liquid) >= 5 and breadth <= .40 and medians["1h"] < 0 and medians["24h"] < 0
        classification = "RISING" if rising else "FALLING" if falling else "MIXED"
        return {"classification": classification, "sample_size": len(liquid), "positive_24h_breadth": breadth, "median_changes": medians}

    @staticmethod
    def valid_evidence(evidence: dict[str, Any], product_id: str, at: datetime) -> tuple[bool, list[str]]:
        failures: list[str] = []
        if evidence.get("product_id") != product_id: failures.append("evidence product mismatch")
        if evidence.get("identity_verified") is not True: failures.append("identity not attested")
        if evidence.get("no_safety_veto") is not True: failures.append("safety veto not cleared")
        if float(evidence.get("safety_score") or 0) < 12: failures.append("safety score below 12")
        if evidence.get("news_veto") is True: failures.append("news veto active")
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
        if not self.policy.regime_allowed(regime["classification"]): failures.append("regime not permitted")
        tier = self.policy.tier(cap, volume)
        if tier == "INELIGIBLE": failures.append("market cap or volume below all policy tiers")
        if not self.policy.min_turnover <= turnover <= self.policy.max_turnover: failures.append("turnover outside policy range")
        if one <= 0: failures.append("1h momentum not positive")
        chase_cap = 2.0 if tier == "EMERGING" else 3.0
        if one > chase_cap: failures.append(f"1h momentum above {chase_cap:g}% anti-chase cap")
        if day <= 0 or day > self.policy.max_momentum_24h_pct: failures.append("24h momentum outside policy range")
        if dilution < .70: failures.append("severe dilution")
        if not self.policy.news_allowed(evidence.get("news_score", 0), news_veto=evidence.get("news_veto") is True): failures.append("news policy gate failed")
        if regime["classification"] == "MIXED" and float(evidence.get("news_score") or 0) < 4:
            failures.append("mixed regime requires a verified catalyst/news score of at least 4")
        components = {
            "regime": 15 if regime["classification"] == "RISING" else (8 if regime["classification"] == "MIXED" else 0),
            "liquidity": 20 if volume >= 25_000_000 else 18,
            "momentum": 13 if one > 0 and 0 < day <= self.policy.max_momentum_24h_pct and week > 0 else (9 if one > 0 and 0 < day <= self.policy.max_momentum_24h_pct else 0),
            "volume_quality": 12 if self.policy.min_turnover <= turnover <= self.policy.max_turnover else 0,
            "tokenomics": 10 if dilution >= .9 else (8 if dilution >= .7 else 0),
            "safety": int(clamp(float(evidence.get("safety_score") or 0), 0, 15)),
            "news": int(clamp(float(evidence.get("news_score") or 0), 0, 10)),
            "social": int(clamp(float(evidence.get("social_score") or 0), 0, 5)),
        }
        minimum_score = self.policy.minimum_score_for(tier)
        if sum(components.values()) < minimum_score: failures.append(f"Model 3.1 score below {minimum_score:g}")
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
        tier = self.policy.tier(market.get("market_cap"), market.get("total_volume"))
        emerging = tier == "EMERGING"
        one = float(market.get("price_change_percentage_1h_in_currency") or 0)
        day = float(market.get("price_change_percentage_24h_in_currency") or 0)
        week = float(market.get("price_change_percentage_7d_in_currency") or 0)
        consensus = multi_horizon_consensus((one, day, week), (.20, .35, .45))
        # CoinGecko does not provide OHLC in this scan. This is a conservative
        # multi-horizon realized-volatility proxy, recorded explicitly as such.
        hourly_volatility_pct = max(abs(one), abs(day) / (24 ** .5), abs(week) / (168 ** .5))
        stop_pct = clamp(hourly_volatility_pct * (2.25 if emerging else 2.50), 3.0 if emerging else 4.0, 7.0 if emerging else 8.0)
        reward_multiple = 2.5
        target_pct = clamp(stop_pct * reward_multiple, 8.0, 20.0)
        total_score = sum(components.values())
        probability = conservative_probability(total_score, consensus["agreement"])
        value = expected_net_value(
            win_probability=probability,
            expected_gain_bps=target_pct * 100,
            expected_loss_bps=stop_pct * 100,
            fee_bps=2 * self.policy.estimated_fee_bps_per_side,
        )
        candidate = {
            "signal_id": signal_id, "product_id": product_id, "opportunity_tier": tier,
            "regime": regime["classification"],
            "component_scores": components,
            "change_1h_pct": float(market.get("price_change_percentage_1h_in_currency") or 0),
            "change_24h_pct": float(market.get("price_change_percentage_24h_in_currency") or 0),
            "change_7d_pct": float(market.get("price_change_percentage_7d_in_currency") or 0),
            "horizon_direction": consensus["direction"], "horizon_agreement": consensus["agreement"],
            "volatility_proxy_pct": hourly_volatility_pct, "volatility_method": "MULTI_HORIZON_REALIZED_PROXY",
            "signal_probability_shadow": probability, "expected_net_bps_shadow": value.expected_net_bps,
            "market_cap_usd": float(market.get("market_cap") or 0),
            "volume_24h_usd": float(market.get("total_volume") or 0),
            "turnover": float(market.get("total_volume") or 0) / float(market.get("market_cap") or 1),
            "identity_verified": True, "no_safety_veto": True, "news_veto": evidence.get("news_veto") is True,
            "notional_usdc": self.emerging_notional if emerging else self.notional,
            "max_loss_usdc": self.emerging_max_loss if emerging else self.max_loss,
            "reference_price": price, "limit_price": price * 1.0035,
            "stop_price": price * (1 - stop_pct / 100),
            "target_1_price": price * (1 + min(target_pct / 2, stop_pct * 1.25) / 100),
            "target_price": price * (1 + target_pct / 100),
            "trail_activation_pct": clamp(stop_pct * .75, 3.0, 6.0),
            "trail_pct": clamp(stop_pct * .50, 2.0, 4.0),
            "thesis": str(evidence.get("thesis") or "Fresh verified catalyst with positive liquid-market momentum"),
            "invalidation": str(evidence.get("invalidation") or "Safety veto, catalyst invalidation, spread/slippage failure, or momentum reversal"),
            "evidence_urls": list(dict.fromkeys(sources)), "source_timestamp": at.isoformat(), "expiry_seconds": 90,
        }
        return candidate, []

    def update_shadow_outcomes(self, markets: dict[str, dict[str, Any]], at: datetime) -> None:
        """Update counterfactual returns for rejected, never-traded candidates."""
        shadows = list(self.state.get("shadow_outcomes", []))
        for item in shadows:
            market = markets.get(str(item.get("symbol", "")).upper())
            price = float((market or {}).get("current_price") or 0)
            entry = float(item.get("entry_price") or 0)
            if price <= 0 or entry <= 0: continue
            current = (price / entry - 1) * 100
            item["last_price"] = price
            item["last_seen_at"] = at.isoformat()
            item["current_return_pct"] = round(current, 6)
            item["max_return_pct"] = round(max(float(item.get("max_return_pct") or current), current), 6)
            item["min_return_pct"] = round(min(float(item.get("min_return_pct") or current), current), 6)
        self.state["shadow_outcomes"] = shadows[-500:]

    def record_shadow(self, product_id: str, market: dict[str, Any], failures: list[str],
                      components: dict[str, int], at: datetime) -> None:
        price = float(market.get("current_price") or 0)
        if price <= 0: return
        key = f"{product_id}|{at.strftime('%Y-%m-%dT%H')}"
        shadows = list(self.state.get("shadow_outcomes", []))
        if any(item.get("key") == key for item in shadows): return
        shadows.append({"key": key, "product_id": product_id,
                        "symbol": product_id.removesuffix("-USDC"),
                        "opportunity_tier": self.policy.tier(market.get("market_cap"), market.get("total_volume")),
                        "score": sum(components.values()), "failures": list(failures),
                        "entry_price": price, "last_price": price,
                        "first_seen_at": at.isoformat(), "last_seen_at": at.isoformat(),
                        "current_return_pct": 0.0, "max_return_pct": 0.0, "min_return_pct": 0.0})
        self.state["shadow_outcomes"] = shadows[-500:]

    def shadow_summary(self) -> dict[str, Any]:
        shadows = list(self.state.get("shadow_outcomes", []))
        positive = [x for x in shadows if float(x.get("current_return_pct") or 0) > 0]
        return {"sample_size": len(shadows), "positive_now": len(positive),
                "average_current_return_pct": (sum(float(x.get("current_return_pct") or 0) for x in shadows) / len(shadows) if shadows else None),
                "best_missed_return_pct": max((float(x.get("max_return_pct") or 0) for x in shadows), default=None)}

    @staticmethod
    def candidate_rank_key(candidate: dict[str, Any]) -> tuple[float, float, float, float, float, str]:
        """Rank by shadow net value, then evidence quality, never feed ordering."""
        components = candidate.get("component_scores") or {}
        return (
            -float(candidate.get("expected_net_bps_shadow") or 0),
            -float(sum(components.values())),
            -float(components.get("safety") or 0),
            -float(components.get("news") or 0),
            -float(candidate.get("change_1h_pct") or 0),
            str(candidate.get("product_id") or ""),
        )

    def scan_once(self) -> dict[str, Any]:
        started = now_utc()
        products_payload = self.fetch(f"{self.executor_url}/api/eligible-products", self.executor_token)
        products = products_payload.get("products", [])
        markets: list[dict[str, Any]] = []
        for page in range(1, self.pages + 1):
            if page > 1 and self.request_spacing:
                time.sleep(self.request_spacing)
            markets.extend(self.market_page(page))
        by_symbol = self.unique_by_symbol(markets)
        regime = self.regime(markets)
        candidates, rejected = [], []
        with self.lock:
            evidence_store = dict(self.state.get("evidence", {}))
            self.update_shadow_outcomes(by_symbol, started)
        for product in products:
            product_id = str(product.get("product_id", ""))
            if not product_id.endswith("-USDC"): continue
            symbol = product_id[:-5].upper()
            market = by_symbol.get(symbol)
            if not market: continue
            evidence = evidence_store.get(str(market["id"]), {})
            components, _ = self.score(market, evidence, regime)
            candidate, failures = self.build_candidate(market, product, evidence, regime, started)
            if candidate: candidates.append(candidate)
            elif evidence:
                rejected.append({"product_id": product_id, "coin_id": market["id"],
                                 "opportunity_tier": self.policy.tier(market.get("market_cap"), market.get("total_volume")),
                                 "score": sum(components.values()), "failures": failures})
                with self.lock: self.record_shadow(product_id, market, failures, components, started)
        candidates.sort(key=self.candidate_rank_key)
        for rank, candidate in enumerate(candidates, 1):
            candidate["candidate_rank"] = rank
            candidate["selection_rationale"] = (
                "Ranked by conservative shadow net expected value, total Model 3.1 score, safety, "
                "verified news, then 1h momentum; "
                "product identity adds no preference or restriction, and all live risk gates remain authoritative."
            )
        ranked = [{
            "rank": item["candidate_rank"], "product_id": item["product_id"],
            "opportunity_tier": item["opportunity_tier"],
            "score": sum(item["component_scores"].values()),
            "component_scores": item["component_scores"],
            "change_1h_pct": item["change_1h_pct"],
            "change_24h_pct": item["change_24h_pct"],
            "horizon_agreement": item["horizon_agreement"],
            "volatility_proxy_pct": item["volatility_proxy_pct"],
            "expected_net_bps_shadow": item["expected_net_bps_shadow"],
            "selection_rationale": item["selection_rationale"],
        } for item in candidates[:10]]
        matched_products = sum(
            str(product.get("product_id", "")).endswith("-USDC")
            and str(product.get("product_id", ""))[:-5].upper() in by_symbol
            for product in products
        )
        status = {"ok": True, "scanned_at": iso_now(), "duration_ms": round((now_utc() - started).total_seconds() * 1000), "universe":"ALL_CRYPTO_USDC_SPOT", "market_count": len(markets), "coinbase_product_count": len(products), "matched_coinbase_products": matched_products, "unmatched_coinbase_products": max(0, len(products) - matched_products), "regime": regime, "candidate_count": len(candidates), "candidate_tiers": {"established": sum(x.get("opportunity_tier") == "ESTABLISHED" for x in candidates), "emerging": sum(x.get("opportunity_tier") == "EMERGING" for x in candidates)}, "ranked_candidates": ranked, "shadow_summary": self.shadow_summary(), "rejected_attested": rejected[:50]}
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
                    previous = dict(self.state.get("status", {}))
                    failure = {"ok": False, "scanned_at": iso_now(), "error": type(exc).__name__, "detail": str(exc)[:1000]}
                    if previous.get("ok") is True:
                        failure["last_successful"] = previous
                    elif isinstance(previous.get("last_successful"), dict):
                        failure["last_successful"] = previous["last_successful"]
                    self.state["status"] = failure
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
