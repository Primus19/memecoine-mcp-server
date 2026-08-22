from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc


class TicketRejected(ValueError):
    pass


def validate_ticket(t: dict[str, Any], equity: float, open_positions: int, allowlist: set[str]) -> None:
    errors: list[str] = []
    product = str(t.get("product_id", "")).upper()
    if open_positions: errors.append("one-position limit reached")
    if product not in allowlist: errors.append("product is not allowlisted")
    if t.get("regime") != "RISING": errors.append("regime is not RISING")
    if float(t.get("score", 0)) < 85: errors.append("score below 85")
    if float(t.get("news_score", 0)) < 4: errors.append("verified news score below 4")
    if min(float(t.get("change_1h_pct", 0)), float(t.get("change_24h_pct", 0))) <= 0: errors.append("1h/24h momentum gate failed")
    if float(t.get("change_24h_pct", 99)) > 15: errors.append("24h move exceeds 15%")
    if float(t.get("market_cap_usd", 0)) < 50_000_000: errors.append("market cap below $50M")
    if float(t.get("volume_24h_usd", 0)) < 10_000_000: errors.append("volume below $10M")
    if not 0.05 <= float(t.get("turnover", -1)) <= 1.0: errors.append("turnover outside 5%-100%")
    if float(t.get("spread_bps", 9999)) > 50: errors.append("spread above 50 bps")
    if float(t.get("slippage_bps", 9999)) > 50: errors.append("slippage above 50 bps")
    if not all(t.get(k) is True for k in ("identity_verified", "spot_available", "no_safety_veto")): errors.append("identity/spot/safety verification failed")
    notional = float(t.get("notional_usdc", 9999))
    if not 5 <= notional <= min(25.0, equity): errors.append("notional outside available $5-$25 envelope")
    if float(t.get("max_loss_usdc", 9999)) > min(2.50, equity * .10): errors.append("loss exceeds 10% equity/$2.50 cap")
    try:
        expiry = datetime.fromisoformat(str(t["expires_at"]).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if not now < expiry <= now + timedelta(hours=1): errors.append("ticket expired or lasts over one hour")
    except Exception: errors.append("invalid expiry")
    entry, stop, target = (float(t.get(k, 0)) for k in ("limit_price", "stop_price", "target_price"))
    if not 0 < stop < entry < target: errors.append("prices must satisfy stop < entry < target")
    if entry and notional * (entry - stop) / entry > min(2.50, equity * .10): errors.append("stop risk exceeds cap")
    if errors: raise TicketRejected("; ".join(errors))

