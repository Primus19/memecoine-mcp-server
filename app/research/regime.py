"""
Market regime classification. Strategies are only allowed to trade in the regime
they were designed for; that single rule removes most whipsaw losses.

  TREND      : ADX above threshold and efficiency ratio high -> breakout/trend strategies
  RANGE      : ADX low, price oscillating around its mean   -> mean reversion
  VOLATILE   : realized vol in the top decile of its history -> stand aside / halve size
  QUIET      : everything else (neutral)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


TREND = "trend"
RANGE = "range"
VOLATILE = "volatile"
QUIET = "quiet"


@dataclass
class RegimeConfig:
    adx_trend: float = 25.0
    adx_range: float = 20.0
    er_trend: float = 0.30
    vol_pctile_extreme: float = 0.90
    vol_ratio_extreme: float = 2.0


def classify(features: pd.DataFrame, cfg: RegimeConfig | None = None) -> pd.Series:
    cfg = cfg or RegimeConfig()
    adx = features["adx_14"]
    er = features["eff_ratio_20"]
    vol_pctile = features.get("vol_pctile", pd.Series(0.5, index=features.index))
    vol_ratio = features.get("vol_ratio", pd.Series(1.0, index=features.index))
    regime = pd.Series(QUIET, index=features.index, dtype=object)
    regime[(adx >= cfg.adx_trend) | ((adx >= cfg.adx_range) & (er >= cfg.er_trend))] = TREND
    regime[(adx < cfg.adx_range) & (er < cfg.er_trend)] = RANGE
    regime[(vol_pctile >= cfg.vol_pctile_extreme) & (vol_ratio >= cfg.vol_ratio_extreme)] = VOLATILE
    regime[adx.isna()] = QUIET
    return regime


def describe(features_row: pd.Series, regime: str) -> str:
    return (f"{regime.upper()} regime: ADX={features_row.get('adx_14', float('nan')):.1f}, "
            f"ER={features_row.get('eff_ratio_20', float('nan')):.2f}, "
            f"vol20={features_row.get('vol_20', float('nan')):.0%}, "
            f"z={features_row.get('zscore_20', float('nan')):.2f}")
