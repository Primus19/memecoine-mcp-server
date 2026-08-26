from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from .policy import OpportunityPolicy

UTC = timezone.utc


class TicketRejected(ValueError):
    pass


def risk_size_ticket(t: dict[str, Any], *, permitted_capital: float,
                     available_usdc: float, allocation_fraction: float = .95) -> dict[str, Any]:
    """Return an executor-sized ticket whose stop loss risks at most 2% capital."""
    result = dict(t)
    entry = float(result.get("limit_price") or 0)
    stop = float(result.get("stop_price") or 0)
    if not 0 < stop < entry:
        return result
    risk_fraction = min(.05, max(.005, float(os.getenv("MEME_MAX_RISK_FRACTION", ".02"))))
    risk_budget = min(float(os.getenv("MEME_MAX_RISK_USDC", ".50")), permitted_capital * risk_fraction)
    stop_fraction = (entry - stop) / entry
    capital_limit = min(available_usdc, permitted_capital) * allocation_fraction
    proposed = min(float(result.get("notional_usdc") or 0), capital_limit, risk_budget / stop_fraction)
    result["notional_usdc"] = round(proposed, 8)
    result["max_loss_usdc"] = round(proposed * stop_fraction, 8)
    result["risk_budget_usdc"] = round(risk_budget, 8)
    result["risk_sizing"] = "STOP_DISTANCE_2PCT_CAPITAL"
    return result


def validate_ticket(
    t: dict[str, Any],
    *,
    available_usdc: float,
    permitted_capital: float,
    open_positions: int,
    product: dict[str, Any],
    allocation_fraction: float = 0.95,
) -> None:
    """Validate a research ticket against live Coinbase and portfolio facts.

    The research model proposes a product, but the server independently verifies
    that Coinbase currently exposes it as a tradable USDC spot market.
    """
    errors: list[str] = []
    policy = OpportunityPolicy.from_env()
    tier = str(t.get("opportunity_tier") or "ESTABLISHED").upper()
    actual_tier = policy.tier(t.get("market_cap_usd"), t.get("volume_24h_usd"))
    product_id = str(t.get("product_id", "")).upper()
    if open_positions:
        errors.append("one-position limit reached")
    if product_id != str(product.get("product_id", "")).upper():
        errors.append("product identity mismatch")
    if not product_id.endswith("-USDC"):
        errors.append("only USDC-quoted spot products are eligible")
    if product.get("product_type") not in (None, "SPOT"):
        errors.append("product is not spot")
    if product.get("trading_disabled") or product.get("view_only"):
        errors.append("product is not currently tradable")
    if not policy.regime_allowed(t.get("regime")):
        errors.append("regime is not permitted")
    required_score = policy.minimum_score_for(tier)
    if actual_tier != tier:
        errors.append("opportunity tier does not match market liquidity")
    if float(t.get("score", 0)) < required_score:
        errors.append(f"score below {required_score:g}")
    if not policy.news_allowed(t.get("news_score", 0), news_veto=t.get("news_veto") is True):
        errors.append("news policy gate failed")
    if min(float(t.get("change_1h_pct", 0)), float(t.get("change_24h_pct", 0))) <= 0:
        errors.append("1h/24h momentum gate failed")
    if float(t.get("change_24h_pct", 99)) > policy.max_momentum_24h_pct:
        errors.append(f"24h move exceeds {policy.max_momentum_24h_pct:g}%")
    if actual_tier == "INELIGIBLE":
        errors.append("market cap or volume below all policy tiers")
    if not policy.min_turnover <= float(t.get("turnover", -1)) <= policy.max_turnover:
        errors.append("turnover outside policy range")
    spread_limit = policy.emerging_max_spread_bps if tier == "EMERGING" else 50
    slippage_limit = policy.emerging_max_slippage_bps if tier == "EMERGING" else 50
    if float(t.get("spread_bps", 9999)) > spread_limit:
        errors.append(f"spread above {spread_limit:g} bps")
    if float(t.get("slippage_bps", 9999)) > slippage_limit:
        errors.append(f"slippage above {slippage_limit:g} bps")
    entry = float(t.get("limit_price", 0) or 0)
    target = float(t.get("target_price", 0) or 0)
    expected_gross_bps = ((target / entry) - 1) * 10_000 if entry > 0 and target > entry else 0
    expected_round_trip_cost_bps = (2 * policy.estimated_fee_bps_per_side
                                    + float(t.get("spread_bps", 9999))
                                    + 2 * float(t.get("slippage_bps", 9999)))
    if expected_gross_bps < expected_round_trip_cost_bps + policy.minimum_net_edge_bps:
        errors.append("target does not clear estimated round-trip costs and minimum net edge")
    if not all(t.get(k) is True for k in ("identity_verified", "spot_available", "no_safety_veto")):
        errors.append("identity/spot/safety verification failed")

    maximum_notional = max(0.0, min(available_usdc, permitted_capital) * allocation_fraction)
    notional = float(t.get("notional_usdc", 9999))
    if not 5 <= notional <= maximum_notional + 1e-9:
        errors.append(f"notional outside available $5-${maximum_notional:.2f} envelope")
    if tier == "EMERGING" and notional > 5.0:
        errors.append("emerging-tier notional exceeds $5")

    maximum_loss = min(float(os.getenv("MEME_MAX_RISK_USDC", ".50")),
                       permitted_capital * min(.05, max(.005, float(os.getenv("MEME_MAX_RISK_FRACTION", ".02")))))
    if float(t.get("max_loss_usdc", 9999)) > maximum_loss:
        errors.append(f"loss exceeds risk-sized ${maximum_loss:.2f} cap")
    if tier == "EMERGING" and float(t.get("max_loss_usdc", 9999)) > .25:
        errors.append("emerging-tier loss exceeds $0.25")
    try:
        expiry = datetime.fromisoformat(str(t["expires_at"]).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if not now < expiry <= now + timedelta(seconds=120):
            errors.append("ticket expired or lasts over two minutes")
    except Exception:
        errors.append("invalid expiry")
    entry, stop, target = (float(t.get(k, 0)) for k in ("limit_price", "stop_price", "target_price"))
    if not 0 < stop < entry < target:
        errors.append("prices must satisfy stop < entry < target")
    if entry and notional * (entry - stop) / entry > maximum_loss:
        errors.append("stop risk exceeds cap")
    try:
        source_time = datetime.fromisoformat(str(t["source_timestamp"]).replace("Z", "+00:00"))
        age = (datetime.now(UTC) - source_time).total_seconds()
        if age < -5 or age > 120:
            errors.append("source market data is not fresh")
    except Exception:
        errors.append("invalid source timestamp")
    if errors:
        raise TicketRejected("; ".join(errors))
