from __future__ import annotations


def supervision_levels(ticket: dict, *, entry: float, mark: float, high_water: float, regime: str = "") -> dict:
    high = max(entry, mark, high_water)
    activation = entry * (1 + float(ticket.get("trail_activation_pct", 12)) / 100)
    trail_active = high >= activation
    hard_stop = float(ticket["stop_price"])
    effective_stop = max(hard_stop, high * (1 - float(ticket.get("trail_pct", 8)) / 100)) if trail_active else hard_stop
    reason = "FALLING_REGIME" if regime.upper() == "FALLING" else ("TRAILING_STOP" if trail_active and mark <= effective_stop else "HARD_STOP_FALLBACK" if mark <= hard_stop else "")
    return {"high_water_price": high, "trail_active": trail_active, "effective_stop_price": effective_stop, "exit_reason": reason}
