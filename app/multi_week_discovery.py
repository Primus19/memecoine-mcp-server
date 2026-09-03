from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class ConfirmationLedger:
    """Durable evidence that a setup persisted; repeated minute scans do not count."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def observe(self, identity: str, observed_at: str, preliminary_pass: bool) -> dict[str, Any]:
        values = self._read()
        if not preliminary_pass:
            values.pop(identity, None)
            self._write(values)
            return {"confirmation_count": 0, "confirmation_span_hours": 0.0}
        now = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(UTC)
        row = values.get(identity)
        if not row:
            row = {"first": now.isoformat(), "last": now.isoformat(), "count": 1}
        else:
            first = datetime.fromisoformat(str(row["first"]).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(row["last"]).replace("Z", "+00:00"))
            # Only an observation at least twelve hours after the first setup
            # proves persistence. Fast repeated scans remain one observation.
            if (now - first).total_seconds() >= 12 * 3600 and (now - last).total_seconds() >= 3600:
                row["count"] = max(2, int(row.get("count") or 1) + 1)
                row["last"] = now.isoformat()
        values[identity] = row
        self._write(values)
        first = datetime.fromisoformat(str(row["first"]).replace("Z", "+00:00"))
        return {
            "confirmation_count": int(row["count"]),
            "confirmation_span_hours": round(max(0.0, (now - first).total_seconds() / 3600), 4),
        }

    def _write(self, values: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def derive_snapshot(asset: dict[str, Any], ledger: ConfirmationLedger) -> dict[str, Any]:
    """Convert a universe asset with daily OHLCV into a normalized paper snapshot."""
    candles = [row for row in asset.get("daily_candles", []) if isinstance(row, dict)]
    candles.sort(key=lambda row: str(row.get("observed_at") or row.get("date") or ""))
    closes = [_number(row.get("close")) for row in candles if _number(row.get("close")) > 0]
    highs = [_number(row.get("high")) for row in candles if _number(row.get("high")) > 0]
    lows = [_number(row.get("low")) for row in candles if _number(row.get("low")) > 0]
    volumes = [_number(row.get("volume_usd")) for row in candles]
    price = _number(asset.get("price"), closes[-1] if closes else 0)
    average20 = _mean(closes[-20:])
    high20 = max(highs[-20:] or [price])
    relative7 = ((price / closes[-8] - 1) * 100 if len(closes) >= 8 else 0) - _number(
        asset.get("benchmark_return_7d_pct"))
    recent_volume = _mean(volumes[-7:])
    prior_volume = _mean(volumes[-14:-7])
    volume_ratio = recent_volume / prior_volume if prior_volume > 0 else 0
    higher_highs = len(highs) >= 3 and highs[-3] < highs[-2] < highs[-1]
    higher_lows = len(lows) >= 3 and lows[-3] < lows[-2] < lows[-1]
    extension = price / average20 - 1 if average20 > 0 else 99
    pullback = 1 - price / high20 if high20 > 0 else 1
    range3 = (max(highs[-3:] or [price]) / max(min(lows[-3:] or [price]), 1e-30) - 1)
    consolidation = 0 <= pullback <= .15 and range3 <= .12 and extension <= .20
    observed_at = str(asset.get("observed_at") or datetime.now(UTC).isoformat())
    identity = f"{str(asset.get('chain') or '').lower()}:{str(asset.get('contract') or '').lower()}"
    cex_mode = asset.get("execution_evidence_mode") == "CEX_ORDER_BOOK"
    preliminary = all((
        len(closes) >= 20, price > average20 > 0, higher_highs, higher_lows,
        relative7 > 0, volume_ratio >= 1.10,
        (_number(asset.get("holder_growth_7d_pct")) > 0 or cex_mode),
        asset.get("sell_route_ok") is True,
        (asset.get("security_verified") is True or
         (cex_mode and asset.get("venue_operational") is True)),
    ))
    confirmations = ledger.observe(identity, observed_at, preliminary)
    return {
        **asset,
        "asset_class": "CRYPTO", "price": price, "observed_at": observed_at,
        "price_above_20d_average": price > average20 > 0,
        "daily_higher_highs": higher_highs, "daily_higher_lows": higher_lows,
        "relative_strength_7d_pct": round(relative7, 6),
        "volume_7d_vs_prior_ratio": round(volume_ratio, 6),
        "extension_from_20d_fraction": round(extension, 6),
        "controlled_pullback_or_consolidation": consolidation,
        "derived_20d_average": round(average20, 12),
        "derived_20d_high": round(high20, 12),
        "derived_pullback_fraction": round(pullback, 6),
        "daily_candle_count": len(closes),
        **confirmations,
    }


def discover(payload: dict[str, Any], ledger: ConfirmationLedger) -> list[dict[str, Any]]:
    """Rank the complete upstream universe; there is no fixed token watchlist."""
    assets = payload.get("crypto_universe") or []
    if not isinstance(assets, list):
        return []
    snapshots = [derive_snapshot(asset, ledger) for asset in assets if isinstance(asset, dict)]
    return sorted(snapshots, key=lambda row: (
        bool(row.get("price_above_20d_average")),
        bool(row.get("daily_higher_lows")),
        _number(row.get("relative_strength_7d_pct")),
        _number(row.get("liquidity_usd")),
    ), reverse=True)
