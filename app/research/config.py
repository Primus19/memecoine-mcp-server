"""Research configuration (costs, risk, strategy parameters). Env-overridable."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class CostConfig:
    """Round-trip cost assumptions in basis points. Defaults mirror production policy:
    LIVE_ESTIMATED_FEE_BPS_PER_SIDE (120 bps = Coinbase Advanced retail taker)."""
    fee_bps_per_side: float = 120.0
    spread_bps: float = 10.0
    slippage_bps_per_side: float = 5.0

    @property
    def round_trip_bps(self) -> float:
        return 2 * self.fee_bps_per_side + self.spread_bps + 2 * self.slippage_bps_per_side

    @classmethod
    def for_asset_class(cls, asset_class: str) -> "CostConfig":
        if asset_class == "forex":
            return cls(fee_bps_per_side=_f("RESEARCH_FX_FEE_BPS", 0.0),
                       spread_bps=_f("RESEARCH_FX_SPREAD_BPS", 1.5),
                       slippage_bps_per_side=_f("RESEARCH_FX_SLIPPAGE_BPS", 0.5))
        return cls(fee_bps_per_side=_f("RESEARCH_CRYPTO_FEE_BPS", _f("LIVE_ESTIMATED_FEE_BPS_PER_SIDE", 120.0)),
                   spread_bps=_f("RESEARCH_CRYPTO_SPREAD_BPS", 10.0),
                   slippage_bps_per_side=_f("RESEARCH_CRYPTO_SLIPPAGE_BPS", 5.0))


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01        # 1% of equity at the initial stop
    max_position_notional_pct: float = 0.25
    max_open_positions: int = 4
    max_gross_leverage: float = 1.0        # forex research may set 2-3; crypto spot stays 1
    daily_loss_limit_pct: float = 0.03
    max_drawdown_halt_pct: float = 0.20
    drawdown_throttle_pct: float = 0.07     # halve size beyond this drawdown
    consecutive_loss_throttle: int = 3
    symbol_cooldown_bars: int = 2
    min_expected_edge_bps: float = 10.0
    target_annual_vol: float = 0.15
    kelly_fraction_cap: float = 0.25


@dataclass
class StrategyConfig:
    donchian_entry: int = 55
    donchian_exit: int = 20
    atr_stop_multiple: float = 2.5
    adx_trend_threshold: float = 20.0
    zscore_entry: float = 2.0
    zscore_exit: float = 0.0
    rsi2_entry: float = 10.0
    mr_max_hold_bars: int = 10
    mr_atr_stop_multiple: float = 2.0
    adx_range_threshold: float = 22.0
    xs_lookback: int = 30
    xs_skip: int = 2
    xs_top_k: int = 3
    ml_enabled: bool = True
    ml_min_probability: float = 0.55
    ml_horizon: int = 5
    ml_min_train: int = 300
    min_confidence: float = 0.55
    allow_short: bool = False


def asset_class_for(symbol: str) -> str:
    base, _, quote = symbol.upper().replace("-", "/").replace("_", "/").partition("/")
    fiat = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SEK", "NOK", "SGD", "HKD", "MXN", "ZAR"}
    metals = {"XAU", "XAG"}
    if (base in fiat or base in metals) and quote in fiat:
        return "forex"
    if quote in fiat | {"USDT", "USDC"}:
        return "crypto"
    return "stock"
