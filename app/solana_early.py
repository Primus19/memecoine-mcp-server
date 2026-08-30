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
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

UTC = timezone.utc
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LOCK = threading.RLock()
GOPLUS_LOCK = threading.RLock()
GOPLUS_LAST_REQUEST = 0.0
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
STATE: dict[str, Any] = {"ok": False, "scanned_at": "", "scan_started_at": "",
                         "scan_status": "NOT_STARTED", "candidates": [], "error": "not scanned",
                         "feed": "", "wallet_events": 0, "strategy_diagnostics": {}}


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
    paper_minimum_score: float = 40.0
    paper_minimum_liquidity_usd: float = 7_500.0
    paper_maximum_token_age_minutes: float = 180.0
    paper_minimum_unique_buyers_5m: int = 10
    paper_maximum_price_impact_bps: float = 250.0
    paper_maximum_top10_holder_fraction: float = 0.80
    paper_maximum_creator_fraction: float = 0.30

    @classmethod
    def from_env(cls) -> "EarlyPolicy":
        return cls(
            minimum_score=float(os.getenv("SOLANA_EARLY_MIN_SCORE", "78")),
            minimum_liquidity_usd=float(os.getenv("SOLANA_EARLY_MIN_LIQUIDITY_USD", "15000")),
            maximum_token_age_minutes=float(os.getenv("SOLANA_EARLY_MAX_AGE_MINUTES", "90")),
            minimum_unique_buyers_5m=int(os.getenv("SOLANA_EARLY_MIN_BUYERS_5M", "25")),
            maximum_probe_usd=min(5.0, max(1.0, float(os.getenv("SOLANA_EARLY_MAX_PROBE_USD", "3")))),
            maximum_loss_usd=min(1.0, max(0.25, float(os.getenv("SOLANA_EARLY_MAX_LOSS_USD", "1")))),
            # Evidence floors prevent stale Railway variables from silently
            # restoring the loss-making, ultra-loose V1 paper gate.
            paper_minimum_score=max(40.0, min(100.0, float(os.getenv("SOLANA_PAPER_MIN_SCORE", "40")))),
            paper_minimum_liquidity_usd=max(7_500.0, float(os.getenv("SOLANA_PAPER_MIN_LIQUIDITY_USD", "7500"))),
            paper_maximum_token_age_minutes=min(180.0, max(30.0, float(os.getenv("SOLANA_PAPER_MAX_AGE_MINUTES", "180")))),
            paper_minimum_unique_buyers_5m=max(10, int(os.getenv("SOLANA_PAPER_MIN_BUYERS_5M", "10"))),
            paper_maximum_price_impact_bps=min(250.0, max(50.0, float(os.getenv("SOLANA_PAPER_MAX_PRICE_IMPACT_BPS", "250")))),
        )


@dataclass(frozen=True)
class PumpfunEvPolicy:
    enabled: bool = True
    target_market_cap_usd: float = 25_000.0
    maximum_entry_market_cap_usd: float = 15_000.0
    minimum_trades_5m: int = 10
    maximum_age_minutes: float = 20.0
    assumed_loss_fraction: float = 0.50
    minimum_ev_rank: float = 0.35
    minimum_stressed_expectancy: float = 0.15
    maximum_payoff_multiple: float = 8.0
    minimum_control_score: float = 20.0
    minimum_net_buy_pressure: float = 0.15
    minimum_buyer_acceleration: float = 1.20
    minimum_volume_acceleration: float = 1.20
    maximum_top10_holder_fraction: float = 0.85
    maximum_creator_fraction: float = 0.35
    minimum_price_change_5m_pct: float = 0.0
    maximum_price_change_5m_pct: float = 40.0

    @classmethod
    def from_env(cls) -> "PumpfunEvPolicy":
        return cls(
            enabled=os.getenv("SOLANA_PUMPFUN_EV_ENABLED", "true").lower() == "true",
            target_market_cap_usd=float(os.getenv("SOLANA_PUMPFUN_EV_TARGET_MCAP_USD", "25000")),
            maximum_entry_market_cap_usd=float(os.getenv("SOLANA_PUMPFUN_EV_MAX_ENTRY_MCAP_USD", "15000")),
            minimum_trades_5m=max(10, int(os.getenv("SOLANA_PUMPFUN_EV_MIN_TRADES_5M", "10"))),
            maximum_age_minutes=min(20.0, float(os.getenv("SOLANA_PUMPFUN_EV_MAX_AGE_MINUTES", "20"))),
            assumed_loss_fraction=clamp(float(os.getenv("SOLANA_PUMPFUN_EV_ASSUMED_LOSS", "0.50")), .10, .90),
            minimum_ev_rank=max(0.35, float(os.getenv("SOLANA_PUMPFUN_EV_MIN_EV_RANK", "0.35"))),
            minimum_stressed_expectancy=max(0.15, float(os.getenv("SOLANA_PUMPFUN_EV_MIN_STRESSED_EXPECTANCY", "0.15"))),
        )


@dataclass(frozen=True)
class MicrocapLaunchPolicy:
    """Paper-only fast momentum test for newly created Solana pools."""

    enabled: bool = True
    minimum_volume_24h_usd: float = 100_000.0
    minimum_market_cap_usd: float = 1_000_000.0
    watch_minimum_volume_24h_usd: float = 5_000.0
    minimum_liquidity_usd: float = 10_000.0
    maximum_age_minutes: float = 30.0
    minimum_trades_5m: int = 25
    minimum_unique_buyers_5m: int = 15
    minimum_net_buy_pressure: float = 0.25
    minimum_buyer_acceleration: float = 1.30
    minimum_volume_acceleration: float = 1.30
    minimum_price_change_5m_pct: float = 5.0
    maximum_price_change_5m_pct: float = 30.0
    maximum_sell_price_impact_bps: float = 150.0
    maximum_top10_holder_fraction: float = 0.70
    maximum_creator_fraction: float = 0.20

    @classmethod
    def from_env(cls) -> "MicrocapLaunchPolicy":
        return cls(
            enabled=os.getenv("SOLANA_MICROCAP_LAUNCH_ENABLED", "true").lower() == "true",
            # $100k is a non-overridable evidence floor. The scanner may retain
            # earlier pools, but the paper executor cannot enter them yet.
            minimum_volume_24h_usd=max(100_000.0, float(os.getenv("SOLANA_MICROCAP_MIN_VOLUME_24H_USD", "100000"))),
            minimum_market_cap_usd=max(
                1_000_000.0,
                float(os.getenv("SOLANA_MICROCAP_MIN_MARKET_CAP_USD", "1000000")),
            ),
            watch_minimum_volume_24h_usd=max(5_000.0, float(os.getenv("SOLANA_MICROCAP_WATCH_MIN_VOLUME_24H_USD", "5000"))),
            minimum_liquidity_usd=max(7_500.0, float(os.getenv("SOLANA_MICROCAP_MIN_LIQUIDITY_USD", "10000"))),
            maximum_age_minutes=min(60.0, max(5.0, float(os.getenv("SOLANA_MICROCAP_MAX_AGE_MINUTES", "30")))),
        )


@dataclass(frozen=True)
class RunnerCapturePolicy:
    """Paper-only cohort for already-confirmed, unusually strong launch runners."""

    enabled: bool = True
    minimum_volume_24h_usd: float = 100_000.0
    minimum_market_cap_usd: float = 1_000_000.0
    minimum_liquidity_usd: float = 10_000.0
    maximum_age_minutes: float = 60.0
    minimum_trades_5m: int = 20
    minimum_unique_buyers_5m: int = 12
    minimum_net_buy_pressure: float = 0.15
    minimum_price_change_5m_pct: float = 15.0
    maximum_price_change_5m_pct: float = 200.0
    minimum_return_since_seen: float = 0.20
    maximum_retracement_from_high: float = 0.10
    maximum_sell_price_impact_bps: float = 150.0
    maximum_top10_holder_fraction: float = 0.80
    maximum_creator_fraction: float = 0.30

    @classmethod
    def from_env(cls) -> "RunnerCapturePolicy":
        return cls(
            enabled=os.getenv("SOLANA_RUNNER_CAPTURE_ENABLED", "true").lower() == "true",
            minimum_volume_24h_usd=max(
                100_000.0, float(os.getenv("SOLANA_RUNNER_MIN_VOLUME_24H_USD", "100000"))
            ),
            minimum_market_cap_usd=max(
                1_000_000.0,
                float(os.getenv("SOLANA_RUNNER_MIN_MARKET_CAP_USD", "1000000")),
            ),
            minimum_liquidity_usd=max(
                7_500.0, float(os.getenv("SOLANA_RUNNER_MIN_LIQUIDITY_USD", "10000"))
            ),
            maximum_age_minutes=min(
                90.0, max(10.0, float(os.getenv("SOLANA_RUNNER_MAX_AGE_MINUTES", "60")))
            ),
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
            CREATE TABLE IF NOT EXISTS candidate_watchlist(
              strategy TEXT NOT NULL, mint TEXT NOT NULL, pool TEXT NOT NULL,
              first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
              initial_price REAL NOT NULL, latest_price REAL NOT NULL,
              max_price REAL NOT NULL, payload TEXT NOT NULL,
              checkpoints TEXT NOT NULL, status TEXT NOT NULL,
              PRIMARY KEY(strategy,mint));
            """)

    def upsert_watch_candidate(self, candidate: dict[str, Any], strategy: str,
                               status: str = "WATCHING") -> bool:
        mint, pool = str(candidate.get("mint") or ""), str(candidate.get("pool") or "")
        price = _number(candidate.get("price_usd"), 0)
        if not mint or not pool or price <= 0:
            return False
        now = str(candidate.get("observed_at") or utcnow())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT first_seen_at,initial_price,max_price,checkpoints,payload FROM candidate_watchlist WHERE strategy=? AND mint=?",
                (strategy, mint)).fetchone()
            first, initial, maximum, checkpoints = (now, price, price, {}) if not row else (
                str(row[0]), float(row[1]), max(float(row[2]), price), json.loads(row[3] or "{}"))
            prior_payload = json.loads(row[4] or "{}") if row else {}
            first_candidate = (prior_payload.get("first_candidate")
                               if isinstance(prior_payload, dict) else None)
            if not isinstance(first_candidate, dict):
                first_candidate = prior_payload if row and isinstance(prior_payload, dict) else candidate
            retained_payload = {"first_candidate": first_candidate, "latest_candidate": candidate}
            elapsed = max(0, (datetime.fromisoformat(now.replace("Z", "+00:00")) -
                              datetime.fromisoformat(first.replace("Z", "+00:00"))).total_seconds() / 60)
            for minute in (5, 15, 30, 60):
                if elapsed >= minute and str(minute) not in checkpoints:
                    checkpoints[str(minute)] = round(price / initial - 1, 6)
            self.db.execute("""INSERT INTO candidate_watchlist VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy,mint) DO UPDATE SET pool=excluded.pool,last_seen_at=excluded.last_seen_at,
                latest_price=excluded.latest_price,max_price=excluded.max_price,payload=excluded.payload,
                checkpoints=excluded.checkpoints,status=excluded.status""",
                (strategy, mint, pool, first, now, initial, price, maximum,
                 json.dumps(retained_payload, sort_keys=True),
                 json.dumps(checkpoints, sort_keys=True), status))
        return row is None

    def watched_pools(self, strategy: str, maximum_age_minutes: int = 60,
                      limit: int = 100) -> list[str]:
        cutoff = datetime.fromtimestamp(time.time() - maximum_age_minutes * 60, UTC).isoformat()
        with self.lock:
            # Refresh both newly observed pools and the strongest developing
            # runners. Ordering only by last_seen_at starved a fast runner once
            # enough newer pools entered the watchlist.
            recent = self.db.execute("""SELECT pool FROM candidate_watchlist
                WHERE strategy=? AND first_seen_at>=? ORDER BY last_seen_at DESC LIMIT ?""",
                (strategy, cutoff, max(1, limit // 2))).fetchall()
            strongest = self.db.execute("""SELECT pool FROM candidate_watchlist
                WHERE strategy=? AND first_seen_at>=?
                ORDER BY (latest_price / MAX(initial_price, 1e-30)) DESC,
                         last_seen_at DESC LIMIT ?""",
                (strategy, cutoff, limit)).fetchall()
        return list(dict.fromkeys(str(row[0]) for row in [*recent, *strongest]
                                  if row[0]))[:limit]

    def is_watched(self, strategy: str, mint: str) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM candidate_watchlist WHERE strategy=? AND mint=?", (strategy, mint)).fetchone()
        return row is not None

    def watchlist_snapshot(self, strategy: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("""SELECT mint,pool,first_seen_at,last_seen_at,initial_price,
                latest_price,max_price,payload,checkpoints,status FROM candidate_watchlist
                WHERE strategy=? ORDER BY last_seen_at DESC LIMIT ?""", (strategy, limit)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            payload = json.loads(item.pop("payload") or "{}")
            item["first_candidate"] = payload.get("first_candidate", payload)
            item["latest_candidate"] = payload.get("latest_candidate", payload)
            item["return_since_seen"] = round(float(row["latest_price"]) /
                                               max(float(row["initial_price"]), 1e-30) - 1, 6)
            item["retracement_from_high"] = round(
                max(0.0, 1 - float(row["latest_price"]) / max(float(row["max_price"]), 1e-30)), 6)
            item["checkpoints"] = json.loads(row["checkpoints"] or "{}")
            output.append(item)
        return output

    def watched_candidate(self, strategy: str, mint: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("""SELECT mint,pool,first_seen_at,last_seen_at,initial_price,
                latest_price,max_price,payload,checkpoints,status FROM candidate_watchlist
                WHERE strategy=? AND mint=?""", (strategy, mint)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"] or "{}")
        return {
            **dict(row),
            "first_candidate": payload.get("first_candidate", payload),
            "latest_candidate": payload.get("latest_candidate", payload),
            "return_since_seen": float(row["latest_price"]) / max(float(row["initial_price"]), 1e-30) - 1,
            "retracement_from_high": max(
                0.0, 1 - float(row["latest_price"]) / max(float(row["max_price"]), 1e-30)
            ),
            "checkpoints": json.loads(row["checkpoints"] or "{}"),
        }

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

    def wallet_evidence(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("""SELECT wallet,mint,side,quantity,observed_at,signature,payload
            FROM wallet_events ORDER BY observed_at DESC LIMIT ?""", (limit,)).fetchall()
        evidence = []
        for row in rows:
            item = dict(row); payload = json.loads(item.pop("payload") or "{}")
            item["quote_usdc"] = payload.get("quote_usdc")
            item["wallet_stats"] = self.wallet_stats(str(item["wallet"]))
            evidence.append(item)
        return evidence


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
    if candidate.get("safety_evidence_status") != "VERIFIED":
        failures.append("verified safety evidence missing")
    required_false = ("mint_authority_active", "freeze_authority_active", "transfer_hook_active",
                      "non_transferable", "creator_selling")
    for field in required_false:
        if field not in candidate:
            failures.append(f"{field} missing")
        elif candidate.get(field) is True:
            failures.append(field)
    if candidate.get("sell_simulation_ok") is not True:
        failures.append("sell simulation failed")
    if candidate.get("top10_holder_fraction") is None:
        failures.append("top-10 concentration unavailable")
    elif _number(candidate.get("top10_holder_fraction")) > policy.maximum_top10_holder_fraction:
        failures.append("top-10 concentration too high")
    if candidate.get("creator_fraction") is None:
        failures.append("creator concentration unavailable")
    elif _number(candidate.get("creator_fraction")) > policy.maximum_creator_fraction:
        failures.append("creator concentration too high")
    return failures


def contract_safety_failures(candidate: dict[str, Any]) -> list[str]:
    """Non-negotiable controls shared by paper exploration and live selection."""
    failures: list[str] = []
    if candidate.get("safety_evidence_status") not in {"VERIFIED", "ONCHAIN_VERIFIED"}:
        failures.append("verified safety evidence missing")
    for field in ("mint_authority_active", "freeze_authority_active", "transfer_hook_active",
                  "non_transferable", "creator_selling"):
        if field not in candidate:
            failures.append(f"{field} missing")
        elif candidate.get(field) is True:
            failures.append(field)
    if candidate.get("sell_simulation_ok") is not True:
        failures.append("sell simulation failed")
    return failures


def score_candidate(candidate: dict[str, Any], ledger: Ledger, policy: EarlyPolicy) -> dict[str, Any]:
    failures = safety_failures(candidate, policy)
    age = _number(candidate.get("token_age_minutes"), 9999)
    liquidity = _number(candidate.get("liquidity_usd"), 0)
    buyers = int(candidate.get("unique_buyers_5m", 0))
    impact = _number(candidate.get("sell_price_impact_bps"), 9999)
    if not 2 <= age <= policy.maximum_token_age_minutes:
        failures.append("token age outside discovery window")
    if liquidity < policy.minimum_liquidity_usd:
        failures.append("liquidity below minimum")
    if buyers < policy.minimum_unique_buyers_5m:
        failures.append("unique buyers below minimum")
    if impact > policy.maximum_price_impact_bps:
        failures.append("sell price impact above maximum")

    buyer_accel = ratio(buyers, int(candidate.get("unique_buyers_previous_5m", 0)))
    volume_accel = ratio(_number(candidate.get("buy_volume_5m_usd")),
                         _number(candidate.get("buy_volume_previous_5m_usd")))
    total = _number(candidate.get("buy_volume_5m_usd")) + _number(candidate.get("sell_volume_5m_usd"))
    pressure = (_number(candidate.get("transaction_buy_pressure"))
                if "transaction_buy_pressure" in candidate else
                ((_number(candidate.get("buy_volume_5m_usd")) -
                  _number(candidate.get("sell_volume_5m_usd"))) / total) if total else -1.0)
    wallet_points, qualified_wallets = smart_wallet_score(candidate.get("buyer_wallets") or [], ledger)
    top10_raw, creator_raw = candidate.get("top10_holder_fraction"), candidate.get("creator_fraction")
    top10 = _number(top10_raw) if top10_raw is not None else None
    creator = _number(creator_raw) if creator_raw is not None else None

    components = {
        "safety": 25.0 if not safety_failures(candidate, policy) else 0.0,
        "liquidity": clamp(math.log10(max(liquidity, 1) / policy.minimum_liquidity_usd + 1) * 12, 0, 15),
        "buyer_acceleration": clamp((buyer_accel - 1) * 12, 0, 15),
        "buy_pressure": clamp((pressure - .05) * 20, 0, 12),
        "distribution": (clamp((.55 - top10) * 20 + (.12 - creator) * 25, 0, 10)
                         if top10 is not None and creator is not None else 0.0),
        "smart_wallets": wallet_points,
        "social_velocity": clamp(_number(candidate.get("social_velocity_ratio")) * 2, 0, 8),
        "creator_history": clamp(_number(candidate.get("creator_history_score")), 0, 5),
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
    paper_failures = contract_safety_failures(candidate)
    if not 1 <= age <= policy.paper_maximum_token_age_minutes:
        paper_failures.append("paper token age outside exploration window")
    if liquidity < policy.paper_minimum_liquidity_usd:
        paper_failures.append("paper liquidity below minimum")
    if buyers < policy.paper_minimum_unique_buyers_5m:
        paper_failures.append("paper unique buyers below minimum")
    if impact > policy.paper_maximum_price_impact_bps:
        paper_failures.append("paper sell price impact above maximum")
    if top10 is None:
        paper_failures.append("paper top-10 concentration unavailable")
    elif top10 > policy.paper_maximum_top10_holder_fraction:
        paper_failures.append("paper top-10 concentration too high")
    if creator is None:
        paper_failures.append("paper creator concentration unavailable")
    elif creator > policy.paper_maximum_creator_fraction:
        paper_failures.append("paper creator concentration too high")
    if buyer_accel < 1.20:
        paper_failures.append("paper buyers are not accelerating")
    if volume_accel < 1.20:
        paper_failures.append("paper volume is not accelerating")
    if pressure < 0.15:
        paper_failures.append("paper net buy pressure below minimum")
    price_change_5m = _number(candidate.get("price_change_5m_pct"), -999)
    if price_change_5m <= 0:
        paper_failures.append("paper five-minute price momentum is not positive")
    if score < policy.paper_minimum_score:
        paper_failures.append(f"paper score {score:.2f} below {policy.paper_minimum_score:.2f}")
    return {
        "mint": str(candidate.get("mint") or ""), "symbol": str(candidate.get("symbol") or ""),
        "price_usd": _number(candidate.get("price_usd")),
        "decimals": int(candidate.get("decimals") or 0),
        "score": score, "components": {key: round(value, 2) for key, value in components.items()},
        "buyer_acceleration": round(buyer_accel, 4), "volume_acceleration": round(volume_accel, 4),
        "net_buy_pressure": round(pressure, 4), "qualified_wallet_count": len(qualified_wallets),
        "token_age_minutes": round(age, 4), "liquidity_usd": round(liquidity, 2),
        "unique_buyers_5m": buyers, "trades_5m": int(_number(candidate.get("trades_5m"))),
        "sell_price_impact_bps": round(impact, 2),
        "top10_holder_fraction": round(top10, 6) if top10 is not None else None,
        "creator_fraction": round(creator, 6) if creator is not None else None,
        "concentration_evidence_status": ("VERIFIED" if top10 is not None and creator is not None
                                          else "UNAVAILABLE"),
        "pool_address": str(candidate.get("pool") or candidate.get("pool_address") or ""),
        "source_observed_at": str(candidate.get("observed_at") or ""),
        "source_url": str(candidate.get("source_url") or ""),
        "price_change_5m_pct": round(price_change_5m, 4),
        "price_change_15m_pct": round(_number(candidate.get("price_change_15m_pct")), 4),
        "safety_evidence_status": str(candidate.get("safety_evidence_status") or "MISSING"),
        "flow_data_provenance": str(candidate.get("flow_data_provenance") or "UNSPECIFIED"),
        "qualified": not failures, "failures": list(dict.fromkeys(failures)),
        "paper_qualified": not paper_failures,
        "paper_failures": list(dict.fromkeys(paper_failures)),
        "paper_probe_usd": policy.maximum_probe_usd if not paper_failures else 0.0,
        "mode": "PAPER_ONLY", "strategy": "SOLANA_EARLY_CONTROL",
    }


def score_pumpfun_ev_candidate(candidate: dict[str, Any], ledger: Ledger,
                               safety_policy: EarlyPolicy,
                               policy: PumpfunEvPolicy) -> dict[str, Any]:
    """Score the PumpBot idea without pretending that we have its trained model.

    The shared CoinGecko feed has 5-minute aggregates, not a 15-second event tape.
    This arm therefore uses a clearly labelled, uncalibrated probability proxy and
    is paper-only. Jupiter's executable sell evidence remains a hard gate.
    """
    control = score_candidate(candidate, ledger, safety_policy)
    # The experiment must preserve executable sellability and immutable-token
    # controls, but it must not inherit the control arm's mature-token holder
    # concentration gates. Those gates made every genuinely early Pump.fun
    # candidate ineligible and prevented the requested forward paper test from
    # collecting any observations at all.
    failures = list(contract_safety_failures(candidate))
    market_cap = _number(candidate.get("market_cap_usd"))
    age = _number(candidate.get("token_age_minutes"), 9999)
    trades_5m = int(_number(candidate.get("trades_5m")))
    impact_bps = _number(candidate.get("sell_price_impact_bps"), 9999)
    price_change_5m = _number(candidate.get("price_change_5m_pct"), -999)
    if not policy.enabled:
        failures.append("pumpfun EV strategy disabled")
    if market_cap <= 0:
        failures.append("market cap missing")
    elif market_cap >= policy.maximum_entry_market_cap_usd:
        failures.append("above pumpfun entry market-cap ceiling")
    if not 0 <= age <= policy.maximum_age_minutes:
        failures.append("outside pumpfun EV age window")
    if trades_5m < policy.minimum_trades_5m:
        failures.append("insufficient recent trades")
    if impact_bps > safety_policy.maximum_price_impact_bps:
        failures.append("sell price impact above maximum")
    if candidate.get("safety_evidence_status") != "VERIFIED":
        failures.append("verified safety evidence missing")
    if candidate.get("top10_holder_fraction") is None:
        failures.append("pumpfun top-10 concentration unavailable")
    elif _number(candidate.get("top10_holder_fraction")) > policy.maximum_top10_holder_fraction:
        failures.append("pumpfun top-10 concentration too high")
    if candidate.get("creator_fraction") is None:
        failures.append("pumpfun creator concentration unavailable")
    elif _number(candidate.get("creator_fraction")) > policy.maximum_creator_fraction:
        failures.append("pumpfun creator concentration too high")
    if control["score"] < policy.minimum_control_score:
        failures.append(f"control evidence score {control['score']:.2f} below {policy.minimum_control_score:.2f}")
    if control["buyer_acceleration"] < policy.minimum_buyer_acceleration:
        failures.append("pumpfun buyers are not accelerating")
    if control["volume_acceleration"] < policy.minimum_volume_acceleration:
        failures.append("pumpfun volume is not accelerating")
    if control["net_buy_pressure"] < policy.minimum_net_buy_pressure:
        failures.append("pumpfun net buy pressure below minimum")
    if not policy.minimum_price_change_5m_pct < price_change_5m <= policy.maximum_price_change_5m_pct:
        failures.append("pumpfun five-minute price momentum outside confirmation range")

    # This is intentionally conservative and transparent. It is a ranking
    # proxy for forward paper collection, not a claimed calibrated probability.
    flow_quality = clamp((control["buyer_acceleration"] - 1.0) / 3.0, 0.0, 1.0)
    pressure_quality = clamp((control["net_buy_pressure"] - 0.05) / 0.75, 0.0, 1.0)
    safety_quality = 1.0 if not safety_failures(candidate, safety_policy) else 0.0
    probability_proxy = clamp(.01 + .09 * flow_quality + .07 * pressure_quality + .03 * safety_quality,
                              .01, .20)
    gross_multiple = policy.target_market_cap_usd / market_cap if market_cap > 0 else 0.0
    executable_cost_fraction = .02 + impact_bps / 10_000.0
    win_return = max(-1.0, min(policy.maximum_payoff_multiple - 1.0,
                               gross_multiple * (1.0 - executable_cost_fraction) - 1.0))
    ev_rank = probability_proxy * max(0.0, win_return)
    stressed_expectancy = (probability_proxy * win_return -
                            (1.0 - probability_proxy) * policy.assumed_loss_fraction)
    if ev_rank < policy.minimum_ev_rank:
        failures.append(f"EV rank {ev_rank:.4f} below {policy.minimum_ev_rank:.4f}")
    if stressed_expectancy <= policy.minimum_stressed_expectancy:
        failures.append(
            f"cost-stressed expectancy {stressed_expectancy:.4f} is not above "
            f"{policy.minimum_stressed_expectancy:.4f}"
        )

    return {
        **control,
        "strategy": "SOLANA_PUMPFUN_EV_EXPERIMENT",
        "qualified": False,
        "failures": ["experimental strategy is never live eligible"],
        "paper_qualified": not failures,
        "paper_failures": list(dict.fromkeys(failures)),
        "paper_probe_usd": safety_policy.maximum_probe_usd if not failures else 0.0,
        "probability_proxy": round(probability_proxy, 6),
        "probability_calibrated": False,
        "entry_market_cap_usd": round(market_cap, 2),
        "target_market_cap_usd": policy.target_market_cap_usd,
        "gross_target_multiple": round(gross_multiple, 6),
        "executable_win_return": round(win_return, 6),
        "ev_rank": round(ev_rank, 6),
        "stressed_expectancy": round(stressed_expectancy, 6),
        "assumed_loss_fraction": policy.assumed_loss_fraction,
        "checkpoint": "5m_proxy",
        "live_eligible": False,
        "model_status": "UNCALIBRATED_PROXY_FORWARD_PAPER_ONLY",
        "entry_reason": (
            f"Paper-only Divine V3 confirmed entry: market cap ${market_cap:,.0f} below "
            f"${policy.maximum_entry_market_cap_usd:,.0f}; {trades_5m} trades in 5m; "
            f"five-minute price change {price_change_5m:.1f}%; net buy pressure "
            f"{control['net_buy_pressure']:.1%}; buyers/volume acceleration "
            f"{control['buyer_acceleration']:.2f}x/{control['volume_acceleration']:.2f}x; "
            f"probability proxy {probability_proxy:.1%}; executable target return "
            f"{win_return:.1%}; EV rank {ev_rank:.4f}; cost-stressed expectancy "
            f"{stressed_expectancy:.4f}; Jupiter sell impact {impact_bps:.0f} bps."
        ),
    }


def score_microcap_launch_candidate(candidate: dict[str, Any], ledger: Ledger,
                                    policy: MicrocapLaunchPolicy) -> dict[str, Any]:
    """Find sustained early runs while failing closed on safety and sellability."""
    age = _number(candidate.get("token_age_minutes"), 9999)
    volume_24h = _number(candidate.get("volume_24h_usd"), 0)
    market_cap = _number(candidate.get("market_cap_usd"), 0)
    liquidity = _number(candidate.get("liquidity_usd"), 0)
    trades = int(_number(candidate.get("trades_5m"), 0))
    buyers = int(_number(candidate.get("unique_buyers_5m"), 0))
    buyer_accel = ratio(buyers, int(_number(candidate.get("unique_buyers_previous_5m"), 0)))
    volume_accel = ratio(_number(candidate.get("buy_volume_5m_usd")),
                         _number(candidate.get("buy_volume_previous_5m_usd")))
    total_flow = (_number(candidate.get("buy_volume_5m_usd")) +
                  _number(candidate.get("sell_volume_5m_usd")))
    pressure = (_number(candidate.get("transaction_buy_pressure"))
                if "transaction_buy_pressure" in candidate else
                ((_number(candidate.get("buy_volume_5m_usd")) -
                  _number(candidate.get("sell_volume_5m_usd"))) / total_flow)
                if total_flow else -1.0)
    momentum_5m = _number(candidate.get("price_change_5m_pct"), -999)
    momentum_15m = _number(candidate.get("price_change_15m_pct"), -999)
    impact = _number(candidate.get("sell_price_impact_bps"), 9999)
    top10, creator = candidate.get("top10_holder_fraction"), candidate.get("creator_fraction")
    failures = contract_safety_failures(candidate)
    checks = (
        (not policy.enabled, "microcap launch strategy disabled"),
        (not 1 <= age <= policy.maximum_age_minutes, "microcap token age outside launch window"),
        (volume_24h < policy.minimum_volume_24h_usd,
         f"microcap 24h volume below ${policy.minimum_volume_24h_usd / 1000:.0f}k execution minimum"),
        (market_cap <= 0, "microcap market cap unavailable"),
        (0 < market_cap < policy.minimum_market_cap_usd,
         f"microcap market cap below ${policy.minimum_market_cap_usd / 1_000_000:.1f}m minimum"),
        (liquidity < policy.minimum_liquidity_usd, "microcap liquidity below minimum"),
        (trades < policy.minimum_trades_5m, "microcap recent trades below minimum"),
        (buyers < policy.minimum_unique_buyers_5m, "microcap unique buyers below minimum"),
        (pressure < policy.minimum_net_buy_pressure, "microcap net buy pressure below minimum"),
        (buyer_accel < policy.minimum_buyer_acceleration, "microcap buyers are not accelerating"),
        (volume_accel < policy.minimum_volume_acceleration, "microcap buy volume is not accelerating"),
        (not policy.minimum_price_change_5m_pct <= momentum_5m <= policy.maximum_price_change_5m_pct,
         "microcap five-minute momentum outside serious-run range"),
        (momentum_15m <= 0, "microcap fifteen-minute momentum is not positive"),
        (impact > policy.maximum_sell_price_impact_bps, "microcap executable sell impact above maximum"),
        (top10 is None, "microcap top-10 concentration unavailable"),
        (top10 is not None and _number(top10) > policy.maximum_top10_holder_fraction,
         "microcap top-10 concentration too high"),
        (creator is None, "microcap creator concentration unavailable"),
        (creator is not None and _number(creator) > policy.maximum_creator_fraction,
         "microcap creator concentration too high"),
    )
    failures.extend(reason for failed, reason in checks if failed)
    score = round(clamp(momentum_5m, 0, 20) + clamp(pressure * 30, 0, 15) +
                  clamp((buyer_accel - 1) * 15, 0, 15) +
                  clamp((volume_accel - 1) * 10, 0, 10) +
                  clamp(math.log10(max(liquidity, 1) / policy.minimum_liquidity_usd + 1) * 15, 0, 15), 2)
    return {
        "mint": str(candidate.get("mint") or ""), "symbol": str(candidate.get("symbol") or ""),
        "price_usd": _number(candidate.get("price_usd")), "decimals": int(candidate.get("decimals") or 0),
        "strategy": "SOLANA_MICROCAP_LAUNCH_MOMENTUM", "strategy_version": "MICROCAP_LAUNCH_V2",
        "mode": "PAPER_ONLY", "qualified": False, "live_eligible": False,
        "paper_qualified": not failures, "paper_failures": list(dict.fromkeys(failures)),
        "failures": ["paper-only challenger"], "score": score,
        "token_age_minutes": round(age, 4), "volume_24h_usd": round(volume_24h, 2),
        "market_cap_usd": round(market_cap, 2),
        "liquidity_usd": round(liquidity, 2), "trades_5m": trades, "unique_buyers_5m": buyers,
        "buyer_acceleration": round(buyer_accel, 4), "volume_acceleration": round(volume_accel, 4),
        "net_buy_pressure": round(pressure, 4), "price_change_5m_pct": round(momentum_5m, 4),
        "price_change_15m_pct": round(momentum_15m, 4), "sell_price_impact_bps": round(impact, 2),
        "top10_holder_fraction": _number(top10) if top10 is not None else None,
        "creator_fraction": _number(creator) if creator is not None else None,
        "safety_evidence_status": str(candidate.get("safety_evidence_status") or "MISSING"),
        "flow_data_provenance": str(candidate.get("flow_data_provenance") or "UNSPECIFIED"),
        "pool_address": str(candidate.get("pool") or ""),
        "source_observed_at": str(candidate.get("observed_at") or ""),
        "source_url": str(candidate.get("source_url") or ""), "qualified_wallet_count": 0,
        "watch_eligible": (policy.enabled and 0 <= age <= 60 and
                           volume_24h >= policy.watch_minimum_volume_24h_usd and
                           liquidity >= 7_500 and buyers >= 10 and pressure > 0 and
                           (momentum_5m >= 3 or buyer_accel >= 1.2 or volume_accel >= 1.2)),
        "entry_reason": (f"Microcap Launch V3 $1M+ rolling-confirmation entry: {age:.1f}-minute pool; "
                         f"${market_cap:,.0f} market cap; ${volume_24h:,.0f} 24h volume; "
                         f"{momentum_5m:.2f}% five-minute momentum; "
                         f"{pressure:.2f} buy pressure; {buyer_accel:.2f}x buyer and "
                         f"{volume_accel:.2f}x volume acceleration; executable sell impact {impact:.0f} bps."),
    }


def score_runner_capture_candidate(candidate: dict[str, Any], ledger: Ledger,
                                   policy: RunnerCapturePolicy) -> dict[str, Any]:
    """Test persistent explosive runners without making them live eligible."""
    watch_strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
    history = ledger.watched_candidate(watch_strategy, str(candidate.get("mint") or ""))
    age = _number(candidate.get("token_age_minutes"), 9999)
    volume_24h = _number(candidate.get("volume_24h_usd"), 0)
    market_cap = _number(candidate.get("market_cap_usd"), 0)
    liquidity = _number(candidate.get("liquidity_usd"), 0)
    trades = int(_number(candidate.get("trades_5m"), 0))
    buyers = int(_number(candidate.get("unique_buyers_5m"), 0))
    pressure = _number(candidate.get("transaction_buy_pressure"), -1)
    momentum_5m = _number(candidate.get("price_change_5m_pct"), -999)
    momentum_15m = _number(candidate.get("price_change_15m_pct"), -999)
    impact = _number(candidate.get("sell_price_impact_bps"), 9999)
    return_since_seen = _number((history or {}).get("return_since_seen"), -1)
    retracement = _number((history or {}).get("retracement_from_high"), 1)
    top10 = candidate.get("top10_holder_fraction")
    creator = candidate.get("creator_fraction")
    failures = contract_safety_failures(candidate)
    checks = (
        (not policy.enabled, "runner capture strategy disabled"),
        (history is None, "runner has no retained first-seen history"),
        (not 1 <= age <= policy.maximum_age_minutes, "runner token age outside test window"),
        (volume_24h < policy.minimum_volume_24h_usd,
         f"runner 24h volume below ${policy.minimum_volume_24h_usd / 1000:.0f}k minimum"),
        (market_cap <= 0, "runner market cap unavailable"),
        (0 < market_cap < policy.minimum_market_cap_usd,
         f"runner market cap below ${policy.minimum_market_cap_usd / 1_000_000:.1f}m minimum"),
        (liquidity < policy.minimum_liquidity_usd, "runner liquidity below minimum"),
        (trades < policy.minimum_trades_5m, "runner recent trades below minimum"),
        (buyers < policy.minimum_unique_buyers_5m, "runner unique buyers below minimum"),
        (pressure < policy.minimum_net_buy_pressure, "runner net buy pressure below minimum"),
        (not policy.minimum_price_change_5m_pct <= momentum_5m <= policy.maximum_price_change_5m_pct,
         "runner five-minute momentum outside explosive range"),
        (momentum_15m <= 0, "runner fifteen-minute momentum is not positive"),
        (return_since_seen < policy.minimum_return_since_seen,
         "runner has not gained 20% since first observation"),
        (retracement > policy.maximum_retracement_from_high,
         "runner has already retraced more than 10% from its observed high"),
        (impact > policy.maximum_sell_price_impact_bps, "runner executable sell impact above maximum"),
        (top10 is not None and _number(top10) > policy.maximum_top10_holder_fraction,
         "runner top-10 concentration too high"),
        (creator is not None and _number(creator) > policy.maximum_creator_fraction,
         "runner creator concentration too high"),
    )
    failures.extend(reason for failed, reason in checks if failed)
    # Real-money probes must satisfy the same sellability constraints as paper
    # entries. A missing route or excessive executable impact is evidence that
    # the position may not be recoverable, not a condition to test with funds.
    live_probe_failures = list(failures)
    score = round(clamp(momentum_5m, 0, 100) * .35 +
                  clamp(return_since_seen * 100, 0, 100) * .30 +
                  clamp(pressure * 100, 0, 100) * .20 +
                  clamp(math.log10(max(liquidity, 1) / policy.minimum_liquidity_usd + 1) * 50,
                        0, 15), 2)
    distribution_status = ("VERIFIED" if top10 is not None and creator is not None
                           else "UNAVAILABLE_PAPER_ONLY")
    return {
        "mint": str(candidate.get("mint") or ""),
        "symbol": str(candidate.get("symbol") or ""),
        "price_usd": _number(candidate.get("price_usd")),
        "market_cap_usd": round(market_cap, 2),
        "decimals": int(candidate.get("decimals") or 0),
        "strategy": "SOLANA_MICROCAP_RUNNER_CAPTURE",
        "strategy_version": "RUNNER_CAPTURE_V1",
        "mode": ("LIVE_LIQUIDITY_PROBE_ELIGIBLE" if not live_probe_failures
                 else "PAPER_ONLY"),
        "qualified": False,
        "live_eligible": False,
        "live_probe_qualified": not live_probe_failures,
        "live_probe_failures": list(dict.fromkeys(live_probe_failures)),
        "paper_qualified": not failures,
        "paper_failures": list(dict.fromkeys(failures)),
        "failures": ["normal live trading disabled; separately capped liquidity probe only"],
        "score": score,
        "token_age_minutes": round(age, 4),
        "volume_24h_usd": round(volume_24h, 2),
        "liquidity_usd": round(liquidity, 2),
        "trades_5m": trades,
        "unique_buyers_5m": buyers,
        "net_buy_pressure": round(pressure, 4),
        "price_change_5m_pct": round(momentum_5m, 4),
        "price_change_15m_pct": round(momentum_15m, 4),
        "return_since_seen": round(return_since_seen, 6),
        "retracement_from_high": round(retracement, 6),
        "first_seen_at": str((history or {}).get("first_seen_at") or ""),
        "sell_price_impact_bps": round(impact, 2),
        "top10_holder_fraction": _number(top10) if top10 is not None else None,
        "creator_fraction": _number(creator) if creator is not None else None,
        "distribution_evidence_status": distribution_status,
        "safety_evidence_status": str(candidate.get("safety_evidence_status") or "MISSING"),
        "flow_data_provenance": str(candidate.get("flow_data_provenance") or "UNSPECIFIED"),
        "pool_address": str(candidate.get("pool") or ""),
        "source_observed_at": str(candidate.get("observed_at") or ""),
        "source_url": str(candidate.get("source_url") or ""),
        "qualified_wallet_count": 0,
        "entry_reason": (
            f"Runner Capture V1 paper entry: {return_since_seen:.1%} since first observation; "
            f"{momentum_5m:.1f}% five-minute and {momentum_15m:.1f}% fifteen-minute momentum; "
            f"${volume_24h:,.0f} volume; ${liquidity:,.0f} liquidity; {pressure:.1%} buy pressure; "
            f"{retracement:.1%} retracement from high; executable sell impact {impact:.0f} bps; "
            f"distribution evidence {distribution_status}."
        ),
    }


def strategy_diagnostics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Explain why each paper strategy did or did not produce candidates."""
    diagnostics: dict[str, Any] = {}
    for strategy in ("SOLANA_EARLY_CONTROL", "SOLANA_PUMPFUN_EV_EXPERIMENT",
                     "SOLANA_MICROCAP_LAUNCH_MOMENTUM", "SOLANA_MICROCAP_RUNNER_CAPTURE"):
        rows = [item for item in results if item.get("strategy") == strategy]
        failure_counts: dict[str, int] = {}
        for row in rows:
            for reason in row.get("paper_failures") or []:
                key = str(reason)
                failure_counts[key] = failure_counts.get(key, 0) + 1
        diagnostics[strategy] = {
            "evaluated": len(rows),
            "paper_qualified": sum(item.get("paper_qualified") is True for item in rows),
            "top_rejections": [
                {"reason": reason, "count": count}
                for reason, count in sorted(failure_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
            ],
        }
    return diagnostics


def json_request(url: str, headers: dict[str, str] | None = None, timeout: float | None = None) -> Any:
    values = {"Accept": "application/json", "User-Agent": "primus-solana-early/1.1", **(headers or {})}
    effective_timeout = timeout or max(5.0, min(30.0, float(os.getenv("SOLANA_EARLY_HTTP_TIMEOUT_SECONDS", "12"))))
    attempts = max(1, min(4, int(os.getenv("SOLANA_EARLY_HTTP_ATTEMPTS", "3"))))
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=values), timeout=effective_timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt + 1 >= attempts:
                raise
            try:
                retry_after = float(exc.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            time.sleep(min(15.0, max(retry_after, 2.0 * (attempt + 1))))
    raise RuntimeError("HTTP retry loop ended without a response")


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
    global GOPLUS_LAST_REQUEST
    query = urllib.parse.urlencode({"contract_addresses": mint})
    payload = None
    last_error: Exception | None = None
    # A missing safety response rejects this token. One bounded request keeps a
    # complete discovery cycle from being held open by a degraded provider.
    for attempt in range(1):
        try:
            with GOPLUS_LOCK:
                spacing = max(.1, min(2.0, float(os.getenv("SOLANA_GOPLUS_REQUEST_SPACING_SECONDS", ".4"))))
                remaining = spacing - (time.monotonic() - GOPLUS_LAST_REQUEST)
                if remaining > 0:
                    time.sleep(remaining)
                payload = json_request(
                    f"https://api.gopluslabs.io/api/v1/solana/token_security?{query}", timeout=5.0)
                GOPLUS_LAST_REQUEST = time.monotonic()
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(.5 * (attempt + 1))
    if payload is None:
        raise RuntimeError(f"GoPlus safety unavailable after retries: {last_error}")
    if int(_number(payload.get("code"), 0)) != 1:
        raise RuntimeError(f"GoPlus safety provider error: code={payload.get('code')} message={payload.get('message')}")
    values = payload.get("result") or {}
    facts = values.get(mint) or values.get(mint.lower()) or (next(iter(values.values())) if len(values) == 1 else {})
    if not isinstance(facts, dict) or not facts:
        raise RuntimeError("GoPlus safety response contained no token facts")
    holders = facts.get("holders") or []
    creators = facts.get("creators") or facts.get("creator") or []
    if isinstance(creators, dict):
        creators = [creators]
    def active(field: str) -> bool:
        if field not in facts:
            raise RuntimeError(f"GoPlus safety fact missing: {field}")
        value = facts.get(field)
        if isinstance(value, dict):
            value = value.get("status")
        elif isinstance(value, (list, tuple, set)):
            return bool(value)
        return str(value).strip().lower() not in {"0", "false", "none", "null", ""}

    def fraction(values: list[dict[str, Any]]) -> float:
        total = sum(_number(item.get("percent")) for item in values if isinstance(item, dict))
        # Providers may encode percentages as either fractions (0.25) or
        # percentage points (25). Normalize while failing closed on nonsense.
        return total / 100 if 1 < total <= 100 else total
    return {
        "mint_authority_active": active("mintable"),
        "freeze_authority_active": active("freezable"),
        "transfer_hook_active": active("transfer_hook"),
        "non_transferable": active("non_transferable"),
        "top10_holder_fraction": fraction(holders[:10]) if holders else None,
        "creator_fraction": fraction(creators) if creators else None,
        "creator_selling": any(str(item.get("sell_all", "0")).lower() not in {"0", "false"}
                               for item in creators if isinstance(item, dict)),
        "safety_evidence_status": "VERIFIED",
    }


def solana_rpc_mint_safety(mint: str) -> dict[str, Any]:
    """Verify immutable mint controls without trusting a market-data provider.

    This fallback intentionally verifies only facts available from the mint
    account. Holder distribution and creator behaviour remain unavailable, so
    normal strategies still fail closed. The separately capped $0.50 liquidity
    probe may use these immutable on-chain facts when GoPlus is unavailable.
    """
    url = (os.getenv("SOLANA_SAFETY_RPC_URL") or os.getenv("SOLANA_RPC_URL") or
           "https://api.mainnet-beta.solana.com").strip()
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    }).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "primus-solana-early/1.2"})
    with urllib.request.urlopen(request, timeout=6.0) as response:
        payload = json.loads(response.read().decode())
    value = (payload.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError("Solana RPC returned no mint account")
    owner = str(value.get("owner") or "")
    parsed = ((value.get("data") or {}).get("parsed") or {})
    info = parsed.get("info") or {}
    if parsed.get("type") != "mint" or owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
        raise RuntimeError("Solana RPC account is not a supported token mint")
    extensions = info.get("extensions") or parsed.get("extensions") or []
    extension_types = {
        str(item.get("extension") or item.get("extensionType") or item.get("type") or "").lower()
        for item in extensions if isinstance(item, dict)
    }
    if owner == TOKEN_2022_PROGRAM_ID and not isinstance(extensions, list):
        raise RuntimeError("Token-2022 extensions were not parsed")
    return {
        "mint_authority_active": info.get("mintAuthority") is not None,
        "freeze_authority_active": info.get("freezeAuthority") is not None,
        "transfer_hook_active": any("transferhook" in value for value in extension_types),
        "non_transferable": any("nontransferable" in value for value in extension_types),
        "top10_holder_fraction": None,
        "creator_fraction": None,
        "creator_selling": None,
        "safety_evidence_status": "ONCHAIN_VERIFIED",
        "safety_evidence_source": "SOLANA_RPC_MINT_ACCOUNT",
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


def public_onchain_candidates(ledger: Ledger) -> list[dict[str, Any]]:
    """Discover Solana pools without consuming CoinGecko account credits.

    GeckoTerminal's public API exposes the same new-pools JSON:API resource.
    Keep this path deliberately unauthenticated: an exhausted CoinGecko key in
    Railway must never stop paper discovery or silently consume a paid quota.
    """
    base = os.getenv("GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2").rstrip("/")
    headers = {"Accept": "application/json", "User-Agent": "primus-solana-early/1.0"}
    pages = max(1, min(5, int(os.getenv("SOLANA_EARLY_MARKET_PAGES", "3"))))
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    for page in range(1, pages + 1):
        try:
            payloads.append(json_request(
                f"{base}/networks/solana/new_pools?include=base_token,quote_token,dex&page={page}", headers))
        except Exception as exc:
            errors.append(f"page {page}: {exc}")
    if not payloads:
        raise RuntimeError("all public GeckoTerminal new-pool pages failed: " + "; ".join(errors))
    now = datetime.now(UTC)
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_pools: set[str] = set()
    for payload in payloads:
        for row in payload.get("data") or []:
            pool = str((row.get("attributes") or {}).get("address") or "")
            if pool and pool not in seen_pools:
                seen_pools.add(pool)
                rows.append((row, payload))

    # A promising pool can disappear from the newest-pools pages before it
    # reaches executable evidence. Refresh retained pools directly for an hour.
    strategy = "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
    # Twenty direct pool refreshes plus three discovery pages remain inside the
    # public 30-request/minute GeckoTerminal allowance.
    refresh_limit = max(10, min(20, int(os.getenv("SOLANA_MICROCAP_WATCH_REFRESH_LIMIT", "20"))))
    for pool in ledger.watched_pools(strategy, limit=refresh_limit):
        if pool in seen_pools:
            continue
        try:
            payload = json_request(
                f"{base}/networks/solana/pools/{pool}?include=base_token,quote_token,dex", headers)
            row = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(row, dict):
                seen_pools.add(pool)
                rows.insert(0, (row, payload))
        except Exception as exc:
            errors.append(f"watched pool {pool}: {exc}")

    def enrich(item: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any] | None:
        row, payload = item
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
            return None
        tx = attributes.get("transactions") or {}
        volume = attributes.get("volume_usd") or {}
        buys5, sells5 = _count(tx, "m5", "buys"), _count(tx, "m5", "sells")
        buyers5 = _count(tx, "m5", "buyers") or buys5
        buyers15 = _count(tx, "m15", "buyers") or _count(tx, "m15", "buys")
        volume5 = _number(volume.get("m5"))
        volume15 = _number(volume.get("m15"))
        try:
            safety = goplus_safety(mint)
        except Exception as exc:
            try:
                safety = {
                    **solana_rpc_mint_safety(mint),
                    "safety_evidence_error": f"GoPlus unavailable; RPC fallback used: {exc}"[:180],
                }
            except Exception as rpc_exc:
                # Missing safety evidence must reject only this token, not stop
                # the complete discovery cycle.
                safety = {
                    "mint_authority_active": None, "freeze_authority_active": None,
                    "transfer_hook_active": None, "non_transferable": None,
                    "top10_holder_fraction": None, "creator_fraction": None,
                    "creator_selling": None,
                    "safety_evidence_status": "UNAVAILABLE",
                    "safety_evidence_error":
                        f"GoPlus: {exc}; Solana RPC: {rpc_exc}"[:180],
                }
        try:
            sell_ok, sell_impact = jupiter_sell_check(
                mint, int(base_token.get("decimals") or 0),
                _number(attributes.get("base_token_price_usd")))
        except Exception:
            # New Pump.fun tokens often have no Jupiter route yet. They are
            # ineligible until a real sell route exists.
            sell_ok, sell_impact = False, 9999.0
        return {
            "mint": mint, "pool": pool, "symbol": str(base_token.get("symbol") or ""),
            "source_url": f"{base}/networks/solana/pools/{pool}",
            "price_usd": _number(attributes.get("base_token_price_usd")),
            "decimals": int(base_token.get("decimals") or 0),
            "observed_at": utcnow(), "token_age_minutes": age,
            "liquidity_usd": _number(attributes.get("reserve_in_usd")),
            "volume_24h_usd": _number(volume.get("h24")),
            "market_cap_usd": _number(attributes.get("market_cap_usd") or attributes.get("fdv_usd")),
            "trades_5m": buys5 + sells5,
            "unique_buyers_5m": buyers5,
            "unique_buyers_previous_5m": max(0, (buyers15 - buyers5) // 2),
            "buy_volume_5m_usd": volume5 * buys5 / max(buys5 + sells5, 1),
            "sell_volume_5m_usd": volume5 * sells5 / max(buys5 + sells5, 1),
            "buy_volume_previous_5m_usd": max(0, volume15 - volume5) / 2,
            "transaction_buy_pressure": (buys5 - sells5) / max(buys5 + sells5, 1),
            "price_change_5m_pct": _number((attributes.get("price_change_percentage") or {}).get("m5")),
            "price_change_15m_pct": _number((attributes.get("price_change_percentage") or {}).get("m15")),
            "flow_data_provenance": "GECKOTERMINAL_ROLLING_WINDOWS; buy/sell volume split estimated by transaction count",
            **safety,
            "sell_simulation_ok": sell_ok, "sell_price_impact_bps": sell_impact,
            "social_velocity_ratio": 0, "creator_history_score": 0,
            "buyer_wallets": ledger.recent_buyers(mint),
            "quote_symbol": str(quote_token.get("symbol") or ""), "source": "geckoterminal_public",
        }

    maximum = max(20, min(60, int(os.getenv("SOLANA_EARLY_MAX_CANDIDATES", "40"))))
    workers = max(1, min(12, int(os.getenv("SOLANA_EARLY_ENRICH_WORKERS", "6"))))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(enrich, item) for item in rows[:maximum]]
        for future in as_completed(futures):
            try:
                value = future.result()
                if value:
                    results.append(value)
            except Exception:
                # A single malformed or unavailable pool must not discard
                # independently verified candidates from this scan.
                continue
    if not results:
        raise RuntimeError("no candidate pools could be enriched")
    return results


def fetch_candidates(ledger: Ledger) -> tuple[list[dict[str, Any]], str]:
    url = os.getenv("SOLANA_EARLY_FEED_URL", "").strip()
    if not url:
        return public_onchain_candidates(ledger), "geckoterminal_public"
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
                     "scan_started_at": value.get("scan_started_at", ""),
                     "scan_status": value.get("scan_status", "UNKNOWN"),
                     "feed": value.get("feed", ""), "wallet_events": value.get("wallet_events", 0),
                     "strategy_diagnostics": value.get("strategy_diagnostics", {}),
                     "microcap_watchlist_summary": value.get("microcap_watchlist_summary", {})}
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
    pumpfun_policy = PumpfunEvPolicy.from_env()
    microcap_policy = MicrocapLaunchPolicy.from_env()
    runner_policy = RunnerCapturePolicy.from_env()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Public GeckoTerminal allows 30 calls/minute. Three pages once a minute
    # leaves ample headroom and prevents stale Railway settings from flooding it.
    interval = max(60, int(os.getenv("SOLANA_EARLY_SCAN_INTERVAL_SECONDS", "60")))
    while True:
        try:
            with LOCK:
                STATE.update(scan_started_at=utcnow(), scan_status="IN_PROGRESS", error="")
            results = []
            candidates, feed = fetch_candidates(ledger)
            for candidate in candidates:
                outcome = score_candidate(candidate, ledger, policy)
                ledger.store_signal(candidate, outcome["score"], "QUALIFIED" if outcome["qualified"] else "REJECTED")
                results.append(outcome)
                pumpfun = score_pumpfun_ev_candidate(candidate, ledger, policy, pumpfun_policy)
                pumpfun_payload = {**candidate, "strategy": pumpfun["strategy"], "ev_rank": pumpfun["ev_rank"]}
                ledger.store_signal(pumpfun_payload, pumpfun["ev_rank"],
                                    "PAPER_QUALIFIED" if pumpfun["paper_qualified"] else "REJECTED")
                results.append(pumpfun)
                microcap = score_microcap_launch_candidate(candidate, ledger, microcap_policy)
                if microcap["watch_eligible"] or ledger.is_watched(microcap["strategy"], microcap["mint"]):
                    ledger.upsert_watch_candidate(
                        candidate, microcap["strategy"],
                        "QUALIFIED" if microcap["paper_qualified"] else "WATCHING")
                ledger.store_signal({**candidate, "strategy": microcap["strategy"]}, microcap["score"],
                                    "PAPER_QUALIFIED" if microcap["paper_qualified"] else "REJECTED")
                results.append(microcap)
                runner = score_runner_capture_candidate(candidate, ledger, runner_policy)
                ledger.store_signal({**candidate, "strategy": runner["strategy"]}, runner["score"],
                                    "PAPER_QUALIFIED" if runner["paper_qualified"] else "REJECTED")
                results.append(runner)
            results.sort(key=lambda item: (item.get("paper_qualified", False),
                                           item.get("ev_rank", item["score"])), reverse=True)
            diagnostics = strategy_diagnostics(results)
            watchlist = ledger.watchlist_snapshot("SOLANA_MICROCAP_LAUNCH_MOMENTUM")
            with LOCK:
                STATE.update(ok=True, scanned_at=utcnow(), scan_status="COMPLETE",
                             # Four strategy evaluations are emitted per pool.
                             # Keep all bounded results so a valid Runner Probe
                             # candidate cannot be truncated by other cohorts.
                             candidates=results[:240], error="", feed=feed,
                             strategy_diagnostics=diagnostics,
                             microcap_watchlist=watchlist,
                             microcap_watchlist_summary={
                                 "tracked": len(watchlist),
                                 "execution_volume_floor_usd": microcap_policy.minimum_volume_24h_usd,
                                 "minimum_market_cap_usd": microcap_policy.minimum_market_cap_usd,
                                 "watch_volume_floor_usd": microcap_policy.watch_minimum_volume_24h_usd,
                                 "runner_capture_enabled": runner_policy.enabled,
                                 "runner_minimum_return_since_seen":
                                     runner_policy.minimum_return_since_seen,
                                 "runner_minimum_market_cap_usd":
                                     runner_policy.minimum_market_cap_usd,
                                 "checkpoints_minutes": [5, 15, 30, 60],
                             },
                             watched_wallets=[value.strip() for value in
                                 os.getenv("SOLANA_WATCH_WALLETS", "").split(",") if value.strip()],
                             wallet_evidence=ledger.wallet_evidence(),
                             wallet_events=len(ledger.wallet_evidence()))
        except Exception as exc:
            with LOCK:
                STATE.update(ok=False, scanned_at=utcnow(), scan_status="FAILED", error=str(exc)[:500])
        time.sleep(interval)


if __name__ == "__main__":
    main()
