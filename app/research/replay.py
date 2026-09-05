"""
Replay real history through the pilot's OWN production rules.

The production services only ever see a live snapshot, so nobody knows whether the
rules they enforce had positive expectancy in the past. This module rebuilds the
snapshot dictionaries from historical candles and runs:

  * crypto  : the Model 3.1 momentum/regime gate (research_feed.score semantics:
              1h > 0, 0 < 24h <= 15%, 7d > 0 for full momentum points, plus a
              market-breadth regime) with the research-feed stop/target/trail
              geometry and lifecycle.supervision_levels exits, under the production
              cost model (LIVE_ESTIMATED_FEE_BPS_PER_SIDE).
  * forex   : market_feed.forex_snapshot-style features from H1/H4/D candles, then
              multi_asset.ForexEngine.evaluate, forex_executor.trend_continuation_signals
              and bryne_liquidity_signals, closed by the paper supervisor's rules
              (stop, target, profit-protection floor).

Every replay records the exact rule variant so results can be compared: e.g. whether
requiring a positive 1h return helps or hurts the crypto sleeve.
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .. import forex_executor as fx
from ..lifecycle import supervision_levels
from .. import multi_asset as _multi_asset
from ..multi_asset import AssetPolicy, ForexEngine, MultiAssetRejected
from ..quant import average_true_range, ewma_volatility, horizon_return, multi_horizon_consensus
from ..validation import promotion_gate
from .config import CostConfig

UTC = timezone.utc


@dataclass
class ReplayTrade:
    symbol: str
    strategy: str
    side: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    return_pct: float      # net of costs, on notional
    r_multiple: float
    reason: str
    bars: int


@dataclass
class ReplayResult:
    strategy: str
    variant: str
    trades: List[ReplayTrade]
    signals_seen: int
    rejections: Dict[str, int] = field(default_factory=dict)

    def summary(self, cost_stress_bps: float = 0.0) -> dict:
        rets = [t.return_pct for t in self.trades]
        rs = [t.r_multiple for t in self.trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        gate = promotion_gate(rets, minimum_samples=100, cost_stress=cost_stress_bps / 10_000)
        return {
            "strategy": self.strategy, "variant": self.variant, "signals_seen": self.signals_seen,
            "trades": len(rets),
            "win_rate": round(len(wins) / len(rets), 3) if rets else None,
            "avg_return_bps": round(float(np.mean(rets)) * 10_000, 1) if rets else None,
            "median_return_bps": round(float(np.median(rets)) * 10_000, 1) if rets else None,
            "expectancy_r": round(float(np.mean(rs)), 3) if rs else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
            "avg_bars": round(float(np.mean([t.bars for t in self.trades])), 1) if self.trades else None,
            "lower_confidence_bound_bps": round(gate.lower_confidence_bound * 10_000, 1) if rets else None,
            "promotion_gate": {"eligible": gate.eligible, "reasons": list(gate.reasons)},
            "exit_reasons": pd.Series([t.reason for t in self.trades]).value_counts().to_dict() if self.trades else {},
            "top_rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])[:6]),
        }


# ---------------------------------------------------------------------------
# Crypto: Model 3.1 momentum gate + research-feed geometry + lifecycle exits
# ---------------------------------------------------------------------------

def replay_model31_crypto(hourly: Dict[str, pd.DataFrame], *, cost: CostConfig, require_positive_1h: bool = True,
                          max_24h_pct: float = 15.0, chase_cap_1h_pct: float = 3.0, require_7d: bool = True,
                          regime_from: Optional[pd.DataFrame] = None, max_hold_hours: int = 72,
                          one_position: bool = True, min_gap_hours: int = 1) -> ReplayResult:
    """Replay the crypto entry gate on hourly candles.

    Entry at the next hour's open when the gate passes (fees/spread/slippage charged).
    Stop / target / trail come from research_feed.build_candidate's geometry:
      stop_pct = clamp(vol_proxy * 2.5, 4, 8), target = clamp(stop*2.5, 8, 20)
      trail activation = clamp(stop*.75, 3, 6), trail = clamp(stop*.5, 2, 4)
    Exits use lifecycle.supervision_levels exactly as production does, checked hourly.
    A "regime" is derived from breadth across the supplied universe (share of coins
    with positive 24h return) as research_feed.regime does.
    """
    variant = (f"1h>0={'on' if require_positive_1h else 'off'},7d>0={'on' if require_7d else 'off'},"
               f"24h<= {max_24h_pct:g}%,hold<= {max_hold_hours}h,fee={cost.fee_bps_per_side:g}bps/side")
    closes = pd.DataFrame({s: df["close"] for s, df in hourly.items()}).sort_index().ffill()
    ret_24 = closes.pct_change(24) * 100
    ret_1 = closes.pct_change(1) * 100
    ret_7d = closes.pct_change(24 * 7) * 100
    breadth = (ret_24 > 0).mean(axis=1)
    med_1, med_24, med_7 = ret_1.median(axis=1), ret_24.median(axis=1), ret_7d.median(axis=1)
    rising = (breadth >= 0.60) & (med_1 > 0) & (med_24 > 0) & (med_7 > 0)
    falling = (breadth <= 0.40) & (med_1 < 0) & (med_24 < 0)
    regime = pd.Series("MIXED", index=closes.index)
    regime[rising] = "RISING"
    regime[falling] = "FALLING"
    policy_fee = cost.fee_bps_per_side
    trades: List[ReplayTrade] = []
    rejections: Dict[str, int] = {}
    seen = 0
    open_until: Dict[str, int] = {}
    portfolio_busy_until = -1
    index = closes.index
    for symbol, df in hourly.items():
        df = df.reindex(index).dropna(subset=["open", "close"])
        loc = {ts: i for i, ts in enumerate(df.index)}
        o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
        n = len(df)
        i = 24 * 7 + 1
        while i < n - 2:
            ts = df.index[i]
            one, day, week = ret_1.at[ts, symbol], ret_24.at[ts, symbol], ret_7d.at[ts, symbol]
            reg = regime.at[ts]
            fails = []
            if reg == "FALLING":
                fails.append("regime FALLING")
            if require_positive_1h and not one > 0:
                fails.append("1h not positive")
            if one > chase_cap_1h_pct:
                fails.append("1h above chase cap")
            if not (0 < day <= max_24h_pct):
                fails.append("24h outside (0, max]")
            if require_7d and not week > 0:
                fails.append("7d not positive")
            if math.isnan(one) or math.isnan(day) or math.isnan(week):
                fails.append("insufficient history")
            if fails:
                for f in fails:
                    rejections[f] = rejections.get(f, 0) + 1
                i += 1
                continue
            seen += 1
            if one_position and i <= portfolio_busy_until:
                rejections["one-position limit"] = rejections.get("one-position limit", 0) + 1
                i += 1
                continue
            vol_proxy = max(abs(one), abs(day) / 24 ** .5, abs(week) / 168 ** .5)
            stop_pct = min(8.0, max(4.0, vol_proxy * 2.5))
            target_pct = min(20.0, max(8.0, stop_pct * 2.5))
            entry_raw = o[i + 1]
            entry = entry_raw * (1 + (cost.spread_bps / 2 + cost.slippage_bps_per_side) / 10_000)
            ticket = {"stop_price": entry * (1 - stop_pct / 100), "target_price": entry * (1 + target_pct / 100),
                      "trail_activation_pct": min(6.0, max(3.0, stop_pct * .75)), "trail_pct": min(4.0, max(2.0, stop_pct * .5)),
                      "spread_bps": cost.spread_bps, "slippage_bps": cost.slippage_bps_per_side,
                      "opportunity_policy": {"estimated_fee_bps_per_side": policy_fee, "minimum_net_edge_bps": 50}}
            high_water = entry
            exit_price, reason, j = None, "", i + 1
            for j in range(i + 2, min(n, i + 2 + max_hold_hours)):
                # intra-bar hard stop / target, then supervision on the close
                if l[j] <= ticket["stop_price"]:
                    exit_price, reason = min(o[j], ticket["stop_price"]), "HARD_STOP"
                    break
                if h[j] >= ticket["target_price"]:
                    exit_price, reason = max(o[j], ticket["target_price"]), "TARGET"
                    break
                high_water = max(high_water, h[j])
                mom = (c[j] / c[j - 1] - 1) * 100
                levels = supervision_levels(ticket, entry=entry, mark=c[j], high_water=high_water,
                                            regime=regime.at[df.index[j]], momentum_1h_pct=mom,
                                            falling_observations=3 if regime.at[df.index[j]] == "FALLING" else 0)
                if levels["exit_reason"]:
                    exit_price, reason = c[j], levels["exit_reason"]
                    break
            else:
                j = min(n - 1, i + 1 + max_hold_hours)
                exit_price, reason = c[j], "MAX_HOLD"
            exit_net = exit_price * (1 - (cost.spread_bps / 2 + cost.slippage_bps_per_side) / 10_000)
            gross = exit_net / entry - 1
            fees = 2 * cost.fee_bps_per_side / 10_000
            net = gross - fees
            risk = stop_pct / 100 + fees
            trades.append(ReplayTrade(symbol, "MODEL_3_1_MOMENTUM", "BUY", ts.isoformat(), df.index[j].isoformat(),
                                      float(entry), float(exit_net), float(net), float(net / risk), reason, j - i))
            portfolio_busy_until = j + min_gap_hours
            i = j + 1
    return ReplayResult("MODEL_3_1_MOMENTUM", variant, trades, seen, rejections)


# ---------------------------------------------------------------------------
# Forex: rebuild market_feed.forex_snapshot from candles and run the live rules
# ---------------------------------------------------------------------------

def _candles(df: pd.DataFrame) -> List[dict]:
    return [{"time": ts.isoformat(), "mid": {"o": str(r.open), "h": str(r.high), "l": str(r.low), "c": str(r.close)},
             "complete": True} for ts, r in df.iterrows()]


def build_forex_snapshot(symbol: str, h1: pd.DataFrame, at: int, spread_bps: float) -> dict:
    """Mirror market_feed.forex_snapshot using only history up to and including H1 bar `at`."""
    window = h1.iloc[max(0, at - 240): at + 1]
    h4 = window.resample("4h", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d1 = h1.iloc[max(0, at - 24 * 60): at + 1].resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    closes = window["close"].tolist()
    h4_closes = h4["close"].tolist()
    d1_closes = d1["close"].tolist()
    mid = float(closes[-1])
    half = mid * spread_bps / 2 / 10_000
    bid, ask = mid - half, mid + half
    change_1h = horizon_return(closes, 1)
    change_4h = horizon_return(h4_closes, 1)
    change_24h = horizon_return(closes, 24)
    change_5d = horizon_return(h4_closes, 30)
    change_20d = horizon_return(d1_closes, 20)
    consensus = multi_horizon_consensus((change_1h, change_4h, change_24h, change_5d, change_20d), (.10, .15, .25, .25, .25))
    trend = (closes[-1] - statistics.mean(closes[-20:])) / max(abs(closes[-1]) * .01, 1e-9)
    h1_candles = _candles(window.tail(120))
    atr = average_true_range(h1_candles, 14)
    ewma = ewma_volatility(closes[-60:]) * mid
    ts = h1.index[at]
    m5_like = [{"time": (ts - timedelta(minutes=5 * (11 - k))).isoformat(), "open": float(window["open"].iloc[-1]),
                "high": float(window["high"].iloc[-1]), "low": float(window["low"].iloc[-1]),
                "close": float(window["close"].iloc[-1])} for k in range(12)]
    return {"asset_class": "FOREX", "symbol": symbol, "price": mid, "bid": bid, "ask": ask,
            "five_streak_candles": m5_like,
            "bryne_h1_candles": [{"time": c["time"], "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
                                  "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in h1_candles[-80:]],
            "spread_bps": spread_bps, "median_spread_bps": spread_bps, "bid_liquidity": 1_000_000, "ask_liquidity": 1_000_000,
            "quote_age_seconds": 0.5, "tradable": True, "market_veto": False, "high_impact_calendar_blackout": False,
            "calendar_verified": True, "economic_event_source": "https://replay.local/calendar-not-modelled",
            "economic_event_blackout_distance_minutes": 10080,
            "observed_at": ts.isoformat(), "source_urls": ["https://replay.local/history"],
            "change_1h_pct": change_1h, "change_4h_pct": change_4h, "change_24h_pct": change_24h,
            "change_5d_pct": change_5d, "change_20d_pct": change_20d,
            "horizon_direction": consensus["direction"], "horizon_agreement": consensus["agreement"],
            "trend_strength": max(-1, min(1, trend)), "liquidity_score": 1.0, "session_liquid": True,
            "economic_event_within_minutes": 10080, "atr_14": atr, "ewma_volatility_price": ewma,
            "stop_distance": max(1.5 * atr, 2.0 * ewma, mid * .0015), "long_financing_rate": 0.0,
            "short_financing_rate": 0.0, "financing_days": [], "maximum_loss_usd": 2.5, "reward_multiple": 2.0,
            "expiry_seconds": 300, "thesis": "replay", "invalidation": "replay"}


def _simulate_forex_exit(h1: pd.DataFrame, start: int, side: str, entry: float, stop: float, target: float,
                         spread_bps: float, max_bars: int, profit_floor: bool = True) -> tuple[float, str, int]:
    """Bar-by-bar exit using the paper supervisor's rules: hard stop, target, and the
    five_streak_profit_floor_r ratchet (production applies it to every forex sleeve)."""
    risk = abs(entry - stop)
    direction = 1 if side == "BUY" else -1
    half = spread_bps / 2 / 10_000
    best_r = 0.0
    for j in range(start, min(len(h1), start + max_bars)):
        o, hi, lo, c = (float(h1.iloc[j][k]) for k in ("open", "high", "low", "close"))
        exec_stop = stop
        if side == "BUY":
            if lo * (1 - half) <= exec_stop:
                return min(o, exec_stop) * (1 - half), "STOP", j
            if hi * (1 - half) >= target:
                return max(o, target) * (1 - half), "TARGET", j
            pnl_r = (c * (1 - half) - entry) / risk
        else:
            if hi * (1 + half) >= exec_stop:
                return max(o, exec_stop) * (1 + half), "STOP", j
            if lo * (1 + half) <= target:
                return min(o, target) * (1 + half), "TARGET", j
            pnl_r = (entry - c * (1 + half)) / risk
        best_r = max(best_r, pnl_r)
        floor_r = fx.five_streak_profit_floor_r(best_r) if profit_floor else 0.0
        if floor_r > 0 and pnl_r <= floor_r:
            return (c * (1 - half) if side == "BUY" else c * (1 + half)), f"PROFIT_PROTECTION_{floor_r:.2f}R", j
    j = min(len(h1) - 1, start + max_bars - 1)
    c = float(h1.iloc[j]["close"])
    return (c * (1 - half) if side == "BUY" else c * (1 + half)), "MAX_HOLD", j


def replay_forex(hourly: Dict[str, pd.DataFrame], *, spread_bps: float = 1.5, max_hold_bars: int = 48,
                 min_score: float = 80.0, sleeves: Optional[List[str]] = None) -> Dict[str, ReplayResult]:
    """Run ForexEngine (score/alignment), trend continuation and Bryne V5 on H1 history."""
    sleeves = sleeves or ["FOREX_ENGINE", "TREND_CONTINUATION", "BRYNE_V5"]
    engine = ForexEngine(AssetPolicy(minimum_score=min_score))
    results = {s: ReplayResult(s, f"spread={spread_bps}bps,max_hold={max_hold_bars}h,min_score={min_score:g}", [], 0) for s in sleeves}
    os.environ.setdefault("FOREX_TREND_MAX_SPREAD_BPS", "3")
    os.environ.setdefault("FOREX_BRYNE_V5_MAX_SPREAD_BPS", "3")
    for symbol, h1 in hourly.items():
        h1 = h1.dropna()
        busy = {s: -1 for s in sleeves}
        for at in range(24 * 25, len(h1) - 2):
            snap = build_forex_snapshot(symbol.replace("/", "_"), h1, at, spread_bps)
            # The engine's staleness checks compare observed_at with "now"; freeze the
            # clock at the bar being replayed so history is not rejected as stale.
            bar_time = h1.index[at].to_pydatetime()
            _multi_asset.now_utc = lambda _t=bar_time: _t
            if "FOREX_ENGINE" in sleeves and at > busy["FOREX_ENGINE"]:
                r = results["FOREX_ENGINE"]
                try:
                    proposal = engine.evaluate(snap)
                    r.signals_seen += 1
                    px, reason, j = _simulate_forex_exit(h1, at + 1, proposal.side, proposal.reference_price,
                                                         proposal.stop_price, proposal.target_price, spread_bps, max_hold_bars)
                    risk = abs(proposal.reference_price - proposal.stop_price)
                    d = 1 if proposal.side == "BUY" else -1
                    net = (px - proposal.reference_price) / proposal.reference_price * d
                    r.trades.append(ReplayTrade(symbol, proposal.strategy, proposal.side, h1.index[at].isoformat(),
                                                h1.index[j].isoformat(), proposal.reference_price, px, net,
                                                (px - proposal.reference_price) * d / risk, reason, j - at))
                    busy["FOREX_ENGINE"] = j + 1
                except MultiAssetRejected as exc:
                    for part in str(exc).split("; ")[:3]:
                        r.rejections[part] = r.rejections.get(part, 0) + 1
            for name, fn in (("TREND_CONTINUATION", fx.trend_continuation_signals), ("BRYNE_V5", fx.bryne_liquidity_signals)):
                if name not in sleeves or at <= busy[name]:
                    continue
                r = results[name]
                try:
                    signals = fn(snap)
                except MultiAssetRejected as exc:
                    r.rejections[str(exc)[:60]] = r.rejections.get(str(exc)[:60], 0) + 1
                    continue
                if not signals:
                    continue
                p = signals[0]
                r.signals_seen += 1
                px, reason, j = _simulate_forex_exit(h1, at + 1, p["side"], p["reference_price"], p["stop_price"],
                                                     p["target_price"], spread_bps, max_hold_bars)
                risk = abs(p["reference_price"] - p["stop_price"])
                d = 1 if p["side"] == "BUY" else -1
                net = (px - p["reference_price"]) / p["reference_price"] * d
                r.trades.append(ReplayTrade(symbol, name, p["side"], h1.index[at].isoformat(), h1.index[j].isoformat(),
                                            p["reference_price"], px, net, (px - p["reference_price"]) * d / risk, reason, j - at))
                busy[name] = j + 1
    _multi_asset.now_utc = lambda: datetime.now(UTC)
    return results
