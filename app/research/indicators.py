"""
Vectorized technical indicators (pandas). Every function takes a DataFrame with
open/high/low/close/volume (or a Series) and returns a Series aligned to the input.

These are the textbook definitions (Wilder smoothing for RSI/ATR/ADX, a real MACD
signal line, etc.). The previous engines in this repo approximated several of these
with random numbers or constants; nothing here is approximated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _wilder(true_range(df), period)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Returns DataFrame with plus_di, minus_di, adx (Wilder)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = _wilder(true_range(df), period)
    plus_di = 100 * _wilder(plus_dm, period) / tr.replace(0.0, np.nan)
    minus_di = 100 * _wilder(minus_dm, period) / tr.replace(0.0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": _wilder(dx, period)})


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + num_std * std, "lower": mid - num_std * std, "std": std})


def zscore(close: pd.Series, period: int = 20) -> pd.Series:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return (close - mid) / std.replace(0.0, np.nan)


def donchian(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Channel of the *previous* `period` bars (shifted by one so a breakout of the
    current bar can be detected without look-ahead)."""
    upper = df["high"].rolling(period, min_periods=period).max().shift(1)
    lower = df["low"].rolling(period, min_periods=period).min().shift(1)
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2})


def realized_vol(close: pd.Series, period: int = 20, periods_per_year: int = 365) -> pd.Series:
    """Annualized close-to-close volatility."""
    returns = np.log(close / close.shift(1))
    return returns.rolling(period, min_periods=period).std(ddof=0) * np.sqrt(periods_per_year)


def efficiency_ratio(close: pd.Series, period: int = 20) -> pd.Series:
    """Kaufman efficiency ratio: |net change| / sum(|changes|). 1 = perfect trend, 0 = pure noise."""
    change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period, min_periods=period).sum()
    return (change / volatility.replace(0.0, np.nan)).clip(0.0, 1.0)


def momentum(close: pd.Series, period: int, skip: int = 0) -> pd.Series:
    """Return over `period` bars ending `skip` bars ago (skip avoids short-term reversal)."""
    return close.shift(skip) / close.shift(period + skip) - 1.0


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period, min_periods=period).mean()
    return volume / avg.replace(0.0, np.nan)


def periods_per_year(timeframe: str, asset_class: str = "crypto") -> int:
    days = 365 if asset_class == "crypto" else 252
    return {"1h": days * 24, "4h": days * 6, "1d": days, "1w": 52}.get(timeframe, days)


def feature_frame(df: pd.DataFrame, timeframe: str = "1d", asset_class: str = "crypto") -> pd.DataFrame:
    """All indicators used by strategies and the ML filter, in one aligned frame."""
    ppy = periods_per_year(timeframe, asset_class)
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["ret_1"] = close.pct_change()
    for n in (3, 5, 10, 20, 60):
        out[f"ret_{n}"] = close.pct_change(n)
    out["ema_20"] = ema(close, 20)
    out["ema_50"] = ema(close, 50)
    out["ema_200"] = ema(close, 200)
    out["dist_ema_50"] = close / out["ema_50"] - 1.0
    out["dist_ema_200"] = close / out["ema_200"] - 1.0
    out["rsi_14"] = rsi(close, 14)
    out["rsi_2"] = rsi(close, 2)
    m = macd(close)
    out["macd_hist"] = m["hist"] / close
    out["atr_14"] = atr(df, 14)
    out["atr_pct"] = out["atr_14"] / close
    a = adx(df, 14)
    out["adx_14"] = a["adx"]
    out["di_diff"] = (a["plus_di"] - a["minus_di"]) / 100.0
    out["zscore_20"] = zscore(close, 20)
    b = bollinger(close, 20)
    out["bb_width"] = (b["upper"] - b["lower"]) / b["mid"]
    d55 = donchian(df, 55)
    d20 = donchian(df, 20)
    out["donchian55_upper"] = d55["upper"]
    out["donchian55_lower"] = d55["lower"]
    out["donchian20_upper"] = d20["upper"]
    out["donchian20_lower"] = d20["lower"]
    out["donchian_pos"] = ((close - d55["lower"]) / (d55["upper"] - d55["lower"]).replace(0.0, np.nan)).clip(0, 1)
    out["vol_20"] = realized_vol(close, 20, ppy)
    out["vol_60"] = realized_vol(close, 60, ppy)
    out["vol_ratio"] = out["vol_20"] / out["vol_60"].replace(0.0, np.nan)
    out["vol_pctile"] = out["vol_20"].rolling(250, min_periods=60).rank(pct=True)
    out["eff_ratio_20"] = efficiency_ratio(close, 20)
    out["mom_30_skip2"] = momentum(close, 30, 2)
    out["mom_90"] = momentum(close, 90)
    out["volume_ratio_20"] = volume_ratio(df["volume"], 20) if df["volume"].abs().sum() > 0 else 1.0
    if hasattr(df.index, "dayofweek"):
        out["dow"] = df.index.dayofweek
    return out
