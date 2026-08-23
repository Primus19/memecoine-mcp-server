from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def enrich_with_coinbase(
    candidate: dict[str, Any],
    *,
    product: dict[str, Any],
    quote: dict[str, Any],
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Replace caller-asserted execution facts with live Coinbase evidence.

    Research remains responsible for regime, momentum, market cap, news,
    tokenomics and safety.  Coinbase is authoritative for whether the product is
    a tradable USDC spot market and for the executable spread/slippage.
    """
    value = deepcopy(candidate)
    requested = str(value.get("product_id", "")).upper()
    actual = str(product.get("product_id", "")).upper()
    spot = product.get("product_type") in (None, "SPOT")
    tradable = not any(
        bool(product.get(key, False))
        for key in ("trading_disabled", "view_only", "cancel_only")
    )
    identity_verified = bool(requested and requested == actual and actual.endswith("-USDC"))
    now = checked_at or datetime.now(timezone.utc).isoformat()

    value.update(
        {
            "product_id": requested,
            "identity_verified": identity_verified,
            "spot_available": bool(identity_verified and spot and tradable),
            "spread_bps": float(quote["spread_bps"]),
            "slippage_bps": float(quote["slippage_bps"]),
            "coinbase_checked_at": now,
            "coinbase_evidence": {
                "product_id": actual,
                "product_type": product.get("product_type"),
                "trading_disabled": bool(product.get("trading_disabled", False)),
                "view_only": bool(product.get("view_only", False)),
                "cancel_only": bool(product.get("cancel_only", False)),
                "best_bid": float(quote["best_bid"]),
                "best_ask": float(quote["best_ask"]),
                "average_buy_price": float(quote["average_buy_price"]),
                "visible_quote_filled": float(quote["visible_quote_filled"]),
                "checked_at": now,
            },
        }
    )
    return value
