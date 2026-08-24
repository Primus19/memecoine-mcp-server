from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
SUPPORTED_ASSET_CLASSES = {"FOREX", "EQUITY", "OPTION"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def _truthy(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class AssetPolicy:
    paper_only: bool = True
    max_open_positions_per_sleeve: int = 1
    max_risk_usd: float = 25.0
    minimum_score: float = 75.0
    max_spread_bps: float = 35.0
    max_snapshot_age_seconds: int = 180

    @classmethod
    def from_env(cls) -> "AssetPolicy":
        # This first release deliberately cannot be switched to live execution.
        return cls(
            paper_only=True,
            max_open_positions_per_sleeve=max(1, int(os.getenv("ASSET_MAX_OPEN_POSITIONS", "1"))),
            max_risk_usd=min(250.0, float(os.getenv("ASSET_MAX_RISK_USD", "25.00"))),
            minimum_score=float(os.getenv("ASSET_MIN_SCORE", "75")),
            max_spread_bps=float(os.getenv("ASSET_MAX_SPREAD_BPS", "35")),
            max_snapshot_age_seconds=max(15, int(os.getenv("ASSET_MAX_SNAPSHOT_AGE_SECONDS", "180"))),
        )


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    asset_class: str
    strategy: str
    symbol: str
    side: str
    score: float
    reference_price: float
    quantity: float
    stop_price: float
    target_price: float
    maximum_loss_usd: float
    expires_at: str
    thesis: str
    invalidation: str
    source_urls: tuple[str, ...]
    snapshot_hash: str


class MultiAssetRejected(ValueError):
    pass


class StrategyEngine:
    asset_class = ""

    def __init__(self, policy: AssetPolicy) -> None:
        self.policy = policy

    def common_checks(self, snapshot: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        if str(snapshot.get("asset_class", "")).upper() != self.asset_class:
            failures.append("asset class mismatch")
        if not str(snapshot.get("symbol", "")).strip():
            failures.append("symbol missing")
        if float(snapshot.get("price") or 0) <= 0:
            failures.append("price is not positive")
        if float(snapshot.get("spread_bps") or 9999) > self.policy.max_spread_bps:
            failures.append("spread above policy maximum")
        if snapshot.get("tradable") is not True:
            failures.append("instrument not tradable")
        if snapshot.get("market_veto") is True:
            failures.append("market veto active")
        urls = snapshot.get("source_urls")
        if not isinstance(urls, list) or not urls or any(not str(url).startswith("https://") for url in urls):
            failures.append("HTTPS sources missing")
        try:
            observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
            age = (now_utc() - observed).total_seconds()
            if age < -5 or age > self.policy.max_snapshot_age_seconds:
                failures.append("snapshot stale")
        except Exception:
            failures.append("invalid observed_at")
        return failures

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        raise NotImplementedError

    def proposal(self, snapshot: dict[str, Any], *, strategy: str, score: float, side: str) -> Proposal:
        price = float(snapshot["price"])
        stop_distance = float(snapshot.get("stop_distance") or price * 0.01)
        if stop_distance <= 0:
            raise MultiAssetRejected("stop distance must be positive")
        risk = min(self.policy.max_risk_usd, float(snapshot.get("maximum_loss_usd") or self.policy.max_risk_usd))
        quantity = risk / stop_distance
        stop = price - stop_distance if side == "BUY" else price + stop_distance
        reward_multiple = max(1.25, float(snapshot.get("reward_multiple") or 2.0))
        target = price + stop_distance * reward_multiple if side == "BUY" else price - stop_distance * reward_multiple
        frozen = dict(snapshot)
        snapshot_hash = _hash(frozen)
        expiry_seconds = min(3600, max(30, int(snapshot.get("expiry_seconds") or 300)))
        expires_at = datetime.fromtimestamp(now_utc().timestamp() + expiry_seconds, UTC).isoformat()
        return Proposal(
            proposal_id=str(uuid.uuid4()), asset_class=self.asset_class, strategy=strategy,
            symbol=str(snapshot["symbol"]).upper(), side=side, score=round(score, 2),
            reference_price=price, quantity=quantity, stop_price=stop, target_price=target,
            maximum_loss_usd=risk, expires_at=expires_at,
            thesis=str(snapshot.get("thesis") or "")[:2000],
            invalidation=str(snapshot.get("invalidation") or "")[:1000],
            source_urls=tuple(dict.fromkeys(str(url) for url in snapshot["source_urls"])),
            snapshot_hash=snapshot_hash,
        )


class ForexEngine(StrategyEngine):
    asset_class = "FOREX"

    @staticmethod
    def alignment(snapshot: dict[str, Any]) -> tuple[str, float, str]:
        """Classify continuation versus a small pullback inside the daily trend."""
        one = float(snapshot.get("change_1h_pct") or 0)
        day = float(snapshot.get("change_24h_pct") or 0)
        trend = float(snapshot.get("trend_strength") or 0)
        if one and day and one * day > 0:
            return "CONTINUATION", 15.0, "BUY" if day > 0 else "SELL"
        pullback_limit = max(0.01, float(snapshot.get("pullback_max_1h_pct") or 0.10))
        if day and trend and day * trend > 0 and one * day < 0 and abs(one) <= pullback_limit:
            return "CONTROLLED_PULLBACK", 8.0, "BUY" if day > 0 else "SELL"
        return "CONTRADICTORY", 0.0, ""

    @classmethod
    def score(cls, snapshot: dict[str, Any]) -> float:
        trend = float(snapshot.get("trend_strength") or 0)
        liquidity = float(snapshot.get("liquidity_score") or 0)
        _, alignment_points, _ = cls.alignment(snapshot)
        return min(100.0, 25 + min(25, abs(trend) * 25) + min(20, liquidity * 20)
                   + alignment_points
                   + (15 if snapshot.get("session_liquid") is True else 0))

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        failures = self.common_checks(snapshot)
        if snapshot.get("economic_event_within_minutes", 999) < 30:
            failures.append("high-impact economic event too close")
        alignment, _, side = self.alignment(snapshot)
        score = self.score(snapshot)
        if alignment == "CONTRADICTORY":
            failures.append("timeframes contradict trend thesis")
        if score < self.policy.minimum_score:
            failures.append(f"score {score:.2f} below policy minimum {self.policy.minimum_score:.2f}")
        if failures:
            raise MultiAssetRejected("; ".join(failures))
        strategy = "FOREX_TREND_PULLBACK" if alignment == "CONTROLLED_PULLBACK" else "FOREX_TREND_CONTINUATION"
        return self.proposal(snapshot, strategy=strategy, score=score, side=side)


class EquityEngine(StrategyEngine):
    asset_class = "EQUITY"

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        failures = self.common_checks(snapshot)
        one = float(snapshot.get("change_1h_pct") or 0)
        day = float(snapshot.get("change_24h_pct") or 0)
        relative_volume = float(snapshot.get("relative_volume") or 0)
        above_vwap = snapshot.get("above_vwap") is True
        if snapshot.get("halted") is True:
            failures.append("instrument halted")
        score = 25 + (20 if one > 0 and day > 0 else 0) + min(25, relative_volume * 10) + (15 if above_vwap else 0) + (15 if snapshot.get("market_trend_positive") is True else 0)
        if one <= 0 or day <= 0:
            failures.append("positive 1h/24h momentum required")
        if score < self.policy.minimum_score:
            failures.append("score below policy minimum")
        if failures:
            raise MultiAssetRejected("; ".join(failures))
        return self.proposal(snapshot, strategy="EQUITY_MOMENTUM", score=score, side="BUY")


class DefinedRiskOptionsEngine(StrategyEngine):
    asset_class = "OPTION"
    ALLOWED_STRUCTURES = {"CALL_DEBIT_SPREAD", "PUT_DEBIT_SPREAD"}

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        failures = self.common_checks(snapshot)
        structure = str(snapshot.get("structure", "")).upper()
        if structure not in self.ALLOWED_STRUCTURES:
            failures.append("only defined-risk debit spreads are supported")
        if int(snapshot.get("days_to_expiry") or 0) < 7:
            failures.append("expiry is under seven days")
        if int(snapshot.get("open_interest") or 0) < 100:
            failures.append("open interest below 100")
        maximum_loss_per_contract = float(snapshot.get("maximum_loss_usd") or 9999)
        if maximum_loss_per_contract <= 0:
            failures.append("defined maximum loss must be positive")
        if maximum_loss_per_contract > self.policy.max_risk_usd:
            failures.append("defined maximum loss exceeds sleeve risk")
        if snapshot.get("all_legs_verified") is not True:
            failures.append("option legs not fully verified")
        score = 30 + min(20, float(snapshot.get("liquidity_score") or 0) * 20) + min(20, float(snapshot.get("edge_score") or 0) * 20) + (15 if snapshot.get("iv_context_verified") is True else 0) + (15 if snapshot.get("underlying_trend_aligned") is True else 0)
        if score < self.policy.minimum_score:
            failures.append("score below policy minimum")
        if failures:
            raise MultiAssetRejected("; ".join(failures))
        # Option quantities are whole spread contracts. The normalized feed must
        # provide the broker-confirmed maximum loss for one complete spread,
        # including the contract multiplier and estimated fees.
        contracts = int(self.policy.max_risk_usd // maximum_loss_per_contract)
        if contracts < 1:
            raise MultiAssetRejected("defined-risk spread cannot fit sleeve risk")
        proposal = self.proposal(snapshot, strategy=structure, score=score, side="BUY")
        return replace(
            proposal,
            quantity=float(contracts),
            maximum_loss_usd=round(contracts * maximum_loss_per_contract, 2),
        )


class PaperLedger:
    """Append-only JSONL ledger; it never talks to a broker."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {"event_id": str(uuid.uuid4()), "recorded_at": now_utc().isoformat(), **event}
        record["record_hash"] = _hash(record)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def open_positions(self, asset_class: str) -> int:
        positions: dict[str, bool] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        for line in lines:
            record = json.loads(line)
            if record.get("asset_class") != asset_class:
                continue
            proposal_id = str(record.get("proposal_id", ""))
            if record.get("type") == "PAPER_FILL":
                positions[proposal_id] = True
            elif record.get("type") == "PAPER_CLOSE":
                positions[proposal_id] = False
        return sum(positions.values())

    def records(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def positions(self) -> list[dict[str, Any]]:
        positions: dict[str, dict[str, Any]] = {}
        for record in self.records():
            proposal_id = str(record.get("proposal_id", ""))
            if record.get("type") == "PAPER_FILL":
                positions[proposal_id] = record
            elif record.get("type") == "PAPER_CLOSE":
                positions.pop(proposal_id, None)
        return list(positions.values())

    def close(self, proposal_id: str, price: float, reason: str) -> dict[str, Any]:
        position = next((item for item in self.positions() if item.get("proposal_id") == proposal_id), None)
        if not position:
            raise MultiAssetRejected("paper position is not open")
        side = str(position["side"])
        quantity = float(position["quantity"])
        entry = float(position["fill_price"])
        pnl = (price - entry) * quantity * (1 if side == "BUY" else -1)
        return self.append({"type": "PAPER_CLOSE", "mode": "PAPER_ONLY", "asset_class": position["asset_class"],
                            "strategy": position["strategy"], "symbol": position["symbol"],
                            "proposal_id": proposal_id, "entry_price": entry, "fill_price": price,
                            "quantity": quantity, "reason": reason, "realized_pnl_usd": round(pnl, 8)})


class MultiAssetEngine:
    def __init__(self, ledger: PaperLedger, policy: AssetPolicy | None = None) -> None:
        self.policy = policy or AssetPolicy.from_env()
        self.ledger = ledger
        self.engines = {
            "FOREX": ForexEngine(self.policy),
            "EQUITY": EquityEngine(self.policy),
            "OPTION": DefinedRiskOptionsEngine(self.policy),
        }

    def enabled(self, asset_class: str) -> bool:
        return _truthy(f"{asset_class}_ENGINE_ENABLED", False)

    def process(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        asset_class = str(snapshot.get("asset_class", "")).upper()
        if asset_class not in SUPPORTED_ASSET_CLASSES:
            raise MultiAssetRejected("unsupported asset class")
        if not self.enabled(asset_class):
            raise MultiAssetRejected(f"{asset_class} engine is disabled")
        if self.ledger.open_positions(asset_class) >= self.policy.max_open_positions_per_sleeve:
            raise MultiAssetRejected("paper sleeve position limit reached")
        proposal = self.engines[asset_class].evaluate(snapshot)
        frozen = asdict(proposal)
        self.ledger.append({"type": "PROPOSAL", **frozen, "mode": "PAPER_ONLY"})
        return self.ledger.append({"type": "PAPER_FILL", **frozen, "mode": "PAPER_ONLY", "fill_price": proposal.reference_price})
