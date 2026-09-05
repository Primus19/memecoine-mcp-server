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
    emerging_enabled: bool = True
    emerging_min_score: float = 84.0
    emerging_min_market_cap_usd: float = 10_000_000.0
    emerging_min_volume_24h_usd: float = 1_000_000.0
    emerging_max_spread_bps: float = 30.0
    emerging_max_slippage_bps: float = 30.0
    estimated_fee_bps_per_side: float = 120.0
    minimum_net_edge_bps: float = 50.0
    # Execution-cost controls. Coinbase Advanced charges roughly half as much for
    # maker (post-only limit) fills as for taker fills at every tier; on a 120 bps
    # taker venue that is the single largest lever on net expectancy.
    maker_fee_bps_per_side: float = 60.0
    entry_post_only: bool = False
    # Momentum-gate shape. Replay evidence (docs/research) shows the 1h>0 filter
    # mostly adds churn; keep it on by default for continuity, switch off via env.
    require_positive_1h: bool = True

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
            emerging_enabled=_truthy("LIVE_EMERGING_MEME_ENABLED", True),
            emerging_min_score=float(os.getenv("LIVE_EMERGING_MIN_SCORE", "84")),
            emerging_min_market_cap_usd=float(os.getenv("LIVE_EMERGING_MIN_MARKET_CAP_USD", "10000000")),
            emerging_min_volume_24h_usd=float(os.getenv("LIVE_EMERGING_MIN_VOLUME_24H_USD", "1000000")),
            emerging_max_spread_bps=float(os.getenv("LIVE_EMERGING_MAX_SPREAD_BPS", "30")),
            emerging_max_slippage_bps=float(os.getenv("LIVE_EMERGING_MAX_SLIPPAGE_BPS", "30")),
            estimated_fee_bps_per_side=float(os.getenv("LIVE_ESTIMATED_FEE_BPS_PER_SIDE", "120")),
            minimum_net_edge_bps=float(os.getenv("LIVE_MIN_NET_EDGE_BPS", "50")),
            maker_fee_bps_per_side=float(os.getenv("LIVE_MAKER_FEE_BPS_PER_SIDE", "60")),
            entry_post_only=_truthy("LIVE_ENTRY_POST_ONLY", False),
            require_positive_1h=_truthy("LIVE_REQUIRE_POSITIVE_1H", True),
        )

    @property
    def entry_fee_bps_per_side(self) -> float:
        """Fee actually paid on entry: maker when post-only, taker otherwise."""
        return self.maker_fee_bps_per_side if self.entry_post_only else self.estimated_fee_bps_per_side

    @property
    def round_trip_fee_bps(self) -> float:
        """Entry fee plus exit fee. Exits are market/bracket (taker) in this pilot."""
        return self.entry_fee_bps_per_side + self.estimated_fee_bps_per_side

    def tier(self, market_cap_usd: Any, volume_24h_usd: Any) -> str:
        cap, volume = float(market_cap_usd or 0), float(volume_24h_usd or 0)
        if cap >= self.min_market_cap_usd and volume >= self.min_volume_24h_usd:
            return "ESTABLISHED"
        if (self.emerging_enabled and cap >= self.emerging_min_market_cap_usd
                and volume >= self.emerging_min_volume_24h_usd):
            return "EMERGING"
        return "INELIGIBLE"

    def minimum_score_for(self, tier: str) -> float:
        return self.emerging_min_score if tier == "EMERGING" else self.min_score

    def regime_allowed(self, regime: Any) -> bool:
        value = str(regime or "").upper()
        return value == "RISING" or (self.allow_mixed_regime and value == "MIXED")

    def news_allowed(self, news_score: Any, *, news_veto: bool = False) -> bool:
        if news_veto:
            return False
        if not self.require_news_catalyst:
            return True
        return float(news_score or 0) >= self.min_news_score_when_required
