from __future__ import annotations

"""Chain-neutral, paper-only research model for multi-week crypto trends.

This module deliberately does not share admission logic with the launch/runner
strategies.  A token that is attractive over twenty-one days should not be
selected because of a single five-minute candle.
"""

from dataclasses import dataclass
import math
from typing import Any


STRATEGY = "MULTI_WEEK_CRYPTO_MOMENTUM_V1"
RESEARCH_COHORT = "EMERGING_FORWARD_PAPER_HOLD"
LIQUID_TREND_COHORT = "LIQUID_TSMOM_FORWARD_PAPER"
CHECKPOINT_DAYS = (3, 7, 14, 21, 30, 60, 90)

@dataclass(frozen=True)
class MultiWeekPolicy:
    minimum_age_days: float = 7.0
    minimum_liquidity_usd: float = 250_000.0
    minimum_volume_24h_usd: float = 500_000.0
    minimum_research_market_cap_usd: float = 100_000.0
    maximum_research_market_cap_usd: float = 25_000_000.0
    minimum_research_liquidity_to_cap: float = 0.03
    minimum_research_volume_to_cap: float = 0.05
    minimum_round_trip_recovery: float = 0.97
    maximum_sell_impact_bps: float = 150.0
    maximum_top10_fraction: float = 0.65
    maximum_creator_fraction: float = 0.15
    minimum_confirmations: int = 2
    minimum_confirmation_span_hours: float = 12.0
    minimum_score: float = 75.0
    maximum_entry_extension_from_20d: float = 0.20
    initial_risk_fraction: float = 0.0025
    maximum_portfolio_risk_fraction: float = 0.01
    minimum_slow_history_days: int = 200
    minimum_slow_positive_windows: int = 2
    minimum_slow_trend_agreement: float = 0.67


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_candidate(candidate: dict[str, Any], policy: MultiWeekPolicy | None = None) -> dict[str, Any]:
    """Return an auditable paper decision; missing evidence always fails safely."""
    policy = policy or MultiWeekPolicy()
    failures: list[str] = []
    warnings: list[str] = []

    required_identity = ("chain", "contract", "symbol", "observed_at")
    for field in required_identity:
        if not str(candidate.get(field) or "").strip():
            failures.append(f"missing {field}")

    age = _number(candidate.get("token_age_days"), -1)
    liquidity = _number(candidate.get("liquidity_usd"))
    volume = _number(candidate.get("volume_24h_usd"))
    market_cap = _number(candidate.get("market_cap_usd"))
    recovery = _number(candidate.get("round_trip_recovery"), -1)
    impact = _number(candidate.get("sell_impact_bps"), 10_000)
    top10 = candidate.get("top10_holder_fraction")
    creator = candidate.get("creator_fraction")
    confirmations = int(_number(candidate.get("confirmation_count")))
    confirmation_span = _number(candidate.get("confirmation_span_hours"))
    confirmation_return = candidate.get("confirmation_return_pct")
    confirmation_drawdown = candidate.get("confirmation_drawdown_pct")

    cex_mode = candidate.get("execution_evidence_mode") == "CEX_ORDER_BOOK"
    slow_returns = [candidate.get(f"return_{days}d_pct") for days in (90, 120, 180, 270)]
    available_slow_returns = [_number(value) for value in slow_returns if value is not None]
    positive_slow_returns = [value for value in available_slow_returns if value > 0]
    slow_agreement = (len(positive_slow_returns) / len(available_slow_returns)
                      if available_slow_returns else 0.0)
    hard_checks = (
        (candidate.get("sell_route_ok") is not True, "full-position sell route unavailable"),
        (not cex_mode and candidate.get("security_verified") is not True, "contract safety not verified"),
        (cex_mode and candidate.get("venue_operational") is not True, "execution venue unavailable"),
        (age < policy.minimum_age_days, "token is too new for a multi-week model"),
        (liquidity < policy.minimum_liquidity_usd, "liquidity below multi-week floor"),
        (volume < policy.minimum_volume_24h_usd, "volume below multi-week floor"),
        (recovery < policy.minimum_round_trip_recovery, "round-trip recovery below minimum"),
        (impact > policy.maximum_sell_impact_bps, "sell impact above maximum"),
        (not cex_mode and top10 is None, "top-10 concentration unavailable"),
        (not cex_mode and top10 is not None and _number(top10) > policy.maximum_top10_fraction, "top-10 concentration too high"),
        (not cex_mode and creator is None, "creator concentration unavailable"),
        (not cex_mode and creator is not None and _number(creator) > policy.maximum_creator_fraction, "creator concentration too high"),
    )
    failures.extend(message for failed, message in hard_checks if failed)
    if cex_mode:
        slow_checks = (
            (int(_number(candidate.get("daily_candle_count"))) < policy.minimum_slow_history_days,
             "fewer than 200 completed daily candles"),
            (candidate.get("price_above_200d_average") is not True,
             "price is not above the 200-day average"),
            (len(positive_slow_returns) < policy.minimum_slow_positive_windows,
             "fewer than two positive slow-momentum windows"),
            (slow_agreement < policy.minimum_slow_trend_agreement,
             "slow-momentum horizon agreement below 67%"),
        )
        failures.extend(message for failed, message in slow_checks if failed)

    trend_flags = {
        "above_20d_trend": candidate.get("price_above_20d_average") is True,
        "higher_highs": candidate.get("daily_higher_highs") is True,
        "higher_lows": candidate.get("daily_higher_lows") is True,
        "positive_weekly_strength": _number(candidate.get("relative_strength_7d_pct")) > 0,
        "persistent_volume": _number(candidate.get("volume_7d_vs_prior_ratio")) >= 1.10,
        "holder_growth": _number(candidate.get("holder_growth_7d_pct")) > 0,
        "venue_liquidity_persistence": cex_mode and candidate.get("venue_operational") is True,
    }
    if cex_mode:
        trend_points = min(44.0, len(positive_slow_returns) * 10.0 +
                           (12.0 if candidate.get("price_above_200d_average") is True else 0.0))
    else:
        trend_points = sum(trend_flags.values()) * 8.0
    execution_points = 0.0
    if recovery >= 0.99:
        execution_points += 12
    elif recovery >= policy.minimum_round_trip_recovery:
        execution_points += 7
    if impact <= 75:
        execution_points += 10
    elif impact <= policy.maximum_sell_impact_bps:
        execution_points += 5
    if liquidity >= 1_000_000:
        execution_points += 8
    elif liquidity >= policy.minimum_liquidity_usd:
        execution_points += 4

    confirmation_points = 12.0 if (
        confirmations >= policy.minimum_confirmations and
        confirmation_span >= policy.minimum_confirmation_span_hours
    ) else 0.0
    if not confirmation_points:
        failures.append("requires two confirmations at least 12 hours apart")

    extension = _number(candidate.get("extension_from_20d_fraction"), 1.0)
    entry_quality_points = 10.0 if (
        candidate.get("controlled_pullback_or_consolidation") is True and
        extension <= policy.maximum_entry_extension_from_20d
    ) else 0.0
    if not entry_quality_points:
        failures.append("entry is extended or lacks a controlled consolidation")

    score = min(100.0, trend_points + execution_points + confirmation_points + entry_quality_points)
    if not cex_mode and sum(trend_flags.values()) < 4:
        failures.append("multi-week trend persistence is incomplete")
    if score < policy.minimum_score:
        failures.append(f"score {score:.1f} below {policy.minimum_score:.1f}")

    qualified = not failures
    # A research paper hold is deliberately less restrictive than a qualified
    # strategy entry.  It creates prospective evidence for emerging assets
    # without pretending missing ownership/safety/trend history has passed.
    # Execution quality remains immutable even in this research cohort.
    research_failures = []
    chain = str(candidate.get("chain") or "").strip().lower()
    research_checks = (
        (chain == "coinbase-spot", "established CEX asset is outside emerging research cohort"),
        (any(not str(candidate.get(field) or "").strip() for field in required_identity),
         "identity incomplete"),
        (candidate.get("sell_route_ok") is not True, "full-position sell route unavailable"),
        (age < policy.minimum_age_days, "token is too new for research hold"),
        (liquidity < policy.minimum_liquidity_usd, "liquidity below research floor"),
        (volume < policy.minimum_volume_24h_usd, "volume below research floor"),
        (market_cap < policy.minimum_research_market_cap_usd, "market cap below research floor or unavailable"),
        (market_cap > policy.maximum_research_market_cap_usd, "market cap too large for emerging upside cohort"),
        (market_cap > 0 and liquidity / market_cap < policy.minimum_research_liquidity_to_cap,
         "liquidity is too shallow relative to market cap"),
        (market_cap > 0 and volume / market_cap < policy.minimum_research_volume_to_cap,
         "volume is too weak relative to market cap"),
        (recovery < policy.minimum_round_trip_recovery, "round-trip recovery below minimum"),
        (impact > policy.maximum_sell_impact_bps, "sell impact above maximum"),
        (confirmations < policy.minimum_confirmations or
         confirmation_span < policy.minimum_confirmation_span_hours,
         "research hold requires two independent observations at least 12 hours apart"),
        (confirmation_return is None, "research persistence return unavailable"),
        (confirmation_return is not None and _number(confirmation_return) < -5,
         "candidate deteriorated more than 5% during confirmation"),
        (confirmation_return is not None and _number(confirmation_return) > 50,
         "candidate rose more than 50% during confirmation; late-chase risk"),
        (confirmation_drawdown is not None and _number(confirmation_drawdown) < -20,
         "candidate confirmation path drawdown exceeds 20%"),
    )
    research_failures.extend(message for failed, message in research_checks if failed)
    research_eligible = not research_failures
    size_headroom = 0.0 if market_cap <= 0 else max(0.0, min(
        1.0, 1.0 - ((market_cap - policy.minimum_research_market_cap_usd) /
                    (policy.maximum_research_market_cap_usd - policy.minimum_research_market_cap_usd))))
    persistence_score = 0.0
    if confirmations >= policy.minimum_confirmations and confirmation_span >= policy.minimum_confirmation_span_hours:
        persistence_score += 10
    if confirmation_return is not None and 0 <= _number(confirmation_return) <= 30:
        persistence_score += 10
    research_score = min(100.0,
        35.0 * max(0.0, min(1.0, (recovery - policy.minimum_round_trip_recovery) / .03)) +
        15.0 * max(0.0, min(1.0, liquidity / 1_000_000.0)) +
        15.0 * max(0.0, min(1.0, volume / 2_000_000.0)) +
        15.0 * size_headroom + persistence_score)
    if candidate.get("catalyst_verified") is not True:
        warnings.append("no verified catalyst; trend evidence must stand on its own")

    return {
        "strategy": STRATEGY,
        "strategy_version": "V1",
        "mode": "PAPER_ONLY",
        "qualified": qualified,
        "research_eligible": research_eligible,
        "research_failures": research_failures,
        "research_score": round(research_score, 2),
        "decision": ("PAPER_ALLOCATION" if qualified and cex_mode else
                     "PAPER_STAGE_1" if qualified else
                     "RESEARCH_PAPER_HOLD" if research_eligible else "FORWARD_TRACK"),
        "cohort": (LIQUID_TREND_COHORT if qualified and cex_mode else
                   "QUALIFIED" if qualified else
                   RESEARCH_COHORT if research_eligible else "TRACK_ONLY"),
        "score": round(score, 2),
        "hard_gate_failures": failures,
        "warnings": warnings,
        "trend_evidence": trend_flags,
        "slow_trend_evidence": {
            "daily_candle_count": int(_number(candidate.get("daily_candle_count"))),
            "price_above_200d_average": candidate.get("price_above_200d_average") is True,
            "positive_windows": len(positive_slow_returns),
            "available_windows": len(available_slow_returns),
            "agreement": round(slow_agreement, 4),
            "returns_pct": {str(days): candidate.get(f"return_{days}d_pct")
                            for days in (90, 120, 180, 270)},
        },
        "checkpoint_days": list(CHECKPOINT_DAYS),
        "entry_plan": {
            "stage_1_fraction": 0.25,
            "stage_2_fraction": 0.25,
            "stage_3_fraction": 0.50,
            "stage_2_requires": "higher low plus renewed volume",
            "stage_3_requires": "confirmed breakout plus unchanged execution safety",
        },
        "promotion_gate": "100 independent cost-stressed closes with positive expectancy",
    }


def manage_position(position: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    """Prospective profit management based only on information available now."""
    policy = MultiWeekPolicy()
    entry = _number(position.get("entry_price") or position.get("fill_price"))
    stop = _number(position.get("initial_stop_price") or position.get("stop_price"))
    price = _number(market.get("executable_price"))
    if entry <= 0 or stop <= 0 or price <= 0:
        return {"action": "HOLD", "fraction": 0.0, "profit_tier": "",
                "reason": "invalid entry, stop, or executable price; reconciliation required",
                "monitoring_error": True}
    peak = max(_number(position.get("peak_executable_price"), entry), price)
    risk = max(entry - stop, entry * 0.001)
    r_multiple = (price - entry) / risk
    peak_r = (peak - entry) / risk
    giveback = (peak - price) / max(peak - entry, risk)

    research_only = position.get("research_only") is True
    liquid_trend = position.get("research_cohort") == LIQUID_TREND_COHORT
    market_cap = _number(market.get("market_cap_usd"))
    liquidity = _number(market.get("liquidity_usd"))
    volume = _number(market.get("volume_24h_usd"))
    research_asymmetry_failed = research_only and (
        market_cap < policy.minimum_research_market_cap_usd or
        market_cap > policy.maximum_research_market_cap_usd or
        liquidity < policy.minimum_liquidity_usd or
        volume < policy.minimum_volume_24h_usd or
        (market_cap > 0 and liquidity / market_cap < policy.minimum_research_liquidity_to_cap) or
        (market_cap > 0 and volume / market_cap < policy.minimum_research_volume_to_cap)
    )
    hard_exit = (
        research_asymmetry_failed or
        market.get("sell_route_ok") is not True or
        _number(market.get("round_trip_recovery"), -1) < 0.95 or
        (not research_only and market.get("security_verified") is not True and not (
            market.get("execution_evidence_mode") == "CEX_ORDER_BOOK" and
            market.get("venue_operational") is True))
    )
    if liquid_trend:
        slow_values = [market.get(f"return_{days}d_pct") for days in (90, 120, 180, 270)]
        slow_values = [_number(value) for value in slow_values if value is not None]
        trend_break = (market.get("price_above_200d_average") is not True and
                       sum(value > 0 for value in slow_values) < 2)
    else:
        trend_break = (not research_only or int(_number(market.get("daily_candle_count"))) >= 20) and sum((
            market.get("price_above_20d_average") is not True,
            market.get("daily_higher_lows") is not True,
            _number(market.get("relative_strength_7d_pct")) <= 0,
            _number(market.get("volume_7d_vs_prior_ratio")) < 0.8,
        )) >= 3

    profit_tier = ""
    if hard_exit:
        action, fraction, reason = "EXIT", 1.0, "execution, safety, or asymmetry gate failed"
    elif trend_break:
        action, fraction, reason = "EXIT", 1.0, "multi-factor trend deterioration"
    # Risk exits dominate historical profit observations. A delayed monitor
    # must never call a below-stop quote a profit-taking opportunity merely
    # because the position touched a tier earlier.
    elif price <= stop:
        action, fraction, reason = "EXIT", 1.0, "initial risk stop reached"
    elif peak_r >= 5 and giveback >= 0.35:
        action, fraction, reason = "EXIT", 1.0, "runner surrendered 35% of favorable excursion"
    elif peak_r >= 1 and r_multiple <= 0.10:
        action, fraction, reason = "EXIT", 1.0, "1R winner returned to cost-aware breakeven"
    # Partial tiers require the current executable mark to remain at the tier;
    # an uncaptured historical peak is missed profit, not an executable fill.
    elif r_multiple >= 10 and not position.get("took_10r_profit"):
        action, fraction, reason, profit_tier = (
            "TAKE_PROFIT", 0.20, "10R moonshot profit tier", "10R")
    elif r_multiple >= 5 and not position.get("took_5r_profit"):
        action, fraction, reason, profit_tier = (
            "TAKE_PROFIT", 0.20, "5R runner profit tier", "5R")
    elif r_multiple >= 2 and not position.get("took_2r_profit"):
        action, fraction, reason, profit_tier = (
            "TAKE_PROFIT", 0.20, "first 2R profit tier", "2R")
    else:
        action, fraction, reason = "HOLD", 0.0, "trend and executable liquidity remain intact"

    return {
        "action": action,
        "fraction": fraction,
        "reason": reason,
        "profit_tier": profit_tier,
        "r_multiple": round(r_multiple, 4),
        "peak_r_multiple": round(peak_r, 4),
        "profit_giveback_fraction": round(max(0.0, giveback), 4),
        "executable_price": price,
        "research_only": research_only,
    }
