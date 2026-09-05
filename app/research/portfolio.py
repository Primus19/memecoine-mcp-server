"""
Portfolio-rebalance backtests for the strategies with the strongest published,
out-of-sample evidence. These are *allocation* rules rebalanced on a schedule, not
per-trade stop/target tickets, which is why their costs are low and their evidence is
durable:

  * Time-series momentum (TSMOM): hold an asset when its trailing 1-12 month return is
    positive, scale by inverse volatility. Moskowitz, Ooi & Pedersen (2012); Hurst,
    Ooi & Pedersen (2017) show it across 100+ years and every liquid asset class.
    In crypto: Liu & Tsyvinski (2021, RFS).
  * Cross-sectional momentum (XSMOM): rank a universe by trailing 2-4 week return,
    hold the top-k. Liu, Tsyvinski & Wu (2022, JF) document it in crypto specifically.

Both are evaluated with the production cost model (fee per side + spread + slippage
charged on every unit of turnover) and compared with buy-and-hold.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .config import CostConfig
from .indicators import periods_per_year


def _metrics(equity: pd.Series, turnover: pd.Series, ppy: int) -> dict:
    rets = equity.pct_change().dropna()
    years = max(len(equity) / ppy, 1e-9)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1.0
    vol = rets.std(ddof=0) * math.sqrt(ppy) if len(rets) > 1 else 0.0
    sharpe = rets.mean() * ppy / vol if vol > 0 else 0.0
    dd = (equity / equity.cummax() - 1).min()
    yearly = equity.resample("YE").last().pct_change().dropna() if len(equity) > ppy else pd.Series(dtype=float)
    return {"cagr_pct": round(float(cagr) * 100, 2), "annual_vol_pct": round(float(vol) * 100, 2),
            "sharpe": round(float(sharpe), 2), "max_drawdown_pct": round(float(dd) * 100, 2),
            "calmar": round(float(cagr / abs(dd)), 2) if dd < 0 else None,
            "annual_turnover": round(float(turnover.sum() / years), 2),
            "final_multiple": round(float(equity.iloc[-1] / equity.iloc[0]), 3),
            "yearly_returns_pct": {str(k.year): round(float(v) * 100, 1) for k, v in yearly.items()},
            "years": round(years, 2)}


def _apply(closes: pd.DataFrame, weights: pd.DataFrame, cost: CostConfig, ppy: int) -> dict:
    """weights: target weights decided at the close of day t, applied to day t+1's return."""
    # Never forward-fill a missing price.  That silently grants a stale asset a
    # zero return and can preserve an untradeable weight in a changing universe.
    rets = closes.pct_change(fill_method=None)
    tradable = closes.notna() & closes.shift(1).notna()
    w = weights.shift(1).where(tradable, 0.0).fillna(0.0)  # no look-ahead
    turnover = (w - w.shift(1).fillna(0.0)).abs().sum(axis=1)
    unit_cost = (cost.fee_bps_per_side + cost.spread_bps / 2 + cost.slippage_bps_per_side) / 10_000
    port = (w * rets.fillna(0.0)).sum(axis=1) - turnover * unit_cost
    equity = (1 + port).cumprod() * 10_000
    out = _metrics(equity, turnover, ppy)
    out["avg_gross_exposure"] = round(float(w.abs().sum(axis=1).mean()), 2)
    out["symbols"] = int(closes.shape[1])
    out["observations"] = int(len(closes))
    out["data_start"] = str(closes.index.min())
    out["data_end"] = str(closes.index.max())
    return out


def buy_and_hold(closes: pd.DataFrame, symbol: str, ppy: int) -> dict:
    s = closes[symbol].dropna()
    equity = s / s.iloc[0] * 10_000
    return _metrics(equity, pd.Series(0.0, index=equity.index), ppy)


def tsmom(closes: pd.DataFrame, *, lookback: int = 90, rebalance: int = 7, vol_window: int = 30,
          target_vol: float = 0.20, max_weight: float = 0.5, long_only: bool = True,
          cost: Optional[CostConfig] = None, ppy: int = 365, ma_filter: Optional[int] = None) -> dict:
    """Time-series momentum. Signal = sign of trailing `lookback` return; weight = signal *
    target_vol / realized_vol, capped, rebalanced every `rebalance` bars. `ma_filter`
    additionally requires price above its N-day moving average (a common robustness tweak)."""
    cost = cost or CostConfig.for_asset_class("crypto")
    mom = closes / closes.shift(lookback) - 1
    signal = np.sign(mom)
    if long_only:
        signal = signal.clip(lower=0)
    if ma_filter:
        above = closes > closes.rolling(ma_filter).mean()
        signal = signal.where(above | (signal < 0), 0.0)
    vol = closes.pct_change(fill_method=None).rolling(vol_window).std(ddof=0) * math.sqrt(ppy)
    raw = (signal * (target_vol / vol.replace(0, np.nan))).clip(-max_weight, max_weight).fillna(0.0)
    # rebalance only every `rebalance` bars
    keep = pd.Series(np.arange(len(raw)) % rebalance == 0, index=raw.index)
    weights = raw.where(keep).ffill().fillna(0.0)
    # cap gross exposure at 1.0 (no leverage) by scaling down proportionally
    gross = weights.abs().sum(axis=1)
    weights = weights.div(gross.where(gross > 1.0, 1.0), axis=0)
    out = _apply(closes, weights, cost, ppy)
    out.update({"strategy": "TSMOM", "lookback": lookback, "rebalance": rebalance, "target_vol": target_vol,
                "long_only": long_only, "ma_filter": ma_filter, "cost_bps_per_side": cost.fee_bps_per_side})
    return out


def xsmom(closes: pd.DataFrame, *, lookback: int = 28, skip: int = 2, top_k: int = 3, rebalance: int = 7,
          abs_filter: bool = True, vol_window: int = 30, target_vol: float = 0.25,
          cost: Optional[CostConfig] = None, ppy: int = 365) -> dict:
    """Cross-sectional momentum: every `rebalance` bars, hold the top-k assets by trailing
    return (skipping the most recent `skip` bars), equal-risk weighted, only if their
    own momentum is positive (abs_filter)."""
    cost = cost or CostConfig.for_asset_class("crypto")
    mom = closes.shift(skip) / closes.shift(lookback + skip) - 1
    ranks = mom.rank(axis=1, ascending=False, method="first")
    selected = ranks <= top_k
    if abs_filter:
        selected &= mom > 0
    vol = closes.pct_change(fill_method=None).rolling(vol_window).std(ddof=0) * math.sqrt(ppy)
    inv_vol = (1.0 / vol.replace(0, np.nan)).where(selected, 0.0).fillna(0.0)
    weights = inv_vol.div(inv_vol.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    # scale to target vol using the cap-weighted average vol of holdings, no leverage
    port_vol = (weights * vol).sum(axis=1)
    scale = (target_vol / port_vol.replace(0, np.nan)).clip(upper=1.0).fillna(0.0)
    weights = weights.mul(scale, axis=0)
    keep = pd.Series(np.arange(len(weights)) % rebalance == 0, index=weights.index)
    weights = weights.where(keep).ffill().fillna(0.0)
    out = _apply(closes, weights, cost, ppy)
    out.update({"strategy": "XSMOM", "lookback": lookback, "skip": skip, "top_k": top_k, "rebalance": rebalance,
                "abs_filter": abs_filter, "cost_bps_per_side": cost.fee_bps_per_side})
    return out


def parameter_sweep(closes: pd.DataFrame, fn, grid: Dict[str, list], **fixed) -> pd.DataFrame:
    """Evaluate a small grid and return a table; used to check robustness (a real edge is
    flat across neighbouring parameters, an overfit one is a spike)."""
    import itertools
    rows = []
    keys = list(grid)
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        try:
            res = fn(closes, **params, **fixed)
        except Exception as exc:  # noqa: BLE001
            res = {"error": str(exc)}
        rows.append({**params, **{k: res.get(k) for k in ("cagr_pct", "sharpe", "max_drawdown_pct", "annual_turnover")}})
    return pd.DataFrame(rows)
