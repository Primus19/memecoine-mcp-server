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

from .quant import conservative_probability, expected_net_value, multi_horizon_consensus
from .multi_week_crypto import STRATEGY as MULTI_WEEK_CRYPTO_STRATEGY, evaluate_candidate

UTC = timezone.utc
SUPPORTED_ASSET_CLASSES = {"FOREX", "EQUITY", "OPTION", "CRYPTO"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def _truthy(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class AssetPolicy:
    paper_only: bool = True
    max_open_positions_per_sleeve: int = 3
    max_risk_usd: float = 25.0
    minimum_score: float = 75.0
    max_spread_bps: float = 35.0
    max_snapshot_age_seconds: int = 180

    @classmethod
    def from_env(cls) -> "AssetPolicy":
        # This first release deliberately cannot be switched to live execution.
        return cls(
            paper_only=True,
            max_open_positions_per_sleeve=max(1, int(os.getenv("ASSET_MAX_OPEN_POSITIONS", "3"))),
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
    expected_net_bps: float = 0.0
    signal_probability: float = 0.0
    horizon_agreement: float = 0.0
    volatility_stop_distance: float = 0.0
    chain: str = ""
    contract: str = ""
    expected_holding_days: float = 0.0
    entry_stage: int = 1
    planned_full_quantity: float = 0.0
    research_only: bool = False
    research_cohort: str = ""
    qualification_failures: tuple[str, ...] = ()


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
        spread = snapshot.get("spread_bps")
        if float(9999 if spread is None else spread) > self.policy.max_spread_bps:
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
        agreement = float(snapshot.get("horizon_agreement") or 0)
        probability = conservative_probability(score, agreement)
        loss_bps = stop_distance / price * 10_000
        financing_rate = float((snapshot.get("long_financing_rate") if side == "BUY" else snapshot.get("short_financing_rate")) or 0)
        financing_bps = max(0.0, -financing_rate * 10_000 / 365 * max(1.0, float(snapshot.get("expected_holding_days") or 2)))
        value = expected_net_value(
            win_probability=probability,
            expected_gain_bps=loss_bps * reward_multiple,
            expected_loss_bps=loss_bps,
            spread_bps=float(snapshot.get("spread_bps") or 0),
            slippage_bps=float(snapshot.get("estimated_slippage_bps") or 0),
            financing_bps=financing_bps,
        )
        return Proposal(
            proposal_id=str(uuid.uuid4()), asset_class=self.asset_class, strategy=strategy,
            symbol=str(snapshot["symbol"]).upper(), side=side, score=round(score, 2),
            reference_price=price, quantity=quantity, stop_price=stop, target_price=target,
            maximum_loss_usd=risk, expires_at=expires_at,
            thesis=str(snapshot.get("thesis") or "")[:2000],
            invalidation=str(snapshot.get("invalidation") or "")[:1000],
            source_urls=tuple(dict.fromkeys(str(url) for url in snapshot["source_urls"])),
            snapshot_hash=snapshot_hash,
            expected_net_bps=round(value.expected_net_bps, 4),
            signal_probability=round(probability, 6),
            horizon_agreement=round(agreement, 6),
            volatility_stop_distance=round(stop_distance, 10),
            chain=str(snapshot.get("chain") or ""),
            contract=str(snapshot.get("contract") or ""),
            expected_holding_days=float(snapshot.get("expected_holding_days") or 0),
            planned_full_quantity=quantity,
        )


class ForexEngine(StrategyEngine):
    asset_class = "FOREX"

    @staticmethod
    def alignment(snapshot: dict[str, Any]) -> tuple[str, float, str]:
        """Classify continuation versus a small pullback inside the daily trend."""
        one = float(snapshot.get("change_1h_pct") or 0)
        day = float(snapshot.get("change_24h_pct") or 0)
        trend = float(snapshot.get("trend_strength") or 0)
        horizon_direction = int(snapshot.get("horizon_direction") or 0)
        horizon_agreement = float(snapshot.get("horizon_agreement") or 0)
        if horizon_direction and horizon_agreement >= .60:
            side = "BUY" if horizon_direction > 0 else "SELL"
            if not day or (day > 0) == (horizon_direction > 0):
                return "MULTI_HORIZON", 15.0, side
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
        alignment, alignment_points, _ = cls.alignment(snapshot)
        if "horizon_agreement" in snapshot:
            agreement = float(snapshot.get("horizon_agreement") or 0)
        else:
            one, day = float(snapshot.get("change_1h_pct") or 0), float(snapshot.get("change_24h_pct") or 0)
            agreement = .75 if alignment == "CONTROLLED_PULLBACK" else multi_horizon_consensus((one, day))["agreement"]
        return min(100.0, 20 + min(20, abs(trend) * 20) + min(20, liquidity * 20)
                   + alignment_points + min(15, agreement * 15)
                   + (10 if snapshot.get("session_liquid") is True else 0))

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        failures = self.common_checks(snapshot)
        if snapshot.get("economic_event_within_minutes", 999) < 30:
            failures.append("high-impact economic event too close")
        if float(snapshot.get("quote_age_seconds") or 0) > 10:
            failures.append("broker quote stale")
        if snapshot.get("session_liquid") is not True:
            failures.append("liquid trading session required")
        if "horizon_agreement" in snapshot and float(snapshot.get("horizon_agreement") or 0) < .75:
            failures.append("multi-horizon agreement below 75%")
        alignment, _, side = self.alignment(snapshot)
        score = self.score(snapshot)
        trend = float(snapshot.get("trend_strength") or 0)
        one = float(snapshot.get("change_1h_pct") or 0)
        four = float(snapshot.get("change_4h_pct") or 0)
        if abs(trend) < .05:
            failures.append("trend strength below 0.05 anti-chop floor")
        if abs(one) < .02:
            failures.append("1h move below 0.02% anti-chop floor")
        if side and trend and ((side == "BUY") != (trend > 0)):
            failures.append("proposed direction contradicts trend strength")
        if side and one and ((side == "BUY") != (one > 0)) and abs(one) > .05:
            failures.append("proposed direction contradicted by material 1h reversal")
        if side and four and ((side == "BUY") != (four > 0)):
            failures.append("proposed direction contradicted by 4h move")
        if alignment == "CONTRADICTORY":
            failures.append("timeframes contradict trend thesis")
        if score < self.policy.minimum_score:
            failures.append(f"score {score:.2f} below policy minimum {self.policy.minimum_score:.2f}")
        if failures:
            raise MultiAssetRejected("; ".join(failures))
        strategy = ("FOREX_MULTI_HORIZON_TREND" if alignment == "MULTI_HORIZON" else
                    "FOREX_TREND_PULLBACK" if alignment == "CONTROLLED_PULLBACK" else
                    "FOREX_TREND_CONTINUATION")
        proposal = self.proposal(snapshot, strategy=strategy, score=score, side=side)
        if proposal.expected_net_bps <= 0:
            raise MultiAssetRejected("estimated net expected value is not positive")
        return proposal


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


class MultiWeekCryptoEngine(StrategyEngine):
    asset_class = "CRYPTO"

    def evaluate(self, snapshot: dict[str, Any]) -> Proposal:
        failures = self.common_checks(snapshot)
        normalized = {
            **snapshot,
            "sell_impact_bps": snapshot.get("sell_impact_bps", snapshot.get("spread_bps")),
        }
        decision = evaluate_candidate(normalized)
        research_only = not decision["qualified"] and decision["research_eligible"]
        if not research_only:
            failures.extend(decision["hard_gate_failures"])
        stop_fraction = float(snapshot.get("initial_stop_fraction") or 0)
        if not .05 <= stop_fraction <= .25:
            failures.append("verified volatility stop must be between 5% and 25%")
        if failures:
            raise MultiAssetRejected("; ".join(dict.fromkeys(failures)))
        prepared = {
            **snapshot,
            "price": float(snapshot.get("executable_buy_price") or snapshot["price"]),
            "stop_distance": float(snapshot["price"]) * stop_fraction,
            "reward_multiple": max(3.0, float(snapshot.get("reward_multiple") or 3.0)),
            "expected_holding_days": max(21.0, float(snapshot.get("expected_holding_days") or 21)),
        }
        proposal = self.proposal(
            prepared, strategy=MULTI_WEEK_CRYPTO_STRATEGY,
            score=float(decision["research_score"] if research_only else decision["score"]), side="BUY")
        if research_only:
            budget = max(100.0, float(os.getenv("MULTI_WEEK_PAPER_BUDGET_USD", "1000")))
            allocation = budget * min(.10, max(.01, float(os.getenv(
                "MULTI_WEEK_RESEARCH_ALLOCATION_FRACTION", ".05"))))
            proposal = replace(proposal, quantity=allocation / proposal.reference_price,
                               maximum_loss_usd=allocation * stop_fraction)
            return replace(proposal, entry_stage=1, research_only=True,
                           research_cohort=str(decision.get("cohort") or ""),
                           qualification_failures=tuple(decision["hard_gate_failures"]))
        return replace(
            proposal, quantity=proposal.quantity * .25,
            maximum_loss_usd=proposal.maximum_loss_usd * .25,
            entry_stage=1,
            research_only=research_only,
            research_cohort=str(decision.get("cohort") or ""),
            qualification_failures=tuple(decision["hard_gate_failures"] if research_only else ()),
        )


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

    def has_open_symbol(self, asset_class: str, symbol: str) -> bool:
        wanted = str(symbol).upper()
        return any(str(item.get("asset_class") or "").upper() == asset_class.upper()
                   and str(item.get("symbol") or "").upper() == wanted
                   for item in self.positions())

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
            elif record.get("type") == "PAPER_ADD" and proposal_id in positions:
                prior = positions[proposal_id]
                prior_quantity = float(prior.get("quantity") or 0)
                added_quantity = float(record.get("added_quantity") or 0)
                total = prior_quantity + added_quantity
                positions[proposal_id] = {
                    **prior,
                    "quantity": total,
                    "fill_price": ((float(prior.get("fill_price") or 0) * prior_quantity +
                                    float(record.get("fill_price") or 0) * added_quantity) /
                                   max(total, 1e-30)),
                    "entry_stage": int(record.get("entry_stage") or prior.get("entry_stage") or 1),
                }
            elif record.get("type") == "PAPER_MARK" and proposal_id in positions:
                positions[proposal_id]["peak_executable_price"] = max(
                    float(positions[proposal_id].get("peak_executable_price") or
                          positions[proposal_id].get("fill_price") or 0),
                    float(record.get("mark_price") or 0),
                )
            elif record.get("type") == "PAPER_PARTIAL_CLOSE" and proposal_id in positions:
                positions[proposal_id] = {
                    **positions[proposal_id],
                    "quantity": float(record.get("remaining_quantity") or 0),
                    "took_2r_profit": (positions[proposal_id].get("took_2r_profit") is True or
                                       record.get("profit_tier") == "2R"),
                    "took_5r_profit": (positions[proposal_id].get("took_5r_profit") is True or
                                       record.get("profit_tier") == "5R"),
                    "took_10r_profit": (positions[proposal_id].get("took_10r_profit") is True or
                                        record.get("profit_tier") == "10R"),
                }
            elif record.get("type") == "PAPER_CLOSE":
                positions.pop(proposal_id, None)
        return list(positions.values())

    def mark(self, position: dict[str, Any], price: float, source: str = "CURRENT_EXECUTABLE_MARK") -> dict[str, Any]:
        if price <= 0:
            raise MultiAssetRejected("paper mark price is not positive")
        side = str(position["side"])
        quantity = float(position["quantity"])
        entry = float(position["fill_price"])
        pnl = (price - entry) * quantity * (1 if side == "BUY" else -1)
        return self.append({"type": "PAPER_MARK", "mode": "PAPER_ONLY",
                            "asset_class": position["asset_class"], "strategy": position["strategy"],
                            "symbol": position["symbol"], "proposal_id": position["proposal_id"],
                            "mark_price": price, "unrealized_pnl_usd": round(pnl, 8),
                            "price_source": source})

    def add_stage(self, proposal_id: str, price: float, stage: int) -> dict[str, Any]:
        position = next((item for item in self.positions() if item.get("proposal_id") == proposal_id), None)
        if not position:
            raise MultiAssetRejected("paper position is not open")
        current_stage = int(position.get("entry_stage") or 1)
        if stage != current_stage + 1 or stage not in {2, 3}:
            raise MultiAssetRejected("paper add must advance exactly one entry stage")
        full = float(position.get("planned_full_quantity") or 0)
        if full <= 0:
            raise MultiAssetRejected("planned full quantity unavailable")
        added = full * (.25 if stage == 2 else .50)
        return self.append({
            "type": "PAPER_ADD", "mode": "PAPER_ONLY",
            "asset_class": position["asset_class"], "strategy": position["strategy"],
            "symbol": position["symbol"], "proposal_id": proposal_id,
            "fill_price": price, "added_quantity": added, "entry_stage": stage,
            "reason": ("higher low and renewed volume confirmed" if stage == 2
                       else "breakout and execution safety reconfirmed"),
        })

    def latest_marks(self) -> dict[str, dict[str, Any]]:
        marks: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if record.get("type") == "PAPER_MARK":
                marks[str(record.get("proposal_id") or "")] = record
        return marks

    def position_diagnostics(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or now_utc()
        marks = self.latest_marks()
        diagnostics = []
        for position in self.positions():
            recorded = str(position.get("recorded_at") or "")
            try:
                opened = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
                age_minutes = max(0.0, (current - opened.astimezone(UTC)).total_seconds() / 60)
            except (TypeError, ValueError):
                age_minutes = None
            proposal_id = str(position.get("proposal_id") or "")
            mark = marks.get(proposal_id, {})
            position_marks = [item for item in self.records()
                              if item.get("type") == "PAPER_MARK" and
                              str(item.get("proposal_id") or "") == proposal_id]
            pnls = [float(item.get("unrealized_pnl_usd") or 0) for item in position_marks]
            entry_price = float(position.get("fill_price") or 0)
            quantity = float(position.get("quantity") or 0)
            entry_value = entry_price * quantity
            mark_price = mark.get("mark_price")
            current_value = float(mark_price) * quantity if mark_price is not None else None
            current_pnl = mark.get("unrealized_pnl_usd")
            diagnostics.append({
                "proposal_id": proposal_id,
                "asset_class": position.get("asset_class"),
                "strategy": position.get("strategy"),
                "score": position.get("score"),
                "symbol": position.get("symbol"),
                "side": position.get("side"),
                "entry_price": position.get("fill_price"),
                "stop_price": position.get("stop_price"),
                "target_price": position.get("target_price"),
                "quantity": position.get("quantity"),
                "maximum_loss_usd": position.get("maximum_loss_usd"),
                "opened_at": recorded,
                "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
                "current_mark_price": mark.get("mark_price"),
                "current_unrealized_pnl_usd": mark.get("unrealized_pnl_usd"),
                "entry_value_usd": round(entry_value, 4),
                "current_value_usd": round(current_value, 4) if current_value is not None else None,
                "return_pct": round(float(current_pnl) / entry_value * 100, 3)
                              if current_pnl is not None and entry_value > 0 else None,
                "last_mark_at": mark.get("recorded_at"),
                "mfe_usd": round(max([0.0, *pnls]), 8),
                "mae_usd": round(min([0.0, *pnls]), 8),
                "research_only": position.get("research_only") is True,
                "research_cohort": position.get("research_cohort") or "",
                "qualification_failures": position.get("qualification_failures") or [],
                "blocks_new_symbol": True,
            })
        return diagnostics

    def close(self, proposal_id: str, price: float, reason: str,
              *, price_source: str = "CURRENT_EXECUTABLE_MARK") -> dict[str, Any]:
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
                            "quantity": quantity, "reason": reason, "realized_pnl_usd": round(pnl, 8),
                            "price_source": price_source})

    def partial_close(self, proposal_id: str, price: float, fraction: float, reason: str,
                      *, price_source: str = "CURRENT_EXECUTABLE_MARK",
                      profit_tier: str | None = None) -> dict[str, Any]:
        position = next((item for item in self.positions() if item.get("proposal_id") == proposal_id), None)
        if not position:
            raise MultiAssetRejected("paper position is not open")
        fraction = max(0.0, min(1.0, float(fraction)))
        if not 0 < fraction < 1:
            raise MultiAssetRejected("partial close fraction must be between zero and one")
        quantity = float(position["quantity"])
        closed_quantity = quantity * fraction
        pnl = (price - float(position["fill_price"])) * closed_quantity
        return self.append({
            "type": "PAPER_PARTIAL_CLOSE", "mode": "PAPER_ONLY",
            "asset_class": position["asset_class"], "strategy": position["strategy"],
            "symbol": position["symbol"], "proposal_id": proposal_id,
            "entry_price": position["fill_price"], "fill_price": price,
            "closed_quantity": closed_quantity, "remaining_quantity": quantity - closed_quantity,
            "fraction": fraction, "reason": reason, "realized_pnl_usd": round(pnl, 8),
            "profit_tier": profit_tier or ("10R" if "10R" in reason else
                                             "5R" if "5R" in reason else
                                             "2R" if "2R" in reason else "OTHER"),
            "price_source": price_source,
        })

    def report(self) -> dict[str, Any]:
        records = self.records()
        closes = [item for item in records if item.get("type") == "PAPER_CLOSE"]
        partials = [item for item in records if item.get("type") == "PAPER_PARTIAL_CLOSE"]
        pnls = [float(item.get("realized_pnl_usd") or 0) for item in closes]
        all_realized = [float(item.get("realized_pnl_usd") or 0) for item in [*closes, *partials]]
        daily_cutoff = now_utc().timestamp() - 86400
        daily_realized = []
        for item in [*closes, *partials]:
            try:
                recorded = datetime.fromisoformat(str(item.get("recorded_at") or "").replace("Z", "+00:00"))
                if recorded.timestamp() >= daily_cutoff:
                    daily_realized.append(float(item.get("realized_pnl_usd") or 0))
            except ValueError:
                continue
        by_strategy: dict[str, dict[str, Any]] = {}
        for item in records:
            if item.get("type") not in {"PAPER_FILL", "PAPER_ADD", "PAPER_CLOSE", "PAPER_PARTIAL_CLOSE"}:
                continue
            strategy = str(item.get("strategy") or "UNKNOWN")
            bucket = by_strategy.setdefault(strategy, {"opened": 0, "closed": 0, "wins": 0,
                                                        "losses": 0, "net_pnl_usd": 0.0})
            if item.get("type") == "PAPER_FILL":
                bucket["opened"] += 1
            elif item.get("type") == "PAPER_ADD":
                pass
            elif item.get("type") == "PAPER_CLOSE":
                pnl = float(item.get("realized_pnl_usd") or 0)
                bucket["closed"] += 1
                bucket["wins" if pnl > 0 else "losses"] += 1
                bucket["net_pnl_usd"] = round(bucket["net_pnl_usd"] + pnl, 8)
            elif item.get("type") == "PAPER_PARTIAL_CLOSE":
                bucket["net_pnl_usd"] = round(
                    bucket["net_pnl_usd"] + float(item.get("realized_pnl_usd") or 0), 8)
        return {
            "paper_only": True,
            "open_positions": self.position_diagnostics(),
            "closed": len(closes),
            "wins": sum(value > 0 for value in pnls),
            "losses": sum(value <= 0 for value in pnls),
            "realized_pnl_usd": round(sum(all_realized), 8),
            "daily_realized_pnl_usd": round(sum(daily_realized), 8),
            "partial_profit_actions": len(partials),
            "by_strategy": by_strategy,
            "recent_closes": list(reversed(closes[-25:])),
        }


class MultiAssetEngine:
    def __init__(self, ledger: PaperLedger, policy: AssetPolicy | None = None) -> None:
        self.policy = policy or AssetPolicy.from_env()
        self.ledger = ledger
        self.engines = {
            "FOREX": ForexEngine(self.policy),
            "EQUITY": EquityEngine(self.policy),
            "OPTION": DefinedRiskOptionsEngine(self.policy),
            "CRYPTO": MultiWeekCryptoEngine(self.policy),
        }

    def enabled(self, asset_class: str) -> bool:
        return _truthy(f"{asset_class}_ENGINE_ENABLED", False)

    def process(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        asset_class = str(snapshot.get("asset_class", "")).upper()
        if asset_class not in SUPPORTED_ASSET_CLASSES:
            raise MultiAssetRejected("unsupported asset class")
        if not self.enabled(asset_class):
            raise MultiAssetRejected(f"{asset_class} engine is disabled")
        proposal = self.engines[asset_class].evaluate(snapshot)
        if self.ledger.has_open_symbol(asset_class, str(snapshot.get("symbol") or "")):
            raise MultiAssetRejected(
                f"qualified signal blocked: {snapshot.get('symbol')} already has an open paper position")
        open_count = self.ledger.open_positions(asset_class)
        capacity = self.policy.max_open_positions_per_sleeve
        if asset_class == "CRYPTO" and proposal.research_only:
            capacity = max(1, int(os.getenv("MULTI_WEEK_RESEARCH_MAX_OPEN_POSITIONS", "3")))
            budget = max(100.0, float(os.getenv("MULTI_WEEK_PAPER_BUDGET_USD", "1000")))
            exposure_cap = budget * min(.50, max(.05, float(os.getenv(
                "MULTI_WEEK_MAX_EXPOSURE_FRACTION", ".15"))))
            open_exposure = sum(float(item.get("entry_value_usd") or 0)
                                for item in self.ledger.position_diagnostics()
                                if item.get("strategy") == MULTI_WEEK_CRYPTO_STRATEGY)
            proposed_exposure = proposal.reference_price * proposal.quantity
            if open_exposure + proposed_exposure > exposure_cap + .01:
                raise MultiAssetRejected(
                    f"research budget cap: ${open_exposure:.2f} open + ${proposed_exposure:.2f} proposed "
                    f"exceeds ${exposure_cap:.2f}")
        if open_count >= capacity:
            blockers = ", ".join(str(item.get("symbol") or "UNKNOWN")
                                 for item in self.ledger.position_diagnostics()
                                 if str(item.get("asset_class") or "").upper() == asset_class)
            raise MultiAssetRejected(
                f"qualified signal blocked: paper sleeve capacity {open_count}/"
                f"{capacity}; open positions: {blockers or 'UNKNOWN'}")
        frozen = asdict(proposal)
        self.ledger.append({"type": "PROPOSAL", **frozen, "mode": "PAPER_ONLY"})
        return self.ledger.append({"type": "PAPER_FILL", **frozen, "mode": "PAPER_ONLY", "fill_price": proposal.reference_price})
