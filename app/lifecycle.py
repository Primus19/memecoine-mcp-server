from __future__ import annotations


def supervision_levels(ticket: dict, *, entry: float, mark: float, high_water: float, regime: str = "") -> dict:
    high = max(entry, mark, high_water)
    activation = entry * (1 + float(ticket.get("trail_activation_pct", 12)) / 100)
    trail_active = high >= activation
    hard_stop = float(ticket["stop_price"])
    effective_stop = max(hard_stop, high * (1 - float(ticket.get("trail_pct", 8)) / 100)) if trail_active else hard_stop
    reason = "FALLING_REGIME" if regime.upper() == "FALLING" else ("TRAILING_STOP" if trail_active and mark <= effective_stop else "HARD_STOP_FALLBACK" if mark <= hard_stop else "")
    return {"high_water_price": high, "trail_active": trail_active, "effective_stop_price": effective_stop, "exit_reason": reason}


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
