from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

UTC = timezone.utc
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LOCK = threading.RLock()
STATE: dict[str, Any] = {"ok": False, "scanned_at": "", "candidates": [], "error": "not scanned",
                         "feed": "", "wallet_events": 0}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def ratio(current: float, prior: float) -> float:
    return float(current) / max(float(prior), 1.0)


@dataclass(frozen=True)
class EarlyPolicy:
    minimum_score: float = 78.0
    minimum_liquidity_usd: float = 15_000.0
    maximum_token_age_minutes: float = 90.0
    minimum_unique_buyers_5m: int = 25
    maximum_top10_holder_fraction: float = 0.50
    maximum_creator_fraction: float = 0.10
    maximum_price_impact_bps: float = 250.0
    maximum_probe_usd: float = 3.0
    maximum_loss_usd: float = 1.0

    @classmethod
    def from_env(cls) -> "EarlyPolicy":
        return cls(
            minimum_score=float(os.getenv("SOLANA_EARLY_MIN_SCORE", "78")),
            minimum_liquidity_usd=float(os.getenv("SOLANA_EARLY_MIN_LIQUIDITY_USD", "15000")),
            maximum_token_age_minutes=float(os.getenv("SOLANA_EARLY_MAX_AGE_MINUTES", "90")),
            minimum_unique_buyers_5m=int(os.getenv("SOLANA_EARLY_MIN_BUYERS_5M", "25")),
            maximum_probe_usd=min(5.0, max(1.0, float(os.getenv("SOLANA_EARLY_MAX_PROBE_USD", "3")))),
            maximum_loss_usd=min(1.0, max(0.25, float(os.getenv("SOLANA_EARLY_MAX_LOSS_USD", "1")))),
        )


class EarlyRejected(ValueError):
    pass


class Ledger:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_outcomes(
              wallet TEXT NOT NULL, token TEXT NOT NULL, opened_at TEXT NOT NULL,
              closed_at TEXT, realized_return REAL, PRIMARY KEY(wallet,token,opened_at));
            CREATE TABLE IF NOT EXISTS signals(
              signal_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, mint TEXT NOT NULL,
              score REAL NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wallet_events(
              signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, mint TEXT NOT NULL,
              side TEXT NOT NULL, quantity REAL NOT NULL, observed_at TEXT NOT NULL,
              payload TEXT NOT NULL);
            """)

    def wallet_stats(self, wallet: str) -> dict[str, float]:
        with self.lock:
            rows = self.db.execute(
                "SELECT realized_return FROM wallet_outcomes WHERE wallet=? AND realized_return IS NOT NULL",
                (wallet,),
            ).fetchall()
        values = [float(row[0]) for row in rows]
        wins = sum(value > 0 for value in values)
        return {
            "sample_size": len(values),
            "win_rate": wins / len(values) if values else 0.0,
            "mean_return": sum(values) / len(values) if values else 0.0,
        }

    def store_signal(self, candidate: dict[str, Any], score: float, status: str) -> bool:
        mint = str(candidate.get("mint") or "")
        observed = str(candidate.get("observed_at") or utcnow())
        digest = hashlib.sha256(f"{mint}:{observed}:{score:.4f}".encode()).hexdigest()
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?)",
                (digest, observed, mint, score, json.dumps(candidate, sort_keys=True), status),
            )
        return cursor.rowcount == 1

    def store_wallet_event(self, event: dict[str, Any]) -> bool:
        values = (str(event["signature"]), str(event["wallet"]), str(event["mint"]),
                  str(event["side"]), float(event.get("quantity", 0)),
                  str(event.get("observed_at") or utcnow()), json.dumps(event, sort_keys=True))
        with self.lock, self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO wallet_events VALUES(?,?,?,?,?,?,?)", values)
        inserted = cursor.rowcount == 1
        if inserted:
            with self.lock, self.db:
                if event["side"] == "BUY" and float(event.get("quote_usdc", 0)) > 0:
                    self.db.execute("INSERT OR IGNORE INTO wallet_outcomes VALUES(?,?,?,?,?)",
                        (event["wallet"], event["mint"], event["observed_at"], None, None))
                elif event["side"] == "SELL" and float(event.get("quote_usdc", 0)) > 0:
                    row = self.db.execute(
                        "SELECT opened_at FROM wallet_outcomes WHERE wallet=? AND token=? AND closed_at IS NULL ORDER BY opened_at LIMIT 1",
                        (event["wallet"], event["mint"])).fetchone()
                    if row:
                        buy = self.db.execute("SELECT payload FROM wallet_events WHERE wallet=? AND mint=? AND side='BUY' AND observed_at=?",
                            (event["wallet"], event["mint"], row[0])).fetchone()
                        cost = float(json.loads(buy[0]).get("quote_usdc", 0)) if buy else 0
                        if cost > 0:
                            realized = float(event["quote_usdc"]) / cost - 1
                            self.db.execute("UPDATE wallet_outcomes SET closed_at=?,realized_return=? WHERE wallet=? AND token=? AND opened_at=?",
                                (event["observed_at"], realized, event["wallet"], event["mint"], row[0]))
        return inserted

    def recent_buyers(self, mint: str, minutes: int = 15) -> list[str]:
        cutoff = datetime.fromtimestamp(time.time() - minutes * 60, UTC).isoformat()
        with self.lock:
            rows = self.db.execute("SELECT DISTINCT wallet FROM wallet_events WHERE mint=? AND side='BUY' AND observed_at>=?",
                                   (mint, cutoff)).fetchall()
        return [str(row[0]) for row in rows]


def smart_wallet_score(wallets: list[str], ledger: Ledger) -> tuple[float, list[dict[str, Any]]]:
    qualified: list[dict[str, Any]] = []
    for wallet in dict.fromkeys(str(value) for value in wallets if value):
        stats = ledger.wallet_stats(wallet)
        # A wallet receives no predictive credit until it has enough closed,
        # timestamped outcomes. One lucky trade cannot create a smart-wallet label.
        if stats["sample_size"] >= 20 and stats["win_rate"] >= 0.55 and stats["mean_return"] > 0:
            qualified.append({"wallet": wallet, **stats})
    return min(10.0, len(qualified) * 2.5), qualified


def safety_failures(candidate: dict[str, Any], policy: EarlyPolicy) -> list[str]:
    failures: list[str] = []
    required_false = ("mint_authority_active", "freeze_authority_active", "transfer_hook_active",
                      "non_transferable", "creator_selling")
    for field in required_false:
        if field not in candidate:
            failures.append(f"{field} missing")
        elif candidate.get(field) is True:
            failures.append(field)
    if candidate.get("sell_simulation_ok") is not True:
        failures.append("sell simulation failed")
    if float(candidate.get("top10_holder_fraction", 1)) > policy.maximum_top10_holder_fraction:
        failures.append("top-10 concentration too high")
    if float(candidate.get("creator_fraction", 1)) > policy.maximum_creator_fraction:
        failures.append("creator concentration too high")
    return failures


def score_candidate(candidate: dict[str, Any], ledger: Ledger, policy: EarlyPolicy) -> dict[str, Any]:
    failures = safety_failures(candidate, policy)
    age = float(candidate.get("token_age_minutes", 9999))
    liquidity = float(candidate.get("liquidity_usd", 0))
    buyers = int(candidate.get("unique_buyers_5m", 0))
    impact = float(candidate.get("sell_price_impact_bps", 9999))
    if not 2 <= age <= policy.maximum_token_age_minutes:
        failures.append("token age outside discovery window")
    if liquidity < policy.minimum_liquidity_usd:
        failures.append("liquidity below minimum")
    if buyers < policy.minimum_unique_buyers_5m:
        failures.append("unique buyers below minimum")
    if impact > policy.maximum_price_impact_bps:
        failures.append("sell price impact above maximum")

    buyer_accel = ratio(buyers, int(candidate.get("unique_buyers_previous_5m", 0)))
    volume_accel = ratio(float(candidate.get("buy_volume_5m_usd", 0)),
                         float(candidate.get("buy_volume_previous_5m_usd", 0)))
    total = float(candidate.get("buy_volume_5m_usd", 0)) + float(candidate.get("sell_volume_5m_usd", 0))
    pressure = ((float(candidate.get("buy_volume_5m_usd", 0)) -
                 float(candidate.get("sell_volume_5m_usd", 0))) / total) if total else -1.0
    wallet_points, qualified_wallets = smart_wallet_score(candidate.get("buyer_wallets") or [], ledger)
    top10 = float(candidate.get("top10_holder_fraction", 1))
    creator = float(candidate.get("creator_fraction", 1))

    components = {
        "safety": 25.0 if not safety_failures(candidate, policy) else 0.0,
        "liquidity": clamp(math.log10(max(liquidity, 1) / policy.minimum_liquidity_usd + 1) * 12, 0, 15),
        "buyer_acceleration": clamp((buyer_accel - 1) * 12, 0, 15),
        "buy_pressure": clamp((pressure - .05) * 20, 0, 12),
        "distribution": clamp((.55 - top10) * 20 + (.12 - creator) * 25, 0, 10),
        "smart_wallets": wallet_points,
        "social_velocity": clamp(float(candidate.get("social_velocity_ratio", 0)) * 2, 0, 8),
        "creator_history": clamp(float(candidate.get("creator_history_score", 0)), 0, 5),
    }
    score = round(sum(components.values()), 2)
    if buyer_accel < 1.20:
        failures.append("buyers are not accelerating")
    if volume_accel < 1.20:
        failures.append("buy volume is not accelerating")
    if pressure < 0.10:
        failures.append("net buy pressure below minimum")
    if score < policy.minimum_score:
        failures.append(f"score {score:.2f} below {policy.minimum_score:.2f}")
    return {
        "mint": str(candidate.get("mint") or ""), "symbol": str(candidate.get("symbol") or ""),
        "price_usd": _number(candidate.get("price_usd")),
        "decimals": int(candidate.get("decimals") or 0),
        "score": score, "components": {key: round(value, 2) for key, value in components.items()},
        "buyer_acceleration": round(buyer_accel, 4), "volume_acceleration": round(volume_accel, 4),
        "net_buy_pressure": round(pressure, 4), "qualified_wallet_count": len(qualified_wallets),
        "qualified": not failures, "failures": list(dict.fromkeys(failures)),
        "paper_probe_usd": policy.maximum_probe_usd if not failures else 0.0,
        "mode": "PAPER_ONLY",
    }


def json_request(url: str, headers: dict[str, str] | None = None) -> Any:
    values = {"Accept": "application/json", "User-Agent": "primus-solana-early/1.1", **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, headers=values), timeout=20) as response:
        return json.loads(response.read().decode())


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _count(transactions: dict[str, Any], horizon: str, side: str) -> int:
    return int(_number((transactions.get(horizon) or {}).get(side), 0))


def _included(payload: dict[str, Any], relation: dict[str, Any]) -> dict[str, Any]:
    target = (relation or {}).get("data") or {}
    for value in payload.get("included") or []:
        if value.get("id") == target.get("id") and value.get("type") == target.get("type"):
            return value.get("attributes") or {}
    return {}


def goplus_safety(mint: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"contract_addresses": mint})
    payload = json_request(f"https://api.gopluslabs.io/api/v1/solana/token_security?{query}")
    values = payload.get("result") or {}
    facts = values.get(mint) or values.get(mint.lower()) or (next(iter(values.values())) if len(values) == 1 else {})
    holders = facts.get("holders") or []
    creators = facts.get("creators") or facts.get("creator") or []
    def active(field: str) -> bool:
        value = facts.get(field)
        return not isinstance(value, dict) or str(value.get("status", "1")).lower() not in {"0", "false"}
    return {
        "mint_authority_active": active("mintable"),
        "freeze_authority_active": active("freezable"),
        "transfer_hook_active": bool(facts.get("transfer_hook")),
        "non_transferable": str(facts.get("non_transferable", "1")).lower() not in {"0", "false"},
        "top10_holder_fraction": sum(_number(item.get("percent")) for item in holders[:10]) if holders else 1.0,
        "creator_fraction": sum(_number(item.get("percent")) for item in creators) if creators else 1.0,
        "creator_selling": any(str(item.get("sell_all", "0")).lower() not in {"0", "false"}
                               for item in creators if isinstance(item, dict)),
    }


def jupiter_sell_check(mint: str, decimals: int, price_usd: float) -> tuple[bool, float]:
    key = os.environ["JUPITER_API_KEY"]
    if not price_usd or decimals < 0:
        return False, 9999.0
    native_amount = max(1, int((1.0 / price_usd) * (10 ** decimals)))
    params = urllib.parse.urlencode({"inputMint": mint,
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "amount": native_amount})
    payload = json_request(f"https://api.jup.ag/swap/v2/order?{params}", {"x-api-key": key})
    impact = abs(_number(payload.get("priceImpactPct"), 100)) * 10_000
    return bool(payload.get("outAmount") or payload.get("outputAmount")), impact


def coingecko_candidates(ledger: Ledger) -> list[dict[str, Any]]:
    key = os.environ["COINGECKO_API_KEY"]
    base = os.getenv("COINGECKO_ONCHAIN_BASE_URL", "https://api.coingecko.com/api/v3/onchain").rstrip("/")
    # CoinGecko Demo and Pro credentials use different hosts and header names.
    # Select the header from the configured host so a Demo key is never sent as
    # a Pro credential (or vice versa).
    header = "x-cg-pro-api-key" if "pro-api.coingecko.com" in base else "x-cg-demo-api-key"
    headers = {header: key}
    payload = json_request(f"{base}/networks/solana/new_pools?include=base_token,quote_token,dex&page=1", headers)
    now = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        attributes = row.get("attributes") or {}
        relations = row.get("relationships") or {}
        base_token = _included(payload, relations.get("base_token") or {})
        quote_token = _included(payload, relations.get("quote_token") or {})
        mint = str(base_token.get("address") or "")
        pool = str(attributes.get("address") or "")
        created = str(attributes.get("pool_created_at") or "")
        try:
            age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            age = 9999
        # Ignore pools where SOL/USDC is the base rather than the discovered token.
        if not mint or base_token.get("symbol") in {"SOL", "WSOL", "USDC", "USDT"}:
            continue
        info = json_request(f"{base}/networks/solana/pools/{urllib.parse.quote(pool)}/info?include=pool", headers)
        token_info = next((item.get("attributes") or {} for item in info.get("data") or []
                           if (item.get("attributes") or {}).get("address") == mint), {})
        tx = attributes.get("transactions") or {}
        volume = attributes.get("volume_usd") or {}
        holders = token_info.get("holders") or {}
        distribution = holders.get("distribution_percentage") or {}
        buys5, sells5 = _count(tx, "m5", "buys"), _count(tx, "m5", "sells")
        buys15, sells15 = _count(tx, "m15", "buys"), _count(tx, "m15", "sells")
        volume5 = _number(volume.get("m5"))
        volume15 = _number(volume.get("m15"))
        safety = goplus_safety(mint)
        sell_ok, sell_impact = jupiter_sell_check(mint, int(base_token.get("decimals") or 0),
                                                  _number(attributes.get("base_token_price_usd")))
        results.append({
            "mint": mint, "pool": pool, "symbol": str(base_token.get("symbol") or ""),
            "price_usd": _number(attributes.get("base_token_price_usd")),
            "decimals": int(base_token.get("decimals") or 0),
            "observed_at": utcnow(), "token_age_minutes": age,
            "liquidity_usd": _number(attributes.get("reserve_in_usd")),
            "unique_buyers_5m": buys5, "unique_buyers_previous_5m": max(0, (buys15 - buys5) // 2),
            "buy_volume_5m_usd": volume5 * buys5 / max(buys5 + sells5, 1),
            "sell_volume_5m_usd": volume5 * sells5 / max(buys5 + sells5, 1),
            "buy_volume_previous_5m_usd": max(0, volume15 - volume5) / 2,
            **safety,
            "coingecko_top10_holder_fraction": _number(distribution.get("top_10"), 100) / 100,
            "sell_simulation_ok": sell_ok, "sell_price_impact_bps": sell_impact,
            "social_velocity_ratio": 0, "creator_history_score": 0,
            "buyer_wallets": ledger.recent_buyers(mint),
            "quote_symbol": str(quote_token.get("symbol") or ""), "source": "coingecko_onchain",
        })
    return results


def fetch_candidates(ledger: Ledger) -> tuple[list[dict[str, Any]], str]:
    url = os.getenv("SOLANA_EARLY_FEED_URL", "").strip()
    if not url:
        return coingecko_candidates(ledger), "coingecko_onchain"
    headers = {"Accept": "application/json", "User-Agent": "primus-solana-early/1.0"}
    token = os.getenv("SOLANA_EARLY_FEED_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json_request(url, headers)
    values = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("early feed must return a candidates list")
    return [value for value in values if isinstance(value, dict)], "normalized_feed"


class Handler(BaseHTTPRequestHandler):
    ledger: Ledger | None = None

    def do_POST(self) -> None:
        if self.path != "/webhooks/helius" or self.ledger is None:
            self.send_error(404); return
        expected = os.getenv("HELIUS_WEBHOOK_AUTH", "")
        if not expected or self.headers.get("Authorization", "") != f"Bearer {expected}":
            self.send_error(401); return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
            payload = json.loads(self.rfile.read(length))
            inserted = 0
            watched = set(filter(None, os.getenv("SOLANA_WATCH_WALLETS", "").split(",")))
            for tx in payload if isinstance(payload, list) else [payload]:
                signature = str(tx.get("signature") or "")
                timestamp = datetime.fromtimestamp(int(tx.get("timestamp") or time.time()), UTC).isoformat()
                transfers = tx.get("tokenTransfers") or []
                involved = {str(value.get(field) or "") for value in transfers
                            for field in ("fromUserAccount", "toUserAccount")}
                for wallet in watched & involved:
                    spent = sum(float(value.get("tokenAmount") or 0) for value in transfers
                                if value.get("mint") == USDC_MINT and value.get("fromUserAccount") == wallet)
                    received = sum(float(value.get("tokenAmount") or 0) for value in transfers
                                   if value.get("mint") == USDC_MINT and value.get("toUserAccount") == wallet)
                    for value in transfers:
                        mint = str(value.get("mint") or "")
                        if not signature or not mint or mint == USDC_MINT:
                            continue
                        side = "BUY" if value.get("toUserAccount") == wallet and spent > 0 else \
                               "SELL" if value.get("fromUserAccount") == wallet and received > 0 else ""
                        if not side:
                            continue
                        event = {"signature": f"{signature}:{wallet}:{mint}:{side}", "wallet": wallet,
                                 "mint": mint, "side": side, "quantity": value.get("tokenAmount") or 0,
                                 "quote_usdc": spent if side == "BUY" else received, "observed_at": timestamp}
                        inserted += int(self.ledger.store_wallet_event(event))
            with LOCK:
                STATE["wallet_events"] = int(STATE.get("wallet_events", 0)) + inserted
            body = json.dumps({"ok": True, "inserted": inserted}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception:
            self.send_error(400)

    def do_GET(self) -> None:
        if self.path not in {"/health", "/status", "/candidates"}:
            self.send_error(404); return
        with LOCK:
            value = dict(STATE)
        if self.path == "/health":
            value = {"ok": True, "operational": value["ok"], "service": "solana-early-discovery",
                     "mode": "PAPER_ONLY", "scanned_at": value["scanned_at"], "error": value["error"],
                     "feed": value.get("feed", ""), "wallet_events": value.get("wallet_events", 0)}
        body = json.dumps(value).encode()
        self.send_response(200 if self.path == "/health" or value.get("ok", True) else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


def main() -> None:
    if os.getenv("SOLANA_EARLY_ENABLED", "false").lower() != "true":
        raise SystemExit("SOLANA_EARLY_ENABLED is not true")
    if os.getenv("SOLANA_EARLY_LIVE_ENABLED", "false").lower() == "true":
        raise SystemExit("live Solana execution is not implemented in discovery release")
    ledger = Ledger(os.getenv("SOLANA_EARLY_LEDGER_PATH", "/app/data/solana_early.sqlite3"))
    Handler.ledger = ledger
    policy = EarlyPolicy.from_env()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    interval = max(10, int(os.getenv("SOLANA_EARLY_SCAN_INTERVAL_SECONDS", "20")))
    while True:
        try:
            results = []
            candidates, feed = fetch_candidates(ledger)
            for candidate in candidates:
                outcome = score_candidate(candidate, ledger, policy)
                ledger.store_signal(candidate, outcome["score"], "QUALIFIED" if outcome["qualified"] else "REJECTED")
                results.append(outcome)
            results.sort(key=lambda item: item["score"], reverse=True)
            with LOCK:
                STATE.update(ok=True, scanned_at=utcnow(), candidates=results[:100], error="", feed=feed)
        except Exception as exc:
            with LOCK:
                STATE.update(ok=False, scanned_at=utcnow(), error=str(exc)[:500])
        time.sleep(interval)


if __name__ == "__main__":
    main()
