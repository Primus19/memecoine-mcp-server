"""Candidate strategies for research. See each class docstring for the evidence behind it."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import StrategyConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------
#
# Strategy contract.
#
# A strategy turns a feature frame (see indicators.feature_frame) and a regime series
# into a *decision frame* with one row per bar. The same decision frame drives the
# backtester and the live engine, so there is exactly one code path to trust.
#
# Decision frame columns
#   long_entry / short_entry : bool  enter at the NEXT bar's open (no look-ahead)
#   long_exit / short_exit   : bool  exit an open position at the next open
#   stop_atr_mult            : float initial stop distance in ATR multiples
#   trailing                 : bool  stop trails the best price (chandelier)
#   target_r                 : float take-profit as a multiple of initial risk (nan = none)
#   max_hold                 : int   maximum bars in trade (0 = unlimited)
#   confidence               : float 0..1 signal quality (used by ensemble & sizing)
#   strategy                 : str   strategy name for attribution
#



DECISION_COLUMNS = ["long_entry", "short_entry", "long_exit", "short_exit", "stop_atr_mult",
                    "trailing", "target_r", "max_hold", "confidence", "strategy"]


def empty_decisions(index: pd.Index, strategy: str = "") -> pd.DataFrame:
    out = pd.DataFrame(index=index)
    out["long_entry"] = False
    out["short_entry"] = False
    out["long_exit"] = False
    out["short_exit"] = False
    out["stop_atr_mult"] = np.nan
    out["trailing"] = False
    out["target_r"] = np.nan
    out["max_hold"] = 0
    out["confidence"] = 0.0
    out["strategy"] = strategy
    return out


@dataclass
class Decision:
    """The last row of a decision frame, as a plain object for the live engine/API."""
    symbol: str
    timestamp: str
    side: int                 # +1 long, -1 short, 0 none
    strategy: str
    confidence: float
    regime: str
    price: float
    atr: float
    stop_price: float
    target_price: Optional[float]
    trailing: bool
    max_hold: int
    reason: str
    exit_long: bool = False
    exit_short: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timestamp": self.timestamp,
            "action": "buy" if self.side > 0 else "sell" if self.side < 0 else "hold",
            "strategy": self.strategy, "confidence": round(self.confidence, 4), "regime": self.regime,
            "price": self.price, "atr": self.atr, "stop_price": self.stop_price,
            "target_price": self.target_price, "trailing_stop": self.trailing, "max_hold_bars": self.max_hold,
            "reason": self.reason, "exit_long": self.exit_long, "exit_short": self.exit_short,
        }


class Strategy:
    name = "base"
    regimes = {"trend", "range", "quiet", "volatile"}  # regimes this strategy may trade in

    def decide(self, features: pd.DataFrame, regime: pd.Series, allow_short: bool = False) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def gate(self, decisions: pd.DataFrame, regime: pd.Series) -> pd.DataFrame:
        """Zero out entries outside the strategy's permitted regimes."""
        allowed = regime.isin(self.regimes)
        decisions = decisions.copy()
        decisions.loc[~allowed, ["long_entry", "short_entry"]] = False
        decisions.loc[~allowed, "confidence"] = 0.0
        return decisions

# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------
#
# Trend / breakout strategy (Donchian breakout with trend filter and ATR trailing stop).
#
# Why this one: time-series momentum / trend following is the most robust documented
# edge across futures, FX and crypto (Moskowitz, Ooi & Pedersen 2012; Hurst, Ooi &
# Pedersen 2017 "A Century of Evidence on Trend-Following"; Liu & Tsyvinski 2021 for
# crypto). It loses often in small amounts and wins rarely in large amounts, so the
# exit (trailing stop) is the whole game; there is no fixed take-profit.
#
# Rules
#   long  : close breaks above the 55-bar Donchian high, EMA50 > EMA200 (or close above
#           EMA200), ADX >= threshold, not in a volatility blow-off
#   short : mirror image (only if shorting is allowed for the venue)
#   exit  : close below the 20-bar Donchian low (long) / above 20-bar high (short),
#           or the 2.5-ATR chandelier trailing stop
# Confidence rises with ADX, efficiency ratio and distance of the breakout from the
# channel, and falls when volatility is extreme.
#




class TrendBreakoutStrategy(Strategy):
    name = "trend_breakout"
    regimes = {"trend", "quiet"}

    def __init__(self, cfg: StrategyConfig | None = None):
        self.cfg = cfg or StrategyConfig()

    def decide(self, f: pd.DataFrame, regime: pd.Series, allow_short: bool = False) -> pd.DataFrame:
        cfg = self.cfg
        d = empty_decisions(f.index, self.name)
        close = f["close"]
        up_trend = (f["ema_50"] > f["ema_200"]) | (close > f["ema_200"])
        down_trend = (f["ema_50"] < f["ema_200"]) | (close < f["ema_200"])
        strong = f["adx_14"] >= cfg.adx_trend_threshold
        not_blowoff = (f["vol_pctile"].fillna(0.5) < 0.97) & (f["ret_5"].abs() < 4 * f["atr_pct"] * np.sqrt(5))
        breakout_up = close > f["donchian55_upper"]
        breakout_down = close < f["donchian55_lower"]

        # Only the first bar of a breakout is an entry (avoid re-entering every bar of a run).
        first_up = breakout_up & ~breakout_up.shift(1, fill_value=False)
        first_down = breakout_down & ~breakout_down.shift(1, fill_value=False)

        d["long_entry"] = (first_up & up_trend & strong & not_blowoff).fillna(False)
        if allow_short:
            d["short_entry"] = (first_down & down_trend & strong & not_blowoff).fillna(False)
        d["long_exit"] = (close < f["donchian20_lower"]).fillna(False)
        d["short_exit"] = (close > f["donchian20_upper"]).fillna(False)

        adx_score = ((f["adx_14"] - cfg.adx_trend_threshold) / 30.0).clip(0, 1)
        er_score = f["eff_ratio_20"].clip(0, 1)
        vol_penalty = (f["vol_pctile"].fillna(0.5) - 0.8).clip(0, 0.2) * 2  # 0..0.4 penalty
        conf = (0.45 + 0.30 * adx_score + 0.25 * er_score - vol_penalty).clip(0, 0.95)
        d["confidence"] = conf.where(d["long_entry"] | d["short_entry"], 0.0).fillna(0.0)
        d["stop_atr_mult"] = cfg.atr_stop_multiple
        d["trailing"] = True
        d["target_r"] = np.nan
        d["max_hold"] = 0
        return self.gate(d, regime)

# ---------------------------------------------------------------------------
# mean_reversion
# ---------------------------------------------------------------------------
#
# Mean reversion strategy for ranging markets (Bollinger z-score + 2-period RSI).
#
# Why: short-horizon reversal is well documented in FX majors and large-cap crypto when
# markets are not trending (Larry Connors' RSI(2) family; Lehmann 1990 reversal). It is
# the natural complement to trend following: it wins small and often, loses rarely but
# larger, so it MUST be regime-gated (ADX low) and carry a hard stop and time stop.
#
# Rules
#   long  : z-score(20) <= -entry AND RSI(2) <= entry AND ADX < range threshold AND
#           price above the 200 EMA (only fade dips inside a larger uptrend for spot
#           crypto; forex may trade both sides)
#   short : mirror image if shorting is allowed
#   exit  : z-score crosses back above 0 (long) / below 0 (short), 2-ATR stop, or the
#           time stop (max_hold bars)
#




class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    regimes = {"range", "quiet"}

    def __init__(self, cfg: StrategyConfig | None = None, require_trend_alignment: bool = True):
        self.cfg = cfg or StrategyConfig()
        self.require_trend_alignment = require_trend_alignment

    def decide(self, f: pd.DataFrame, regime: pd.Series, allow_short: bool = False) -> pd.DataFrame:
        cfg = self.cfg
        d = empty_decisions(f.index, self.name)
        z = f["zscore_20"]
        calm = f["adx_14"] < cfg.adx_range_threshold
        not_extreme = f["vol_pctile"].fillna(0.5) < 0.90
        above_200 = f["close"] > f["ema_200"]
        below_200 = f["close"] < f["ema_200"]
        long_setup = (z <= -cfg.zscore_entry) & (f["rsi_2"] <= cfg.rsi2_entry) & calm & not_extreme
        short_setup = (z >= cfg.zscore_entry) & (f["rsi_2"] >= 100 - cfg.rsi2_entry) & calm & not_extreme
        if self.require_trend_alignment:
            long_setup &= above_200
            short_setup &= below_200
        d["long_entry"] = long_setup.fillna(False)
        if allow_short:
            d["short_entry"] = short_setup.fillna(False)
        d["long_exit"] = (z >= cfg.zscore_exit).fillna(False)
        d["short_exit"] = (z <= -cfg.zscore_exit).fillna(False)
        depth = ((z.abs() - cfg.zscore_entry) / 1.5).clip(0, 1)
        calm_score = ((cfg.adx_range_threshold - f["adx_14"]) / cfg.adx_range_threshold).clip(0, 1)
        conf = (0.45 + 0.30 * depth + 0.20 * calm_score).clip(0, 0.9)
        d["confidence"] = conf.where(d["long_entry"] | d["short_entry"], 0.0).fillna(0.0)
        d["stop_atr_mult"] = cfg.mr_atr_stop_multiple
        d["trailing"] = False
        d["target_r"] = np.nan  # exit on z-score reversion instead of a fixed target
        d["max_hold"] = cfg.mr_max_hold_bars
        return self.gate(d, regime)

# ---------------------------------------------------------------------------
# momentum_xs
# ---------------------------------------------------------------------------
#
# Cross-sectional momentum ranking for a crypto universe.
#
# Liu, Tsyvinski & Wu (2022, Journal of Finance, "Common Risk Factors in Cryptocurrency")
# document a robust cross-sectional momentum factor: coins with the strongest trailing
# 1-4 week returns outperform the weakest over the next 1-4 weeks. We use it as a
# *universe filter*: only the top-k coins by 30-day return (skipping the last 2 days to
# avoid short-term reversal) and with positive absolute momentum are eligible for
# long entries. This concentrates the trend strategy on the coins most likely to keep
# trending and keeps it out of the laggards.
#




def cross_sectional_momentum_ranks(closes: Dict[str, pd.Series], lookback: int = 30, skip: int = 2) -> pd.DataFrame:
    """Return a DataFrame indexed by time with one column per symbol: rank (1 = best) of
    trailing momentum among symbols with data at that bar. NaN when insufficient data."""
    frame = pd.DataFrame(closes).sort_index()
    mom = frame.shift(skip) / frame.shift(lookback + skip) - 1.0
    ranks = mom.rank(axis=1, ascending=False, method="min")
    return ranks.where(mom.notna())


def eligible_universe(closes: Dict[str, pd.Series], top_k: int = 3, lookback: int = 30, skip: int = 2) -> pd.DataFrame:
    """Boolean frame: True where a symbol is in the top-k by momentum AND has positive momentum."""
    frame = pd.DataFrame(closes).sort_index()
    mom = frame.shift(skip) / frame.shift(lookback + skip) - 1.0
    ranks = mom.rank(axis=1, ascending=False, method="min")
    return ((ranks <= top_k) & (mom > 0)).fillna(False)

# ---------------------------------------------------------------------------
# ml
# ---------------------------------------------------------------------------
#
# Machine-learning direction filter (gradient boosting) with honest, leakage-free
# walk-forward evaluation.
#
# This is deliberately a *filter*, not a signal generator. Pure ML price prediction
# on daily bars has near-zero out-of-sample edge; what it can do is veto rule-based
# entries whose feature context historically ended badly. Many hobby engines train
# regressors on raw price with price itself as a feature (trivial leakage) and report
# in-sample R^2; this module avoids that:
#
#   * labels each bar by whether the forward `horizon` return beat round-trip cost
#     (+1), lost more than cost (-1), or was noise (dropped)
#   * trains only on bars whose labels are fully known (embargo of `horizon` bars)
#   * evaluates strictly out-of-sample by expanding-window walk-forward
#   * reports OOS accuracy / log-loss so nobody mistakes in-sample fit for skill
#   * degrades gracefully to "no opinion" (probability 0.5) if scikit-learn is missing
#


try:  # optional dependency
    from sklearn.ensemble import HistGradientBoostingClassifier
    _HAS_SKLEARN = True
except Exception:  # noqa: BLE001
    _HAS_SKLEARN = False

FEATURES: List[str] = [
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_20", "ret_60", "dist_ema_50", "dist_ema_200",
    "rsi_14", "rsi_2", "macd_hist", "atr_pct", "adx_14", "di_diff", "zscore_20", "bb_width",
    "donchian_pos", "vol_20", "vol_ratio", "vol_pctile", "eff_ratio_20", "mom_30_skip2", "mom_90",
    "volume_ratio_20", "dow",
]


@dataclass
class MLReport:
    samples: int
    oos_accuracy: Optional[float]
    oos_logloss: Optional[float]
    baseline_accuracy: Optional[float]
    enabled: bool

    def to_dict(self) -> dict:
        return self.__dict__


class MLDirectionFilter:
    def __init__(self, horizon: int = 5, cost_bps: float = 60.0, min_train: int = 300,
                 refit_every: int = 20, enabled: bool = True):
        self.horizon = horizon
        self.cost = cost_bps / 10_000
        self.min_train = min_train
        self.refit_every = refit_every
        self.enabled = enabled and _HAS_SKLEARN
        self.model = None
        self.report = MLReport(0, None, None, None, self.enabled)

    @staticmethod
    def _make_model():
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150,
                                              l2_regularization=1.0, min_samples_leaf=30, random_state=7)

    def _xy(self, f: pd.DataFrame):
        cols = [c for c in FEATURES if c in f.columns]
        X = f[cols].astype(float)
        fwd = f["close"].shift(-self.horizon) / f["close"] - 1.0
        y = pd.Series(np.nan, index=f.index)
        y[fwd > self.cost] = 1.0
        y[fwd < -self.cost] = 0.0
        return X, y

    def walk_forward_probabilities(self, f: pd.DataFrame) -> pd.Series:
        """Out-of-sample P(up) for every bar, using only information available before it."""
        probs = pd.Series(0.5, index=f.index)
        if not self.enabled:
            return probs
        X, y = self._xy(f)
        valid_feature = X.notna().all(axis=1)
        n = len(f)
        model = None
        preds, truths, baseline = [], [], []
        for i in range(self.min_train, n):
            if model is None or (i - self.min_train) % self.refit_every == 0:
                # labels for bars < i - horizon are known at bar i (embargo)
                train_idx = np.arange(0, i - self.horizon)
                mask = valid_feature.values[train_idx] & y.notna().values[train_idx]
                if mask.sum() < self.min_train // 2:
                    continue
                model = self._make_model()
                model.fit(X.values[train_idx][mask], y.values[train_idx][mask])
            if not valid_feature.iloc[i]:
                continue
            p = float(model.predict_proba(X.values[i:i + 1])[0, 1])
            probs.iloc[i] = p
            if not np.isnan(y.iloc[i]):
                preds.append(p)
                truths.append(y.iloc[i])
                baseline.append(y.values[:i - self.horizon][~np.isnan(y.values[:i - self.horizon])].mean())
        if truths:
            preds_a, truths_a = np.array(preds), np.array(truths)
            eps = 1e-6
            self.report = MLReport(
                samples=len(truths),
                oos_accuracy=float(((preds_a > 0.5) == (truths_a > 0.5)).mean()),
                oos_logloss=float(-np.mean(truths_a * np.log(preds_a + eps) + (1 - truths_a) * np.log(1 - preds_a + eps))),
                baseline_accuracy=float(max(truths_a.mean(), 1 - truths_a.mean())),
                enabled=True,
            )
        self.model = model
        return probs

    def fit(self, f: pd.DataFrame) -> None:
        if not self.enabled:
            return
        X, y = self._xy(f)
        mask = X.notna().all(axis=1) & y.notna()
        mask.iloc[-self.horizon:] = False  # labels unknown for the last `horizon` bars
        if mask.sum() < self.min_train // 2:
            self.model = None
            return
        self.model = self._make_model()
        self.model.fit(X[mask].values, y[mask].values)

    def probability_up(self, f_row: pd.Series) -> float:
        if not self.enabled or self.model is None:
            return 0.5
        cols = [c for c in FEATURES if c in f_row.index]
        x = f_row[cols].astype(float).values.reshape(1, -1)
        if np.isnan(x).any():
            return 0.5
        return float(self.model.predict_proba(x)[0, 1])

# ---------------------------------------------------------------------------
# ensemble
# ---------------------------------------------------------------------------
#
# Regime-gated ensemble. Each component strategy proposes entries only in the regime it
# was designed for; the ensemble keeps the highest-confidence proposal per bar, applies
# the ML direction filter as a veto, and requires a minimum confidence.
#





class EnsembleStrategy(Strategy):
    name = "ensemble"

    def __init__(self, cfg: Optional[StrategyConfig] = None, strategies: Optional[List[Strategy]] = None,
                 ml_filter: Optional[MLDirectionFilter] = None):
        self.cfg = cfg or StrategyConfig()
        self.strategies = strategies or [TrendBreakoutStrategy(self.cfg), MeanReversionStrategy(self.cfg)]
        self.ml = ml_filter

    def decide(self, f: pd.DataFrame, regime: pd.Series, allow_short: bool = False,
               ml_probs: Optional[pd.Series] = None) -> pd.DataFrame:
        best = empty_decisions(f.index, "")
        best_conf = pd.Series(0.0, index=f.index)
        for strat in self.strategies:
            d = strat.decide(f, regime, allow_short)
            d = d.copy()
            d["confidence"] = d["confidence"].astype(float)
            is_entry = d["long_entry"] | d["short_entry"]
            better = is_entry & (d["confidence"] > best_conf)
            for col in best.columns:
                best.loc[better, col] = d.loc[better, col]
            best_conf = best_conf.where(~better, d["confidence"])
            # exits are unioned: any strategy asking to exit its own position wins
            best["long_exit"] = best["long_exit"] | d["long_exit"].where(d["strategy"] == best["strategy"], False)
            best["short_exit"] = best["short_exit"] | d["short_exit"].where(d["strategy"] == best["strategy"], False)
        # no trading in blow-off volatility
        best.loc[regime == "volatile", ["long_entry", "short_entry"]] = False

        if ml_probs is not None and self.cfg.ml_enabled:
            p = ml_probs.reindex(f.index).fillna(0.5)
            veto_long = best["long_entry"] & (p < self.cfg.ml_min_probability)
            veto_short = best["short_entry"] & (p > 1 - self.cfg.ml_min_probability)
            best.loc[veto_long, "long_entry"] = False
            best.loc[veto_short, "short_entry"] = False
            # tilt confidence by how far the model is from indifference
            tilt = (p - 0.5).abs() * 0.4
            best["confidence"] = (best["confidence"] + tilt).clip(0, 0.99)
        low = best["confidence"] < self.cfg.min_confidence
        best.loc[low, ["long_entry", "short_entry"]] = False
        best.loc[~(best["long_entry"] | best["short_entry"]), "confidence"] = 0.0
        return best

