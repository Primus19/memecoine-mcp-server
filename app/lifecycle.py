from __future__ import annotations


def supervision_levels(ticket: dict, *, entry: float, mark: float, high_water: float, regime: str = "",
                       momentum_1h_pct: float | None = None) -> dict:
    high = max(entry, mark, high_water)
    fee_bps = 2 * float((ticket.get("opportunity_policy") or {}).get("estimated_fee_bps_per_side", 120))
    execution_bps = float(ticket.get("spread_bps") or 0) + 2 * float(ticket.get("slippage_bps") or 0)
    edge_bps = float((ticket.get("opportunity_policy") or {}).get("minimum_net_edge_bps", 50))
    break_even_activation_pct = max(2.0, (fee_bps + execution_bps + edge_bps) / 100)
    break_even_active = high >= entry * (1 + break_even_activation_pct / 100)
    activation = entry * (1 + float(ticket.get("trail_activation_pct", 5)) / 100)
    trail_active = high >= activation
    hard_stop = float(ticket["stop_price"])
    gain_pct = (high / entry - 1) * 100 if entry else 0
    trail_pct = 2.5 if gain_pct >= 8 else float(ticket.get("trail_pct", 4))
    candidate_stop = high * (1 - trail_pct / 100) if trail_active else entry if break_even_active else hard_stop
    effective_stop = max(hard_stop, candidate_stop)
    deteriorating = momentum_1h_pct is not None and momentum_1h_pct <= -1.0 and mark < high * .98
    reason = "FALLING_REGIME" if regime.upper() == "FALLING" else ("POSITION_MOMENTUM_REVERSAL" if deteriorating else "TRAILING_STOP" if mark <= effective_stop and (trail_active or break_even_active) else "HARD_STOP_FALLBACK" if mark <= hard_stop else "")
    return {"high_water_price": high, "break_even_active":break_even_active,"break_even_activation_pct":break_even_activation_pct,"trail_active": trail_active,"trail_pct":trail_pct, "effective_stop_price": effective_stop, "position_deteriorating":deteriorating,"exit_reason": reason}


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
