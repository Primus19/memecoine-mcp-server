from __future__ import annotations

import os


def supervision_levels(ticket: dict, *, entry: float, mark: float, high_water: float, regime: str = "",
                       momentum_1h_pct: float | None = None, falling_observations: int = 0) -> dict:
    high = max(entry, mark, high_water)
    policy = ticket.get("opportunity_policy") or {}
    taker = float(policy.get("estimated_fee_bps_per_side", 120))
    spread_bps = float(ticket.get("spread_bps") or 0)
    slippage_bps = float(ticket.get("slippage_bps") or 0)
    edge_bps = float((ticket.get("opportunity_policy") or {}).get("minimum_net_edge_bps", 50))
    # `entry` is the broker-reconciled cost basis (buy value plus entry fee),
    # so only expected exit costs remain to be covered.  Dividing by one minus
    # the exit-cost rate is exact; simply adding round-trip bps double-counts
    # the already-paid entry costs and labels a profit-lock as break-even.
    exit_cost_rate = (taker + spread_bps / 2 + slippage_bps) / 10_000
    cost_covered_price = entry / max(1e-9, 1 - exit_cost_rate)
    break_even_activation_pct = max(2.0, (cost_covered_price / entry - 1) * 100 + edge_bps / 100)
    break_even_active = high >= entry * (1 + break_even_activation_pct / 100)
    # A stop placed exactly at the reconciled entry cost is not break-even once
    # the exit fee and market-order execution costs are paid. Default to the
    # cost-covered price; the flag restores the legacy entry-price stop only for
    # controlled comparisons.
    cost_aware = os.getenv("LIFECYCLE_BREAK_EVEN_INCLUDES_COSTS", "true").strip().lower() in {"1", "true", "yes", "on"}
    break_even_price = cost_covered_price if cost_aware else entry
    activation = entry * (1 + float(ticket.get("trail_activation_pct", 5)) / 100)
    trail_active = high >= activation
    hard_stop = float(ticket["stop_price"])
    initial_risk = max(0.0, entry - hard_stop)
    mfe_r = (high - entry) / initial_risk if initial_risk else 0.0
    gain_pct = (high / entry - 1) * 100 if entry else 0
    trail_pct = 2.5 if gain_pct >= 8 else float(ticket.get("trail_pct", 4))
    candidate_stop = high * (1 - trail_pct / 100) if trail_active else break_even_price if break_even_active else hard_stop
    # Risk-unit ratchet protects a position before a percentage trail can
    # activate. It is deliberately gradual to avoid turning ordinary noise
    # into premature exits: at 0.5R cap loss near -0.1R; at 1R lock 0.25R;
    # at 1.5R lock 0.75R.
    ratchet_r = .75 if mfe_r >= 1.5 else .25 if mfe_r >= 1.0 else -.10 if mfe_r >= .5 else None
    ratchet_stop = entry + ratchet_r * initial_risk if ratchet_r is not None else hard_stop
    effective_stop = max(hard_stop, candidate_stop, ratchet_stop)
    deteriorating = momentum_1h_pct is not None and momentum_1h_pct <= -1.0 and mark < high * .98
    falling_confirmed = regime.upper() == "FALLING" and falling_observations >= 3
    falling_deteriorating = falling_confirmed and mark <= entry and mark < high * .98
    # Position-level stops take precedence. A broad market label alone must not
    # liquidate a healthy position on one noisy observation.
    reason = ("HARD_STOP_FALLBACK" if mark <= hard_stop else
              "PROFIT_RISK_RATCHET" if mark <= effective_stop and ratchet_r is not None and ratchet_stop >= candidate_stop else
              "TRAILING_STOP" if mark <= effective_stop and (trail_active or break_even_active) else
              "POSITION_MOMENTUM_REVERSAL" if deteriorating else
              "FALLING_REGIME_CONFIRMED" if falling_deteriorating else "")
    return {"high_water_price": high, "break_even_active":break_even_active,"break_even_activation_pct":break_even_activation_pct,"break_even_price":break_even_price,"break_even_includes_costs":cost_aware,"trail_active": trail_active,"trail_pct":trail_pct, "effective_stop_price": effective_stop, "risk_ratchet_active": ratchet_r is not None, "maximum_favorable_r": mfe_r, "risk_ratchet_floor_r": ratchet_r, "position_deteriorating":deteriorating,"falling_observations":falling_observations,"falling_regime_confirmed":falling_confirmed,"exit_reason": reason}


def profit_protection_challenger(ticket: dict, *, entry: float, mark: float, high_water: float) -> dict:
    """Evaluate tighter protection in shadow mode without changing live exits."""
    high = max(entry, mark, high_water)
    activation_pct = float(ticket.get("challenger_trail_activation_pct", 5))
    trail_pct = float(ticket.get("challenger_trail_pct", 4))
    break_even_pct = float(ticket.get("challenger_break_even_activation_pct", 3))
    break_even_active = high >= entry * (1 + break_even_pct / 100)
    trail_active = high >= entry * (1 + activation_pct / 100)
    candidate_stop = high * (1 - trail_pct / 100) if trail_active else entry if break_even_active else float(ticket["stop_price"])
    effective_stop = max(float(ticket["stop_price"]), candidate_stop)
    return {
        "name": "PROFIT_PROTECTION_5_4", "shadow_only": True,
        "trail_activation_pct": activation_pct, "trail_pct": trail_pct,
        "break_even_activation_pct": break_even_pct,
        "break_even_active": break_even_active, "trail_active": trail_active,
        "effective_stop_price": effective_stop, "would_exit": mark <= effective_stop,
    }
