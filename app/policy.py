from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def _truthy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OpportunityPolicy:
    """Configurable opportunity gates; execution and safety controls stay hard."""

    min_score: float = 78.0
    allow_mixed_regime: bool = True
    require_news_catalyst: bool = False
    min_news_score_when_required: float = 4.0
    min_market_cap_usd: float = 25_000_000.0
    min_volume_24h_usd: float = 5_000_000.0
    min_turnover: float = 0.03
    max_turnover: float = 1.50
    max_momentum_24h_pct: float = 15.0

    @classmethod
    def from_env(cls) -> "OpportunityPolicy":
        return cls(
            min_score=float(os.getenv("LIVE_MIN_MODEL_SCORE", "78")),
            allow_mixed_regime=_truthy("LIVE_ALLOW_MIXED_REGIME", True),
            require_news_catalyst=_truthy("LIVE_REQUIRE_NEWS_CATALYST", False),
            min_news_score_when_required=float(os.getenv("LIVE_MIN_NEWS_SCORE", "4")),
            min_market_cap_usd=float(os.getenv("LIVE_MIN_MARKET_CAP_USD", "25000000")),
            min_volume_24h_usd=float(os.getenv("LIVE_MIN_VOLUME_24H_USD", "5000000")),
            min_turnover=float(os.getenv("LIVE_MIN_TURNOVER", "0.03")),
            max_turnover=float(os.getenv("LIVE_MAX_TURNOVER", "1.50")),
            max_momentum_24h_pct=float(os.getenv("LIVE_MAX_24H_GAIN_PCT", "15")),
        )

    def regime_allowed(self, regime: Any) -> bool:
        value = str(regime or "").upper()
        return value == "RISING" or (self.allow_mixed_regime and value == "MIXED")

    def news_allowed(self, news_score: Any, *, news_veto: bool = False) -> bool:
        if news_veto:
            return False
        if not self.require_news_catalyst:
            return True
        return float(news_score or 0) >= self.min_news_score_when_required

