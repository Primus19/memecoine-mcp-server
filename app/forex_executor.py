from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .broker_adapters import BrokerError, OandaAdapter
from .forex_email import ForexReportEmailer
from .forex_report import render_forex_report
from .multi_asset import AssetPolicy, ForexEngine, MultiAssetRejected
from .validation import promotion_gate
from .version import FOREX_MODEL_VERSION, deployment_info

UTC = timezone.utc
LOCK = threading.RLock()
STATE = {"ok": False, "mode": "STARTING", "last_scan": "", "last_error": "", "open_positions": 0,
         "report": {}}


def truthy(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def five_streak_enabled() -> bool:
    return os.getenv("FOREX_FIVE_STREAK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def broker_client_id(intent_id: str) -> str:
    """Return the exact restart-safe ID written into OANDA extensions."""
    return str(intent_id).replace("-", "")[:32]


def validated_snapshots(payload: dict, now: datetime | None = None) -> list[dict]:
    snapshots = payload.get("snapshots", [])
    if not isinstance(snapshots, list) or not snapshots:
        raise MultiAssetRejected("market feed returned no tradable snapshots")
    try:
        scanned_at = datetime.fromisoformat(str(payload.get("scanned_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MultiAssetRejected("market feed scan timestamp is missing or invalid") from exc
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=UTC)
    max_age = max(30, int(os.getenv("FOREX_MARKET_FEED_MAX_AGE_SECONDS", "180")))
    if ((now or datetime.now(UTC)) - scanned_at.astimezone(UTC)).total_seconds() > max_age:
        raise MultiAssetRejected("market feed snapshot is stale")
    if any(item.get("calendar_verified") is not True for item in snapshots):
        raise MultiAssetRejected("market feed lacks verified economic calendar evidence")
    return snapshots


def calendar_execution_allowed(snapshot: dict) -> bool:
    """Fail closed when pair-level calendar evidence is contradictory."""
    return (
        snapshot.get("calendar_verified") is True and
        str(snapshot.get("economic_event_source", "")).startswith("https://") and
        int(snapshot.get("economic_event_within_minutes") or 0) <= 0 and
        snapshot.get("high_impact_calendar_blackout") is not True
    )


FIVE_STREAK_STRATEGY = "FOREX_FIVE_STREAK_EXPERIMENT"
FIVE_STREAK_FILTERED_V3_STRATEGY = "FOREX_FIVE_STREAK_FILTERED_V3"
FIVE_STREAK_FILTERED_STRATEGY = "FOREX_FIVE_STREAK_FILTERED_V4_RATCHET"
BRYNE_LIQUIDITY_V5_STRATEGY = "FOREX_BRYNE_LIQUIDITY_RANGE_V5"
FIVE_STREAK_DISPLAY_NAME = "Bryne and Lot-Bill Strategy"


def five_streak_position_pnl(position: dict, price: float) -> float:
    direction = 1 if position["side"] == "BUY" else -1
    risk_distance = abs(float(position["entry_price"]) - float(position["stop_price"]))
    if risk_distance <= 0 or price <= 0:
        return 0.0
    return ((price - float(position["entry_price"])) * direction / risk_distance
            * float(position["maximum_loss_usd"]))


def five_streak_profit_floor_r(maximum_favorable_r: float) -> float:
    """Paper-only ratchet; the protected floor can only move upward."""
    if maximum_favorable_r >= 1.0:
        return .75
    if maximum_favorable_r >= .75:
        return .50
    if maximum_favorable_r >= .50:
        return .20
    return 0.0


def live_profit_protection_shadow(trade: dict, maximum_favorable_r: float) -> dict:
    """Evaluate the Bryne ratchet against a live trade without changing orders.

    This is deliberately observation-only. A paper result is not sufficient
    authority to move an OANDA stop, but collecting the same R-normalized
    measurements makes a prospective promotion decision possible.
    """
    entry = float(trade.get("entry_price") or 0)
    stop = float(trade.get("stop_price") or 0)
    current = float(trade.get("current_price") or 0)
    side = str(trade.get("side") or "")
    risk_distance = abs(entry - stop)
    if not entry or not current or risk_distance <= 0 or side not in {"BUY", "SELL"}:
        return {"trade_id": str(trade.get("trade_id") or ""), "eligible": False,
                "shadow_only": True, "reason": "missing executable entry, stop, or current price"}
    direction = 1 if side == "BUY" else -1
    current_r = (current - entry) * direction / risk_distance
    peak_r = max(float(maximum_favorable_r or 0), current_r)
    floor_r = five_streak_profit_floor_r(peak_r)
    return {
        "trade_id": str(trade.get("trade_id") or ""), "instrument": trade.get("instrument"),
        "side": side, "eligible": True, "shadow_only": True,
        "source_strategy": FIVE_STREAK_FILTERED_STRATEGY,
        "challenger": "BRYNE_RATCHET_TRANSFER_V1", "current_r": round(current_r, 6),
        "maximum_favorable_r": round(peak_r, 6), "protected_floor_r": floor_r,
        "would_exit_now": floor_r > 0 and current_r <= floor_r,
        "adoption_gate": "30-50 independent closed shadow observations with positive cost-stressed expectancy",
    }


def live_profit_exit_decision(trade: dict, snapshot: dict, maximum_favorable_r: float,
                              minimum_score: float) -> dict:
    """Return a guarded, executable live profit-retention decision."""
    shadow = live_profit_protection_shadow(trade, maximum_favorable_r)
    if not shadow.get("eligible"):
        return {**shadow, "execute": False, "reason": shadow.get("reason")}
    current_r = float(shadow["current_r"])
    peak_r = float(shadow["maximum_favorable_r"])
    floor_r = float(shadow["protected_floor_r"])
    alignment, _, proposed_side = ForexEngine.alignment(snapshot)
    score = ForexEngine.score(snapshot)
    side = str(trade.get("side") or "")
    quote_fresh = float(snapshot.get("quote_age_seconds") or 999) <= 10
    executable = float(trade.get("current_price") or 0) > 0
    session_ok = snapshot.get("session_liquid") is True and snapshot.get("tradable") is True
    ratchet_exit = floor_r > 0 and current_r <= floor_r
    min_peak = max(.10, float(os.getenv("FOREX_LIVE_DETERIORATION_MIN_MFE_R", ".10")))
    min_retained = max(.01, float(os.getenv("FOREX_LIVE_DETERIORATION_MIN_CURRENT_R", ".02")))
    giveback_fraction = ((peak_r - current_r) / peak_r) if peak_r > 0 else 0.0
    required_giveback = min(.90, max(.20, float(os.getenv(
        "FOREX_LIVE_DETERIORATION_GIVEBACK_FRACTION", ".40"))))
    contradictory = (alignment == "CONTRADICTORY" or not proposed_side or proposed_side != side
                     or score < minimum_score - 10)
    deterioration_exit = (peak_r >= min_peak and current_r >= min_retained
                          and giveback_fraction >= required_giveback and contradictory)
    execute = bool(quote_fresh and executable and session_ok and (ratchet_exit or deterioration_exit))
    reason = (f"PROFIT_RATCHET_{floor_r:.2f}R" if ratchet_exit else
              "PROFIT_RETENTION_ALIGNMENT_DETERIORATION" if deterioration_exit else "HOLD")
    return {
        **shadow, "shadow_only": False, "execute": execute, "reason": reason,
        "alignment": alignment, "current_score": round(score, 4),
        "minimum_score": minimum_score, "giveback_fraction": round(giveback_fraction, 6),
        "quote_fresh": quote_fresh, "session_liquid": session_ok,
        "safeguard": "Target remains active unless profit ratchet or confirmed thesis deterioration triggers",
    }


def early_thesis_failure_shadow(position: dict, snapshot: dict, current_pnl_usd: float,
                                maximum_favorable_pnl_usd: float,
                                elapsed_minutes: float) -> dict:
    """Measure a no-follow-through exit without changing a live or paper trade."""
    risk = float(position.get("maximum_loss_usd") or 0)
    side = str(position.get("side") or "")
    if risk <= 0 or side not in {"BUY", "SELL"}:
        return {"eligible": False, "shadow_only": True, "reason": "missing risk or side"}
    current_r = current_pnl_usd / risk
    maximum_favorable_r = max(0.0, maximum_favorable_pnl_usd / risk)
    alignment, _, proposed_side = ForexEngine.alignment(snapshot)
    score = ForexEngine.score(snapshot)
    minimum_minutes = max(15.0, float(os.getenv("FOREX_THESIS_FAILURE_MINUTES", "30")))
    maximum_mfe_r = min(.10, max(0.0, float(os.getenv("FOREX_THESIS_FAILURE_MAX_MFE_R", ".05"))))
    maximum_current_r = min(-.01, float(os.getenv("FOREX_THESIS_FAILURE_CURRENT_R", "-.05")))
    contradictory = (alignment == "CONTRADICTORY" or not proposed_side or proposed_side != side
                     or score < float(os.getenv("FOREX_THESIS_FAILURE_SCORE", "70")))
    would_exit = (elapsed_minutes >= minimum_minutes and maximum_favorable_r <= maximum_mfe_r
                  and current_r <= maximum_current_r and contradictory)
    return {
        "eligible": True, "shadow_only": True, "experiment": "ZERO_MFE_THESIS_FAILURE_V1",
        "would_exit_now": would_exit, "elapsed_minutes": round(elapsed_minutes, 3),
        "current_r": round(current_r, 6), "maximum_favorable_r": round(maximum_favorable_r, 6),
        "alignment": alignment, "proposed_side": proposed_side, "current_score": round(score, 4),
        "reason": "NO_FOLLOW_THROUGH_AND_THESIS_DETERIORATION" if would_exit else "HOLD",
        "adoption_gate": "30-50 independent closes with positive cost-stressed expectancy",
    }


def signal_close_time(value: str | None) -> datetime | None:
    """OANDA M5 candle times identify candle open, not signal availability."""
    if not value:
        return None
    try:
        opened = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return opened.astimezone(UTC) + timedelta(minutes=5)
    except ValueError:
        return None


def five_streak_baseline_signals(snapshot: dict) -> list[dict]:
    """Reproduce the attached v2 specification for comparison only."""
    candles = list(snapshot.get("five_streak_candles") or [])
    streak, signals = 0, []
    for index, candle in enumerate(candles):
        if index == 0:
            continue
        opened, closed, prior = float(candle["open"]), float(candle["close"]), float(candles[index - 1]["close"])
        green = closed > opened and closed > prior
        red = closed < opened and closed < prior
        streak = (streak + 1 if streak > 0 else 1) if green else \
                 (streak - 1 if streak < 0 else -1) if red else 0
        # Only the newest closed candle is actionable. Older candles establish
        # streak context but must not create retrospective paper fills.
        if abs(streak) < 5 or index < 4 or index != len(candles) - 1:
            continue
        side = "BUY" if streak > 0 else "SELL"
        anchor = candles[index - 4]
        signal_close = closed
        execution_entry = float(snapshot.get("ask") if side == "BUY" else snapshot.get("bid") or signal_close)
        stop = float(anchor["low"] if side == "BUY" else anchor["high"])
        if (side == "BUY" and stop >= execution_entry) or (side == "SELL" and stop <= execution_entry):
            raise MultiAssetRejected("Five-Streak stop is on the wrong side of executable entry")
        distance = abs(execution_entry - stop)
        target = execution_entry + distance if side == "BUY" else execution_entry - distance
        signal_key = f"{FIVE_STREAK_STRATEGY}:{snapshot['symbol']}:{candle.get('time')}:{side}"
        signals.append({"proposal_id": hashlib.sha256(signal_key.encode()).hexdigest(),
                        "expires_at": utcnow(), "symbol": snapshot["symbol"], "side": side,
                        "reference_price": execution_entry, "signal_close": signal_close,
                        "quantity": 1.0, "stop_price": stop, "target_price": target,
                        "maximum_loss_usd": 0.0, "score": abs(streak),
                        "strategy": FIVE_STREAK_STRATEGY, "signal_time": candle.get("time"),
                        "model_version": "five-streak-v2-paper"})
    return signals


def five_streak_signals(snapshot: dict) -> list[dict]:
    """Filtered v3 challenger: first streak only, liquid and trend-confirmed."""
    allowed = {value.strip() for value in os.getenv(
        "FOREX_FIVE_STREAK_V3_INSTRUMENTS", "USD_JPY,XAU_USD,XAG_USD").split(",") if value.strip()}
    if snapshot.get("symbol") not in allowed:
        return []
    if snapshot.get("session_liquid") is not True:
        raise MultiAssetRejected("Five-Streak v3 requires a liquid session")
    agreement = float(snapshot.get("horizon_agreement") or 0)
    trend = float(snapshot.get("trend_strength") or 0)
    one = float(snapshot.get("change_1h_pct") or 0)
    if agreement < .75 or abs(trend) < .05 or not .02 <= abs(one) <= .50:
        raise MultiAssetRejected("Five-Streak v3 trend, agreement, or 1h anti-chop/anti-chase gate failed")
    candles = list(snapshot.get("five_streak_candles") or [])
    baseline = five_streak_baseline_signals(snapshot)
    if not baseline:
        return []
    proposal = baseline[-1]
    closed_at = signal_close_time(proposal.get("signal_time"))
    evaluation_latency = ((datetime.now(UTC) - closed_at).total_seconds()
                          if closed_at else float("inf"))
    if evaluation_latency < -5 or evaluation_latency > 300:
        raise MultiAssetRejected("Five-Streak v3 signal is stale by more than one completed M5 candle")
    # Extended streak entries created serially correlated losses in the first
    # sample. V3 acts only on the first candle that completes a five-run.
    streak = 0
    for index, candle in enumerate(candles):
        if index == 0: continue
        opened, closed, prior = float(candle["open"]), float(candle["close"]), float(candles[index - 1]["close"])
        green, red = closed > opened and closed > prior, closed < opened and closed < prior
        streak = (streak + 1 if streak > 0 else 1) if green else (streak - 1 if streak < 0 else -1) if red else 0
    if abs(streak) != 5:
        return []
    side = proposal["side"]
    if (side == "BUY") != (trend > 0) or (side == "BUY") != (one > 0):
        raise MultiAssetRejected("Five-Streak v3 direction contradicts trend or 1h move")
    spread_bps = (float(snapshot.get("ask") or 0) - float(snapshot.get("bid") or 0)) / max(float(snapshot.get("price") or proposal["reference_price"]), 1e-12) * 10_000
    if spread_bps > float(os.getenv("FOREX_FIVE_STREAK_V3_MAX_SPREAD_BPS", "3")):
        raise MultiAssetRejected("Five-Streak v3 executable spread too wide")
    risk = abs(proposal["reference_price"] - proposal["stop_price"])
    signal_close = float(proposal.get("signal_close") or proposal["reference_price"])
    adverse_drift_r = ((proposal["reference_price"] - signal_close) / risk
                       if side == "BUY" else (signal_close - proposal["reference_price"]) / risk)
    if adverse_drift_r > float(os.getenv("FOREX_FIVE_STREAK_V3_MAX_ENTRY_DRIFT_R", ".20")):
        raise MultiAssetRejected("Five-Streak v3 executable entry chased more than 0.20R beyond signal close")
    atr = float(snapshot.get("atr_14") or 0)
    proposal.update({
        "proposal_id": hashlib.sha256(proposal["proposal_id"].encode()).hexdigest(),
        "target_price": proposal["reference_price"] + (1.5 * risk if side == "BUY" else -1.5 * risk),
        "strategy": FIVE_STREAK_FILTERED_STRATEGY,
        "model_version": "five-streak-filtered-v3-paper",
        "signal_closed_at": closed_at.isoformat() if closed_at else None,
        "evaluation_latency_seconds": round(evaluation_latency, 3),
        "entry_spread_bps": round(spread_bps, 4),
        "entry_slippage_bps": round((proposal["reference_price"] - signal_close) / signal_close *
                                     (1 if side == "BUY" else -1) * 10_000, 4),
        "experiment": {"fixed_stop_distance": risk, "atr_14": atr,
                       "volatility_stop_1_5atr": 1.5 * atr if atr > 0 else None,
                       "profit_protection_ladder": {"0.50R": "0.20R", "0.75R": "0.50R",
                                                    "1.00R": "0.75R"}},
        "entry_reason": (f"Filtered V4 Ratchet: first five-candle {side} streak; agreement={agreement:.2f}, "
                         f"trend={trend:.4f}, 1h={one:.4f}%, spread={spread_bps:.2f}bps; target=1.5R."),
    })
    return [proposal]


def bryne_liquidity_signals(snapshot: dict) -> list[dict]:
    """Range/sweep/order-block implementation from Bryne's follow-up lessons.

    H1 establishes the range and structure. The latest completed H1 candle must
    retest the last opposing candle after a sweep and confirmed structure break.
    This deliberately avoids predicting a breakout or entering during the sweep.
    """
    if snapshot.get("session_liquid") is not True or snapshot.get("tradable") is not True:
        raise MultiAssetRejected("Bryne V5 requires a live liquid session")
    if snapshot.get("market_veto") is True:
        raise MultiAssetRejected("Bryne V5 economic-event veto is active")
    spread = float(snapshot.get("spread_bps") or 999)
    if spread > float(os.getenv("FOREX_BRYNE_V5_MAX_SPREAD_BPS", "3")):
        raise MultiAssetRejected("Bryne V5 executable spread is too wide")
    candles = list(snapshot.get("bryne_h1_candles") or [])
    if len(candles) < 28:
        raise MultiAssetRejected("Bryne V5 needs at least 28 completed H1 candles")
    history, sweep, breakout, retest = candles[-28:-3], candles[-3], candles[-2], candles[-1]
    atr = float(snapshot.get("atr_14") or 0)
    if atr <= 0:
        raise MultiAssetRejected("Bryne V5 ATR evidence is unavailable")
    # Bryne explicitly uses the line/body structure for range levels and the
    # wick beyond that structure as the liquidity sweep.
    body_high = lambda c: max(float(c["open"]), float(c["close"]))
    body_low = lambda c: min(float(c["open"]), float(c["close"]))
    range_high = max(body_high(c) for c in history)
    range_low = min(body_low(c) for c in history)
    width = range_high - range_low
    if width <= 0 or width > float(os.getenv("FOREX_BRYNE_V5_MAX_RANGE_ATR", "6")) * atr:
        raise MultiAssetRejected("Bryne V5 did not identify a bounded H1 trading range")
    touch_tolerance = .25 * atr
    high_touches = sum(body_high(c) >= range_high - touch_tolerance for c in history)
    low_touches = sum(body_low(c) <= range_low + touch_tolerance for c in history)
    if min(high_touches, low_touches) < 2:
        raise MultiAssetRejected("Bryne V5 range lacks two-sided liquidity touches")

    sweep_buffer = .05 * atr
    bullish_sweep = (float(sweep["low"]) < range_low - sweep_buffer
               and float(sweep["close"]) > range_low
               and float(breakout["close"]) > float(sweep["high"]))
    bearish_sweep = (float(sweep["high"]) > range_high + sweep_buffer
               and float(sweep["close"]) < range_high
               and float(breakout["close"]) < float(sweep["low"]))
    recent = candles[-10:-2]
    compression_highs = [body_high(c) for c in recent]
    compression_lows = [body_low(c) for c in recent]
    compressing = (max(compression_highs[-3:]) < max(compression_highs[:3])
                   and min(compression_lows[-3:]) > min(compression_lows[:3]))
    bullish_compression = compressing and float(breakout["close"]) > max(compression_highs)
    bearish_compression = compressing and float(breakout["close"]) < min(compression_lows)
    bullish = bullish_sweep or bullish_compression
    bearish = bearish_sweep or bearish_compression
    if bullish == bearish:
        return []
    side = "BUY" if bullish else "SELL"
    opposing = [c for c in candles[-10:-1]
                if (float(c["close"]) < float(c["open"])) == bullish]
    if not opposing:
        raise MultiAssetRejected("Bryne V5 found no opposing order-block candle before displacement")
    order_block = opposing[-1]
    order_low, order_high = float(order_block["low"]), float(order_block["high"])
    retest_price = float(retest["close"])
    retest_touched = float(retest["low"]) <= order_high and float(retest["high"]) >= order_low
    if not retest_touched or not order_low <= retest_price <= order_high:
        raise MultiAssetRejected("Bryne V5 is waiting for an H1 order-block retest")
    breakout_body = abs(float(breakout["close"]) - float(breakout["open"]))
    if breakout_body < float(os.getenv("FOREX_BRYNE_V5_MIN_DISPLACEMENT_ATR", ".35")) * atr:
        raise MultiAssetRejected("Bryne V5 structure break lacks displacement")
    order_mid = (order_low + order_high) / 2
    # A retest candle may enter the block with an opposing body; what matters
    # is that it closes back away from the invalidation side of the block.
    retest_rejection = ((bullish and retest_price > order_mid) or
                        (bearish and retest_price < order_mid))
    if not retest_rejection:
        raise MultiAssetRejected("Bryne V5 order-block retest lacks directional rejection")
    range_mid = (range_high + range_low) / 2
    if (bullish and retest_price > range_mid) or (bearish and retest_price < range_mid):
        raise MultiAssetRejected("Bryne V5 retest is outside the value half of the range")
    entry = float(snapshot.get("ask") if bullish else snapshot.get("bid") or retest_price)
    stop_buffer = .10 * atr
    invalidation_low = min(float(sweep["low"]), min(compression_lows))
    invalidation_high = max(float(sweep["high"]), max(compression_highs))
    stop = invalidation_low - stop_buffer if bullish else invalidation_high + stop_buffer
    risk = abs(entry - stop)
    if risk <= 0 or (bullish and stop >= entry) or (bearish and stop <= entry):
        raise MultiAssetRejected("Bryne V5 sweep stop is invalid for executable entry")
    adverse_entry_drift_r = ((entry - retest_price) / risk if bullish else
                             (retest_price - entry) / risk)
    if adverse_entry_drift_r > float(os.getenv("FOREX_BRYNE_V5_MAX_ENTRY_DRIFT_R", ".20")):
        raise MultiAssetRejected("Bryne V5 executable entry drifted more than 0.20R beyond retest")
    target = entry + 2 * risk if bullish else entry - 2 * risk
    signal_time = str(retest.get("time") or "")
    key = f"{BRYNE_LIQUIDITY_V5_STRATEGY}:{snapshot['symbol']}:{signal_time}:{side}"
    return [{"proposal_id": hashlib.sha256(key.encode()).hexdigest(), "expires_at": utcnow(),
             "symbol": snapshot["symbol"], "side": side, "reference_price": entry,
             "quantity": 1.0, "stop_price": stop, "target_price": target,
             "maximum_loss_usd": 0.0, "score": 1.0, "strategy": BRYNE_LIQUIDITY_V5_STRATEGY,
             "signal_time": signal_time, "model_version": "bryne-liquidity-range-v5-paper",
             "entry_spread_bps": round(spread, 4),
             "experiment": {"timeframe": "H1", "setup_type": ("LIQUIDITY_SWEEP_REVERSAL"
                            if bullish_sweep or bearish_sweep else "COMPRESSION_BREAKOUT_RETEST"),
                            "range_basis": "CANDLE_BODIES", "range_high": range_high, "range_low": range_low,
                            "range_mid": range_mid, "sweep_high": float(sweep["high"]),
                            "sweep_low": float(sweep["low"]), "order_block_low": order_low,
                            "order_block_high": order_high, "reward_multiple": 2.0},
             "entry_drift_r": round(adverse_entry_drift_r, 4),
             "entry_reason": (f"Bryne V5 {side}: H1 range with {low_touches} low/{high_touches} high touches; "
                              f"{'liquidity sweep reversal' if bullish_sweep or bearish_sweep else 'compression breakout'}, "
                              f"structure confirmation and last-opposing-candle order-block retest in value zone; "
                              f"stop beyond sweep; target=2R; spread={spread:.2f}bps.")}]


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
              type TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL,
              record_hash TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              symbol TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL, quantity REAL NOT NULL,
              stop_price REAL NOT NULL, target_price REAL NOT NULL, maximum_loss_usd REAL NOT NULL,
              mode TEXT NOT NULL, status TEXT NOT NULL, broker_order_id TEXT, broker_trade_id TEXT,
              realized_pnl_usd REAL, score REAL, model_version TEXT, closed_at TEXT,
              max_favorable_pnl_usd REAL DEFAULT 0, max_adverse_pnl_usd REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_checkpoints(
              intent_id TEXT NOT NULL, checkpoint_minutes INTEGER NOT NULL,
              observed_at TEXT NOT NULL, executable_price REAL NOT NULL,
              pnl_usd REAL NOT NULL, source_observed_at TEXT,
              source_url TEXT, status TEXT NOT NULL,
              PRIMARY KEY(intent_id,checkpoint_minutes));
            CREATE TABLE IF NOT EXISTS live_trade_checkpoints(
              trade_id TEXT NOT NULL, instrument TEXT NOT NULL,
              checkpoint_minutes INTEGER NOT NULL, observed_at TEXT NOT NULL,
              executable_price REAL NOT NULL, pnl_usd REAL NOT NULL,
              spread_bps REAL, financing_usd REAL, source_observed_at TEXT,
              source_url TEXT, status TEXT NOT NULL,
              PRIMARY KEY(trade_id,checkpoint_minutes));
            CREATE TABLE IF NOT EXISTS shadow_exit_observations(
              position_id TEXT NOT NULL, experiment TEXT NOT NULL,
              observed_at TEXT NOT NULL, strategy TEXT, instrument TEXT,
              executable_price REAL, pnl_usd REAL, elapsed_minutes REAL,
              evidence_json TEXT NOT NULL,
              PRIMARY KEY(position_id,experiment));
            """)
            columns = {str(row[1]) for row in self.db.execute("PRAGMA table_info(intents)")}
            for name, kind in (("score", "REAL"), ("model_version", "TEXT"), ("closed_at", "TEXT"),
                               ("strategy", "TEXT"), ("signal_time", "TEXT"), ("close_reason", "TEXT"),
                               ("close_price", "REAL"),
                               ("entry_reason", "TEXT"),
                               ("signal_closed_at", "TEXT"),
                               ("evaluation_latency_seconds", "REAL"),
                               ("entry_spread_bps", "REAL"),
                               ("entry_slippage_bps", "REAL"),
                               ("experiment_json", "TEXT"),
                               ("max_favorable_at", "TEXT"),
                               ("max_adverse_at", "TEXT"),
                               ("max_favorable_pnl_usd", "REAL DEFAULT 0"),
                               ("max_adverse_pnl_usd", "REAL DEFAULT 0")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE intents ADD COLUMN {name} {kind}")
            checkpoint_columns = {str(row[1]) for row in self.db.execute(
                "PRAGMA table_info(trade_checkpoints)")}
            for name, kind in (("capture_delay_seconds", "REAL"), ("checkpoint_quality", "TEXT")):
                if name not in checkpoint_columns:
                    self.db.execute(f"ALTER TABLE trade_checkpoints ADD COLUMN {name} {kind}")

    def event(self, kind: str, payload: dict) -> None:
        with self.lock, self.db:
            row = self.db.execute("SELECT record_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous = row[0] if row else "GENESIS"
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            stamp = utcnow()
            digest = hashlib.sha256(f"{previous}|{stamp}|{kind}|{body}".encode()).hexdigest()
            self.db.execute("INSERT INTO events(recorded_at,type,payload,previous_hash,record_hash) VALUES(?,?,?,?,?)",
                            (stamp, kind, body, previous, digest))

    def setting(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def open_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY)).fetchone()[0])

    def open_risk(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(maximum_loss_usd),0) FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY)).fetchone()
        return float(row[0] or 0)

    def open_symbols(self) -> list[str]:
        return [str(row[0]) for row in self.db.execute("SELECT symbol FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY)).fetchall()]

    def has_intent(self, intent_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM intents WHERE id=?", (intent_id,)).fetchone() is not None

    def symbol_in_cooldown(self, symbol: str, seconds: int) -> bool:
        row = self.db.execute("SELECT created_at FROM intents WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (symbol,)).fetchone()
        if not row: return False
        created = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        return (datetime.now(UTC) - created).total_seconds() < seconds

    def add_intent(self, proposal: dict, mode: str, status: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO intents(id,created_at,expires_at,symbol,side,entry_price,quantity,stop_price,target_price,
              maximum_loss_usd,mode,status,score,model_version,strategy,signal_time,entry_reason,
              signal_closed_at,evaluation_latency_seconds,entry_spread_bps,entry_slippage_bps,experiment_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (proposal["proposal_id"], utcnow(), proposal["expires_at"], proposal["symbol"], proposal["side"], proposal["reference_price"],
               proposal["quantity"], proposal["stop_price"], proposal["target_price"], proposal["maximum_loss_usd"], mode, status,
               proposal.get("score"), proposal.get("model_version", FOREX_MODEL_VERSION),
               proposal.get("strategy", "FOREX_CONTROL"), proposal.get("signal_time"),
               proposal.get("entry_reason"), proposal.get("signal_closed_at"),
               proposal.get("evaluation_latency_seconds"), proposal.get("entry_spread_bps"),
               proposal.get("entry_slippage_bps"), json.dumps(proposal.get("experiment") or {})))

    def record_checkpoint(self, intent: dict, checkpoint: int, snapshot: dict, price: float,
                          capture_delay_seconds: float = 0) -> bool:
        pnl = five_streak_position_pnl(intent, price)
        source_urls = snapshot.get("source_urls") or []
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO trade_checkpoints
                (intent_id,checkpoint_minutes,observed_at,executable_price,pnl_usd,
                 source_observed_at,source_url,status,capture_delay_seconds,checkpoint_quality)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (intent["id"], checkpoint, utcnow(), price, pnl,
                snapshot.get("observed_at"), source_urls[0] if source_urls else None,
                intent.get("status") or "UNKNOWN", max(0.0, capture_delay_seconds),
                "ON_TIME" if capture_delay_seconds <= 120 else "FIRST_AVAILABLE_AFTER_HORIZON"))
        return cursor.rowcount == 1

    def record_shadow_exit(self, position_id: str, strategy: str, instrument: str,
                           price: float, pnl: float, evidence: dict) -> bool:
        if not evidence.get("would_exit_now"):
            return False
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO shadow_exit_observations
                VALUES(?,?,?,?,?,?,?,?,?)""", (position_id, evidence["experiment"], utcnow(),
                strategy, instrument, price, pnl, evidence.get("elapsed_minutes"),
                json.dumps(evidence, sort_keys=True)))
        return cursor.rowcount == 1

    def shadow_exit_observations(self, limit: int = 250) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM shadow_exit_observations ORDER BY observed_at DESC LIMIT ?", (limit,))]

    def trade_checkpoints(self, limit: int = 250) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM trade_checkpoints ORDER BY observed_at DESC LIMIT ?", (limit,)).fetchall()]

    def record_live_checkpoint(self, trade: dict, checkpoint: int) -> bool:
        price = float(trade.get("current_price") or 0)
        if not trade.get("trade_id") or not trade.get("instrument") or price <= 0:
            return False
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO live_trade_checkpoints
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                str(trade["trade_id"]), str(trade["instrument"]), checkpoint, utcnow(), price,
                float(trade.get("unrealized_pnl_usd") or 0), trade.get("current_spread_bps"),
                trade.get("financing_usd"), trade.get("current_price_observed_at"),
                "https://developer.oanda.com/rest-live-v20/pricing-ep/", "OPEN"))
        return cursor.rowcount == 1

    def live_trade_checkpoints(self, limit: int = 250) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM live_trade_checkpoints ORDER BY observed_at DESC LIMIT ?", (limit,)).fetchall()]

    def strategy_stats(self, strategy: str) -> dict:
        rows = [dict(row) for row in self.db.execute(
            "SELECT status,realized_pnl_usd FROM intents WHERE strategy=?", (strategy,)).fetchall()]
        closed = [float(row["realized_pnl_usd"]) for row in rows if row["realized_pnl_usd"] is not None]
        wins = sum(value > 0 for value in closed)
        return {"strategy": strategy, "opened": len(rows), "open": sum(row["status"] == "PAPER_OPEN" for row in rows),
                "closed": len(closed), "wins": wins, "losses": len(closed) - wins,
                "win_rate": wins / len(closed) if closed else None, "net_pnl_usd": round(sum(closed), 8),
                "expectancy_usd": sum(closed) / len(closed) if closed else None}

    def strategy_intents(self, strategy: str, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,created_at,signal_time,closed_at,symbol,side,status,entry_price,stop_price,target_price,"
            "maximum_loss_usd,realized_pnl_usd,close_reason,close_price,entry_reason,max_favorable_pnl_usd,max_adverse_pnl_usd FROM intents WHERE strategy=? "
            "ORDER BY created_at DESC LIMIT ?", (strategy, limit)).fetchall()]

    def update_intent(self, intent_id: str, status: str, order_id: str = "", trade_id: str = "") -> None:
        with self.db:
            self.db.execute("UPDATE intents SET status=?,broker_order_id=COALESCE(NULLIF(?,''),broker_order_id),broker_trade_id=COALESCE(NULLIF(?,''),broker_trade_id) WHERE id=?",
                            (status, order_id, trade_id, intent_id))

    def paper_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status='PAPER_OPEN'").fetchall()]

    def broker_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status IN ('SUBMITTING','SUBMITTED','OPEN')").fetchall()]

    def intent_for_broker_client_id(self, client_id: str) -> dict | None:
        for row in self.broker_positions():
            if broker_client_id(str(row["id"])) == str(client_id):
                return row
        return None

    def recent_intents(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def recent_events(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT seq,recorded_at,type,record_hash FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()]

    def close_broker_intent(self, trade_id: str, realized_pnl_usd: float | None, close_reason: str = "BROKER_CLOSE") -> bool:
        with self.db:
            cursor = self.db.execute("""UPDATE intents SET status='BROKER_CLOSED', realized_pnl_usd=?, closed_at=?,close_reason=?
                              WHERE broker_trade_id=? AND status='OPEN'""",
                            (realized_pnl_usd, utcnow(), close_reason, trade_id))
        return cursor.rowcount == 1

    def import_closed_broker_intent(self, outcome: dict) -> bool:
        """Backfill one tagged OANDA trade that closed before this ledger saw it."""
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO intents(
                id,created_at,expires_at,symbol,side,entry_price,quantity,stop_price,target_price,
                maximum_loss_usd,mode,status,broker_order_id,broker_trade_id,realized_pnl_usd,
                score,model_version,closed_at,strategy,close_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (outcome["proposal_id"], outcome["created_at"], outcome["created_at"],
                 outcome["symbol"], outcome["side"], outcome["entry_price"], outcome["quantity"],
                 outcome["stop_price"], outcome["target_price"], outcome["maximum_loss_usd"],
                 outcome["mode"], "BROKER_CLOSED", outcome["broker_order_id"],
                 outcome["broker_trade_id"], outcome["realized_pnl_usd"], None,
                 FOREX_MODEL_VERSION, outcome["closed_at"], "FOREX_CONTROL", "BROKER_HISTORY"))
        return cursor.rowcount == 1

    def realized_pnl(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) FROM intents WHERE realized_pnl_usd IS NOT NULL").fetchone()
        return float(row[0])

    def update_excursions(self, trades: list[dict]) -> None:
        with self.db:
            for trade in trades:
                trade_id = str(trade.get("id") or "")
                if not trade_id:
                    continue
                current = float(trade.get("unrealizedPL") or 0) + float(trade.get("financing") or 0)
                self.db.execute("""UPDATE intents SET
                    max_favorable_pnl_usd=MAX(COALESCE(max_favorable_pnl_usd,0),?),
                    max_adverse_pnl_usd=MIN(COALESCE(max_adverse_pnl_usd,0),?)
                    WHERE broker_trade_id=? AND status='OPEN'""", (current, current, trade_id))

    def model_review(self, minimum_score: float) -> dict:
        rows = [dict(row) for row in self.db.execute("""SELECT score,realized_pnl_usd,symbol,side,closed_at,
                    max_favorable_pnl_usd,max_adverse_pnl_usd
                    FROM intents WHERE status IN ('BROKER_CLOSED','PAPER_CLOSED') AND COALESCE(strategy,'') NOT IN (?,?)
                    AND realized_pnl_usd IS NOT NULL ORDER BY closed_at""", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchall()]
        pnls = [float(row["realized_pnl_usd"]) for row in rows]
        wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
        sample = len(pnls)
        favorable = [float(row.get("max_favorable_pnl_usd") or 0) for row in rows]
        adverse = [float(row.get("max_adverse_pnl_usd") or 0) for row in rows]
        captured = [float(row["realized_pnl_usd"]) / float(row["max_favorable_pnl_usd"])
                    for row in rows if float(row.get("max_favorable_pnl_usd") or 0) > 0]
        by_symbol = {}
        for row in rows:
            bucket = by_symbol.setdefault(str(row["symbol"]), {"sample_size": 0, "net_pnl_usd": 0.0, "wins": 0})
            bucket["sample_size"] += 1
            bucket["net_pnl_usd"] = round(bucket["net_pnl_usd"] + float(row["realized_pnl_usd"]), 8)
            bucket["wins"] += int(float(row["realized_pnl_usd"]) > 0)
        gate = promotion_gate(pnls, minimum_samples=100, cost_stress=.05)
        review = {
            "model_version": FOREX_MODEL_VERSION, "sample_size": sample,
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / sample if sample else None,
            "net_pnl_usd": round(sum(pnls), 8),
            "net_expectancy_usd": sum(pnls) / sample if sample else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "average_win_usd": sum(wins) / len(wins) if wins else None,
            "average_loss_usd": sum(losses) / len(losses) if losses else None,
            "average_max_favorable_excursion_usd": sum(favorable) / sample if sample else None,
            "average_max_adverse_excursion_usd": sum(adverse) / sample if sample else None,
            "average_profit_capture": sum(captured) / len(captured) if captured else None,
            "by_symbol": by_symbol,
            "champion_minimum_score": minimum_score,
            "status": "ELIGIBLE FOR PROSPECTIVE CHALLENGER REVIEW" if gate.eligible else "MODEL LOCKED - COLLECTING EVIDENCE",
            "parameters_changed": False,
            "promotion_gate": {"eligible": gate.eligible, "reasons": list(gate.reasons),
                               "lower_confidence_bound": gate.lower_confidence_bound,
                               "cost_stressed_expectancy": gate.stressed_mean_return},
            "promotion_rule": "At least 100 closed trades, a positive 95% lower confidence bound, positive cost-stressed expectancy, and prospective challenger performance without worse drawdown",
        }
        last_sample = int(self.setting("model_review_sample_size", "-1"))
        if sample != last_sample:
            self.set_setting("model_review_sample_size", str(sample))
            self.event("MODEL_REVIEW", review)
        return review


def closed_trade_details(transactions: list[dict]) -> dict[str, dict]:
    """Return OANDA trade IDs closed/reduced by recent fill transactions."""
    values: dict[str, dict] = {}
    for transaction in transactions:
        if str(transaction.get("type", "")).upper() != "ORDER_FILL":
            continue
        legs = list(transaction.get("tradesClosed") or [])
        if transaction.get("tradeReduced"):
            legs.append(transaction["tradeReduced"])
        for leg in legs:
            trade_id = str(leg.get("tradeID") or "")
            if not trade_id:
                continue
            net = (float(leg.get("realizedPL") or 0) + float(leg.get("financing") or 0)
                   + float(leg.get("dividendAdjustment") or 0)
                   - abs(float(leg.get("guaranteedExecutionFee") or 0)))
            detail = values.get(trade_id, {"pnl": 0.0, "reason": ""})
            detail["pnl"] += net
            detail["reason"] = str(transaction.get("reason") or transaction.get("type") or "BROKER_CLOSE")
            values[trade_id] = detail
    return values


def closed_trade_pnl(transactions: list[dict]) -> dict[str, float]:
    return {trade_id: float(detail["pnl"]) for trade_id, detail in closed_trade_details(transactions).items()}


def normalize_open_trade(trade: dict, snapshot: dict | None = None) -> dict:
    """Expose the broker facts needed to independently audit every live position."""
    units = float(trade.get("currentUnits") or trade.get("initialUnits") or 0)
    stop = trade.get("stopLossOrder") or {}
    target = trade.get("takeProfitOrder") or {}
    snapshot = snapshot or {}
    executable = snapshot.get("bid") if units > 0 else snapshot.get("ask") if units < 0 else None
    return {
        "trade_id": str(trade.get("id") or ""),
        "instrument": str(trade.get("instrument") or ""),
        "side": "BUY" if units > 0 else "SELL" if units < 0 else "UNKNOWN",
        "units": abs(units), "entry_price": float(trade.get("price") or 0),
        "open_time": trade.get("openTime"),
        "current_price": float(executable or trade.get("averageClosePrice") or 0) or None,
        "current_price_observed_at": snapshot.get("observed_at"),
        "current_spread_bps": snapshot.get("spread_bps"),
        "unrealized_pnl_usd": float(trade.get("unrealizedPL") or 0),
        "financing_usd": float(trade.get("financing") or 0),
        "broker_initial_margin_usd": float(trade.get("initialMarginRequired") or 0) or None,
        "stop_order_id": str(stop.get("id") or ""),
        "stop_price": float(stop.get("price") or 0) or None,
        "target_order_id": str(target.get("id") or ""),
        "target_price": float(target.get("price") or 0) or None,
        "client_tag": str((trade.get("clientExtensions") or {}).get("tag") or ""),
    }


def historical_managed_trade_outcomes(transactions: list[dict], mode: str) -> list[dict]:
    """Recover closed trades created by this service from OANDA history.

    Only orders carrying our client-extension tag are eligible. This excludes
    manual or unrelated account activity while repairing model/report samples
    after restarts or earlier ledger gaps.
    """
    orders = {str(item.get("id") or ""): item for item in transactions
              if str(item.get("type") or "").upper() == "MARKET_ORDER"
              and (item.get("clientExtensions") or {}).get("tag") == "primus-forex-v1"
              and (item.get("clientExtensions") or {}).get("id")}
    opened: dict[str, dict] = {}
    closed_at: dict[str, str] = {}
    for item in transactions:
        if str(item.get("type") or "").upper() != "ORDER_FILL":
            continue
        order = orders.get(str(item.get("orderID") or ""))
        leg = item.get("tradeOpened") or {}
        trade_id = str(leg.get("tradeID") or "")
        if order and trade_id:
            units = float(leg.get("units") or item.get("units") or order.get("units") or 0)
            entry = float(item.get("price") or order.get("priceBound") or 0)
            stop = float((order.get("stopLossOnFill") or {}).get("price") or 0)
            target = float((order.get("takeProfitOnFill") or {}).get("price") or 0)
            if units and entry > 0 and stop > 0 and target > 0:
                opened[trade_id] = {
                    "proposal_id": str((order.get("clientExtensions") or {})["id"]),
                    "created_at": str(item.get("time") or order.get("time") or utcnow()),
                    "symbol": str(item.get("instrument") or order.get("instrument") or ""),
                    "side": "BUY" if units > 0 else "SELL", "entry_price": entry,
                    "quantity": abs(units), "stop_price": stop, "target_price": target,
                    "maximum_loss_usd": abs(entry - stop) * abs(units),
                    "mode": mode, "broker_order_id": str(item.get("orderID") or ""),
                    "broker_trade_id": trade_id,
                }
        for closed_leg in list(item.get("tradesClosed") or []) + ([item["tradeReduced"]] if item.get("tradeReduced") else []):
            closed_at[str(closed_leg.get("tradeID") or "")] = str(item.get("time") or utcnow())
    pnl = closed_trade_pnl(transactions)
    return [{**value, "realized_pnl_usd": pnl[trade_id],
             "closed_at": closed_at.get(trade_id, utcnow())}
            for trade_id, value in opened.items() if trade_id in pnl]


def transaction_managed_intent_id(trade_id: str, transactions: list[dict]) -> str:
    fills = [item for item in transactions if str(item.get("type", "")).upper() == "ORDER_FILL"
             and str((item.get("tradeOpened") or {}).get("tradeID") or "") == trade_id]
    for fill in fills:
        order_id = str(fill.get("orderID") or "")
        created = next((item for item in transactions
                        if str(item.get("id") or "") == order_id
                        and str(item.get("type", "")).upper() == "MARKET_ORDER"), {})
        extensions = created.get("clientExtensions") or {}
        if extensions.get("tag") == "primus-forex-v1" and extensions.get("id"):
            return str(extensions["id"])
    return ""


def recoverable_managed_trade(trade: dict, maximum_loss_usd: float, transactions: list[dict] | None = None) -> dict | None:
    """Rebuild a ledger intent only for our tagged, broker-protected trade."""
    extensions = trade.get("clientExtensions") or {}
    intent_id = str(extensions.get("id") or "") if extensions.get("tag") == "primus-forex-v1" else ""
    if not intent_id and transactions:
        intent_id = transaction_managed_intent_id(str(trade.get("id") or ""), transactions)
    if not intent_id:
        return None
    stop_order, target_order = trade.get("stopLossOrder") or {}, trade.get("takeProfitOrder") or {}
    if not stop_order.get("price") or not target_order.get("price"):
        return None
    units = float(trade.get("currentUnits") or 0)
    entry = float(trade.get("price") or 0)
    if not units or entry <= 0:
        return None
    return {
        "proposal_id": intent_id,
        "expires_at": str(trade.get("openTime") or utcnow()),
        "symbol": str(trade.get("instrument") or ""),
        "side": "BUY" if units > 0 else "SELL",
        "reference_price": entry,
        "quantity": abs(units),
        "stop_price": float(stop_order["price"]),
        "target_price": float(target_order["price"]),
        "maximum_loss_usd": maximum_loss_usd,
        "score": None,
        "model_version": FOREX_MODEL_VERSION,
    }


def fetch_json(url: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "primus-forex-executor/1.0"}
    token = os.getenv("MULTI_ASSET_FEED_BEARER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.loads(response.read().decode())


def live_armed(adapter: OandaAdapter) -> bool:
    return all((
        truthy("FOREX_LIVE_ENABLED"),
        os.getenv("FOREX_LIVE_ACK") == "I_ACCEPT_REAL_MONEY_RISK",
        adapter.environment == "live",
        os.getenv("FOREX_ALLOWED_ACCOUNT_ID", "") == adapter.account,
    ))


def practice_armed(adapter: OandaAdapter) -> bool:
    return all((truthy("FOREX_PRACTICE_EXECUTION_ENABLED"), adapter.environment == "practice",
                os.getenv("FOREX_PRACTICE_ACK") == "I_ACCEPT_PRACTICE_ORDER_EXECUTION"))


def safe_quantity(adapter: OandaAdapter, proposal: dict, risk_usd: float,
                  margin_budget_usd: float | None = None,
                  notional_budget_usd: float | None = None) -> float:
    quote = adapter.price(proposal["symbol"])
    if quote.get("status") != "tradeable":
        raise MultiAssetRejected("broker reports instrument non-tradeable")
    observed = datetime.fromisoformat(str(quote["time"]).replace("Z", "+00:00"))
    age = (datetime.now(UTC) - observed).total_seconds()
    if age < -5 or age > 10:
        raise MultiAssetRejected("broker quote stale")
    factor = float(quote.get("quoteHomeConversionFactors", {}).get("negativeUnits") or 0)
    bids, asks = quote.get("bids", []), quote.get("asks", [])
    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2 if bids and asks else float(proposal["reference_price"])
    distance = abs(float(proposal["reference_price"]) - float(proposal["stop_price"]))
    if factor <= 0 or distance <= 0:
        raise MultiAssetRejected("risk conversion unavailable")
    units = int(risk_usd / (distance * factor))
    metadata = adapter.instrument(proposal["symbol"])
    margin_rate = float(metadata.get("marginRate") or 1)
    unit_notional_home = mid * factor
    notional_cap = max(0.0, float(notional_budget_usd if notional_budget_usd is not None else
                                    os.getenv("FOREX_MAX_NOTIONAL_USD", "50")))
    margin_cap = max(0.0, float(margin_budget_usd if margin_budget_usd is not None else
                                  os.getenv("FOREX_MAX_MARGIN_USED_USD", "5")))
    absolute_notional = max(0.0, float(os.getenv("FOREX_ABSOLUTE_MAX_NOTIONAL_USD", "0") or 0))
    absolute_margin = max(0.0, float(os.getenv("FOREX_ABSOLUTE_MAX_MARGIN_USED_USD", "0") or 0))
    if absolute_notional:
        notional_cap = min(notional_cap, absolute_notional)
    if absolute_margin:
        margin_cap = min(margin_cap, absolute_margin)
    units = min(units, int(notional_cap / unit_notional_home), int(margin_cap / (unit_notional_home * margin_rate)))
    if units < 1:
        raise MultiAssetRejected("risk cap cannot support minimum unit")
    return float(units)


def confirmed_trade_actions(transactions: list[dict], summary: dict, open_trades: list[dict],
                            pending_orders: list[dict], risk: dict, snapshots: list[dict],
                            intents: list[dict], outcomes: list[dict]) -> list[dict]:
    """Convert confirmed broker fills into sanitized, explanatory email payloads."""
    actions = []
    intent_by_trade = {str(item.get("broker_trade_id")): item for item in intents
                       if item.get("broker_trade_id")}
    outcome_by_symbol = {str(item.get("symbol")): item for item in outcomes}
    calendar_verified = all(item.get("calendar_verified") is True for item in snapshots) if snapshots else False
    blackout_symbols = {
        str(item.get("symbol") or item.get("instrument") or "")
        for item in snapshots
        if int(item.get("economic_event_within_minutes") or 0) > 0
    }

    def base(tx: dict, pair: str, action_id: str) -> dict:
        outcome = outcome_by_symbol.get(pair, {})
        pair_blackout = pair in blackout_symbols
        score = outcome.get("score")
        signal = (
            f"Signal score {score:.2f} versus "
            f"{outcome.get('minimum_score', risk.get('minimum_score'))}; "
            f"{outcome.get('alignment', 'broker fill confirmation')}"
        ) if isinstance(score, (int, float)) else (
            "Confirmed broker fill; original signal details are retained in the trade ledger."
        )
        return {
            "action_id": action_id,
            "pair": pair,
            "execution_time": tx.get("time"),
            "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"),
            "cumulative_realized_pnl_usd": summary.get("pl"),
            "daily_pnl_usd": (float(summary.get("nav") or 0) -
                              float(risk.get("daily_baseline_nav") or summary.get("nav") or 0)),
            "nav": summary.get("nav"),
            "margin_used": summary.get("margin_used"),
            "margin_available": summary.get("margin_available"),
            "remaining_positions": open_trades,
            "pending_protective_orders": pending_orders,
            "signal_trigger": signal,
            "calendar_state": (
                f"Verified: {'yes' if calendar_verified else 'no'}; "
                f"blackout for {pair or 'this pair'}: {'yes' if pair_blackout else 'no'}; "
                f"other scanned pairs with blackout: "
                f"{', '.join(sorted(blackout_symbols - {pair})) or 'none'}"
            ),
            "executor_state": "LIVE_ARMED and ready; broker fill confirmed",
            "risk_summary": (
                f"{float(risk.get('risk_per_trade_pct') or 0) * 100:.2f}% NAV per trade; "
                f"{float(risk.get('combined_risk_pct') or 0) * 100:.2f}% combined risk; "
                f"{risk.get('maximum_open_positions')} maximum positions; "
                f"{float(risk.get('drawdown_pct') or 0) * 100:.2f}% drawdown"
            ),
            "warnings": ([outcome.get("reason")] if outcome.get("reason") else []) +
                        ([f"Economic-calendar blackout applies to {pair}."]
                         if pair_blackout else []),
        }

    for tx in transactions:
        if str(tx.get("type")) != "ORDER_FILL":
            continue
        tx_id = str(tx.get("id") or "")
        pair = str(tx.get("instrument") or "")
        reason = str(tx.get("reason") or "MARKET_ORDER")
        opened = tx.get("tradeOpened") or {}
        if opened:
            trade_id = str(opened.get("tradeID") or "")
            intent = intent_by_trade.get(trade_id, {})
            units = float(opened.get("units") or tx.get("units") or intent.get("quantity") or 0)
            side = "BUY" if units > 0 else "SELL"
            action = base(tx, pair or str(intent.get("symbol") or ""), f"{tx_id}:open:{trade_id}")
            action.update({
                "email_action": side,
                "action": "New position opened",
                "side": side,
                "filled_quantity": abs(units),
                "execution_price": opened.get("price") or tx.get("price") or intent.get("entry_price"),
                "realized_pnl_usd": float(tx.get("pl") or 0),
                "entry_reason": intent.get("entry_reason") or action["signal_trigger"],
                "trigger": (
                    f"A broker-confirmed {side} fill opened a new {action['pair'].replace('_', '/')} "
                    f"position. The order passed the strategy score, liquidity, spread, session, "
                    f"calendar, risk-budget, correlation, and protection checks."
                ),
                "position_impact": (
                    f"Exposure increased by {abs(units):g} units. "
                    f"{len(open_trades)} position(s) are now open, with protective stop-loss and "
                    f"take-profit orders listed below."
                ),
            })
            actions.append(action)
        closed_items = list(tx.get("tradesClosed") or [])
        if tx.get("tradeReduced"):
            closed_items.append(tx["tradeReduced"])
        for item in closed_items:
            trade_id = str(item.get("tradeID") or "")
            intent = intent_by_trade.get(trade_id, {})
            closed_pair = pair or str(intent.get("symbol") or "")
            units = abs(float(item.get("units") or 0))
            realized = (float(item.get("realizedPL") or 0) + float(item.get("financing") or 0) +
                        float(item.get("dividendAdjustment") or 0))
            trigger_names = {
                "STOP_LOSS_ORDER": "The protective stop-loss was executed because price reached the predefined loss boundary.",
                "TAKE_PROFIT_ORDER": "The protective take-profit was executed because price reached the planned profit target.",
                "MARKET_ORDER_TRADE_CLOSE": "The position was closed by a confirmed market close instruction.",
            }
            trigger = trigger_names.get(
                reason, f"A broker-confirmed fill reduced or closed the position ({reason.replace('_', ' ').lower()}).")
            action = base(tx, closed_pair, f"{tx_id}:close:{trade_id}")
            action.update({
                "email_action": "CLOSED",
                "action": "Partial position close" if tx.get("tradeReduced") and not tx.get("tradesClosed")
                          else "Position closed",
                "side": "CLOSE",
                "filled_quantity": units,
                "execution_price": item.get("price") or tx.get("price"),
                "realized_pnl_usd": realized,
                "entry_reason": intent.get("entry_reason") or "Historical broker trade: the original entry rationale was not stored.",
                "exit_reason": trigger,
                "trigger": trigger,
                "position_impact": (
                    f"Exposure decreased by {units:g} units. Realized result from this fill was "
                    f"{'a gain' if realized >= 0 else 'a loss'} of ${abs(realized):.4f}. "
                    f"{len(open_trades)} position(s) remain open."
                ),
            })
            actions.append(action)
    return actions


def five_streak_email_actions(outcomes: list[dict], closes: list[dict], intents: list[dict],
                              summary: dict) -> list[dict]:
    """Make Bryne and Lot-Bill paper fills first-class, idempotent email actions."""
    by_id = {str(item.get("id") or ""): item for item in intents}
    actions = []
    for item in outcomes:
        if item.get("status") != "PAPER_FILL":
            continue
        intent = by_id.get(str(item.get("intent_id") or ""), {})
        side = str(item.get("side") or intent.get("side") or "")
        reason = str(item.get("entry_reason") or intent.get("entry_reason") or
                     "Five qualifying closed M5 candles triggered the paper entry.")
        actions.append({
            "action_id": f"bryne:paper:open:{item.get('intent_id')}",
            "strategy_name": FIVE_STREAK_DISPLAY_NAME,
            "email_action": f"PAPER {side}", "action": "New Bryne and Lot-Bill paper position opened",
            "pair": item.get("symbol"), "execution_time": item.get("signal_time"),
            "signal_time": item.get("signal_time") or intent.get("signal_time"), "side": side,
            "status": "PAPER_OPEN", "entry_price": item.get("entry") or intent.get("entry_price"),
            "stop_price": intent.get("stop_price"), "target_price": intent.get("target_price"),
            "maximum_loss_usd": intent.get("maximum_loss_usd"),
            "paper_allocation_usd": intent.get("maximum_loss_usd"),
            "filled_quantity": intent.get("quantity"), "execution_price": item.get("entry"),
            "realized_pnl_usd": 0, "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"),
            "nav": summary.get("nav"), "margin_used": summary.get("margin_used"),
            "margin_available": summary.get("margin_available"), "remaining_positions": [],
            "trigger": reason, "entry_reason": reason, "signal_trigger": reason,
            "position_impact": "Paper-only experiment; no broker funds or margin were used.",
            "calendar_state": "Paper experiment; market feed calendar evidence retained.",
            "executor_state": "PAPER ONLY", "risk_summary": "Bryne V5 paper risk cap; sweep stop and 2R target.",
            "warnings": ["Bryne and Lot-Bill Strategy is paper-only."],
        })
    for item in closes:
        if item.get("strategy") not in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY}:
            continue
        intent = by_id.get(str(item.get("intent_id") or ""), {})
        close_price = item.get("fill_price")
        price_detail = (f" at {close_price}" if close_price not in (None, "")
                        else "; the historical close price was not retained")
        exit_reason = f"Paper exit: {item.get('reason')}{price_detail}."
        actions.append({
            "action_id": f"bryne:paper:close:{item.get('intent_id')}:{item.get('reason')}",
            "strategy_name": FIVE_STREAK_DISPLAY_NAME,
            "email_action": "PAPER CLOSED", "action": "Bryne and Lot-Bill paper position closed",
            "pair": item.get("symbol"), "execution_time": item.get("closed_at") or utcnow(),
            "signal_time": intent.get("signal_time"), "side": intent.get("side"),
            "status": "PAPER_CLOSED", "entry_price": intent.get("entry_price"),
            "stop_price": intent.get("stop_price"), "target_price": intent.get("target_price"),
            "maximum_loss_usd": intent.get("maximum_loss_usd"), "close_reason": item.get("reason"),
            "paper_allocation_usd": intent.get("maximum_loss_usd"),
            "filled_quantity": intent.get("quantity"), "execution_price": item.get("fill_price"),
            "realized_pnl_usd": item.get("realized_pnl_usd"),
            "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"), "nav": summary.get("nav"),
            "margin_used": summary.get("margin_used"), "margin_available": summary.get("margin_available"),
            "remaining_positions": [], "trigger": exit_reason,
            "entry_reason": intent.get("entry_reason") or "Historical paper entry reason was not stored.",
            "exit_reason": exit_reason, "signal_trigger": intent.get("entry_reason"),
            "position_impact": "Paper position closed; no broker funds or margin were used.",
            "calendar_state": "Paper experiment", "executor_state": "PAPER ONLY",
            "risk_summary": "Paper risk cap applied.",
            "warnings": ["Bryne and Lot-Bill Strategy is paper-only."],
        })
    return actions


class Executor:
    def __init__(self):
        self.adapter = OandaAdapter()
        path = os.getenv("FOREX_LEDGER_PATH", "/app/data/forex.sqlite3")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.ledger = Ledger(path)
        base_policy = AssetPolicy.from_env()
        self.engine = ForexEngine(replace(
            base_policy, minimum_score=float(os.getenv("FOREX_MIN_SCORE", "80"))))
        self.base_risk_pct = min(0.01, max(0.001, float(os.getenv("FOREX_RISK_PER_TRADE_PCT", "0.01"))))
        self.emailer = ForexReportEmailer(self.ledger)

    @staticmethod
    def max_positions() -> int:
        # Deliberate hard ceiling: NAV compounding may scale size, never concurrency beyond two.
        return min(2, max(1, int(os.getenv("FOREX_MAX_OPEN_POSITIONS", "2"))))

    @staticmethod
    def currencies(symbol: str) -> set[str]:
        return {part for part in str(symbol).upper().split("_") if part}

    def risk_limits(self, nav: float) -> dict:
        nav = max(0.0, float(nav))
        stored_peak_value = self.ledger.setting("peak_nav", "")
        stored_peak = float(stored_peak_value or nav)
        peak_nav = max(nav, stored_peak)
        if not stored_peak_value or peak_nav > stored_peak:
            self.ledger.set_setting("peak_nav", str(peak_nav))
        drawdown_pct = 0.0 if peak_nav <= 0 else max(0.0, (peak_nav - nav) / peak_nav)
        risk_pct, positions = self.base_risk_pct, self.max_positions()
        if drawdown_pct >= 0.10:
            risk_pct, positions = 0.0, 0
        elif drawdown_pct >= 0.07:
            risk_pct, positions = min(risk_pct, 0.0025), 1
        elif drawdown_pct >= 0.04:
            risk_pct = min(risk_pct, 0.005)
        return {
            "nav": nav,
            "peak_nav": peak_nav,
            "drawdown_pct": drawdown_pct,
            "risk_per_trade_pct": risk_pct,
            "risk_per_trade_usd": nav * risk_pct,
            "maximum_open_positions": positions,
            "combined_risk_pct": 0.02,
            "combined_risk_usd": nav * 0.02,
            "daily_loss_pct": 0.03,
            "daily_loss_usd": nav * 0.03,
            "weekly_loss_pct": 0.06,
            "weekly_loss_usd": nav * 0.06,
            "single_notional_pct": 1.0,
            "single_notional_usd": nav,
            "combined_notional_pct": 1.75,
            "combined_notional_usd": nav * 1.75,
            "maximum_margin_used_usd": nav * 0.10,
            "new_entries_halted": drawdown_pct >= 0.10,
        }

    def open_notional_home(self, trades: list[dict]) -> float:
        total = 0.0
        for trade in trades:
            symbol = str(trade.get("instrument") or "")
            units = abs(float(trade.get("currentUnits") or trade.get("initialUnits") or 0))
            if not symbol or units <= 0:
                continue
            quote = self.adapter.price(symbol)
            bids, asks = quote.get("bids", []), quote.get("asks", [])
            if not bids or not asks:
                continue
            mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            factor = float(quote.get("quoteHomeConversionFactors", {}).get("negativeUnits") or 0)
            total += units * mid * factor
        return total

    def reconcile(self) -> dict:
        summary = self.adapter.preflight()
        trades = self.adapter.open_trades()
        pending = self.adapter.pending_orders()
        prior_tx = self.ledger.setting("last_transaction_id")
        transactions = self.adapter.transactions_since(prior_tx).get("transactions", []) if prior_tx else \
                       self.adapter.transactions_since("1").get("transactions", []) if trades else []
        if summary.get("last_transaction_id"):
            self.ledger.set_setting("last_transaction_id", summary["last_transaction_id"])
        self.ledger.event("BROKER_RECONCILIATION", {"summary": summary, "open_trades": trades,
                                                     "pending_orders": pending, "transactions": transactions})
        limits = self.risk_limits(float(summary["nav"]))
        if len(trades) > self.max_positions():
            raise BrokerError("broker position count exceeds two-position safety ceiling")
        if live_armed(self.adapter) or practice_armed(self.adapter):
            history_version = "tagged-closed-v1"
            if self.ledger.setting("historical_reconciliation_version") != history_version:
                history = self.adapter.transactions_since("1").get("transactions", [])
                imported = []
                mode = "LIVE" if live_armed(self.adapter) else "PRACTICE"
                for outcome in historical_managed_trade_outcomes(history, mode):
                    if self.ledger.import_closed_broker_intent(outcome):
                        imported.append({"intent_id": outcome["proposal_id"],
                                         "trade_id": outcome["broker_trade_id"],
                                         "symbol": outcome["symbol"],
                                         "realized_pnl_usd": outcome["realized_pnl_usd"]})
                self.ledger.set_setting("historical_reconciliation_version", history_version)
                self.ledger.event("BROKER_HISTORY_RECONCILED", {"imported": imported,
                                  "tagged_closed_trade_count": len(imported)})
            broker_positions = self.ledger.broker_positions()
            expected = {str(item.get("broker_trade_id") or "") for item in broker_positions if item.get("broker_trade_id")}
            actual = {str(item.get("id") or "") for item in trades}
            unexpected = actual - expected
            for trade in trades:
                trade_id = str(trade.get("id") or "")
                if trade_id not in unexpected:
                    continue
                limits = self.risk_limits(float(summary["nav"]))
                recovered = recoverable_managed_trade(trade, max(0.10, limits["risk_per_trade_usd"]), transactions)
                if not recovered:
                    continue
                existing=self.ledger.intent_for_broker_client_id(recovered["proposal_id"])
                intent_id=str(existing["id"]) if existing else recovered["proposal_id"]
                if not existing:
                    if self.ledger.has_intent(intent_id):
                        continue
                    self.ledger.add_intent(recovered, "LIVE" if live_armed(self.adapter) else "PRACTICE", "OPEN")
                self.ledger.update_intent(intent_id, "OPEN", trade_id=trade_id)
                self.ledger.event("BROKER_TRADE_RECOVERED", {"intent_id": intent_id,
                                  "trade_id": trade_id, "symbol": recovered["symbol"]})
                unexpected.remove(trade_id)
            broker_positions = self.ledger.broker_positions()
            if unexpected: raise BrokerError("unexpected broker trade detected; entries paused")
            self.ledger.update_excursions(trades)
            close_details = closed_trade_details(transactions)
            pnl_by_trade = {trade_id: float(detail["pnl"]) for trade_id,detail in close_details.items()}
            for position in broker_positions:
                trade_id = str(position.get("broker_trade_id") or "")
                if trade_id and trade_id not in actual:
                    pnl = pnl_by_trade.get(trade_id)
                    if pnl is None:
                        closed = self.adapter.trade(trade_id).get("trade", {})
                        pnl = (float(closed.get("realizedPL") or 0) + float(closed.get("financing") or 0)
                               + float(closed.get("dividendAdjustment") or 0))
                    close_reason=str((close_details.get(trade_id) or {}).get("reason") or "BROKER_HISTORY_LOOKUP")
                    if self.ledger.close_broker_intent(trade_id, pnl, close_reason):
                        self.ledger.event("BROKER_TRADE_CLOSED", {"intent_id": position["id"],
                                          "trade_id": trade_id, "realized_pnl_usd": pnl,"close_reason":close_reason})
            for trade in trades:
                if not trade.get("stopLossOrder") or not trade.get("takeProfitOrder"):
                    trade_id = str(trade.get("id") or "")
                    result = self.adapter.close_trade(trade_id) if trade_id else {}
                    self.ledger.event("UNPROTECTED_TRADE_EMERGENCY_CLOSE", {"trade_id": trade_id, "response": result})
                    raise BrokerError("unprotected broker trade closed; entries paused")
        today = datetime.now(UTC).date()
        today_key = today.isoformat()
        week_key = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        if self.ledger.setting("daily_baseline_date") != today_key:
            self.ledger.set_setting("daily_baseline_date", today_key)
            self.ledger.set_setting("daily_baseline_nav", str(summary["nav"]))
            self.ledger.event("DAILY_BASELINE_RESET", {"date": today_key, "nav": summary["nav"]})
        if self.ledger.setting("weekly_baseline_week") != week_key:
            self.ledger.set_setting("weekly_baseline_week", week_key)
            self.ledger.set_setting("weekly_baseline_nav", str(summary["nav"]))
            self.ledger.event("WEEKLY_BASELINE_RESET", {"week": week_key, "nav": summary["nav"]})
        daily_nav = float(self.ledger.setting("daily_baseline_nav", str(summary["nav"])))
        weekly_nav = float(self.ledger.setting("weekly_baseline_nav", str(summary["nav"])))
        armed = live_armed(self.adapter) or practice_armed(self.adapter)
        if armed and daily_nav - float(summary["nav"]) >= limits["daily_loss_usd"]:
            raise BrokerError("3% daily NAV loss circuit breaker active")
        if armed and weekly_nav - float(summary["nav"]) >= limits["weekly_loss_usd"]:
            raise BrokerError("6% weekly NAV loss circuit breaker active")
        if armed and limits["new_entries_halted"]:
            raise BrokerError("10% peak-to-current drawdown circuit breaker active")
        if armed and float(summary["margin_used"]) > limits["maximum_margin_used_usd"]:
            raise BrokerError("NAV-based margin-used circuit breaker active")
        with LOCK:
            STATE["open_positions"] = len(trades)
        return {"summary": summary, "open_trades": trades, "pending_orders": pending,
                "transactions": transactions}

    def process(self, snapshot: dict) -> dict:
        proposal = vars(self.engine.evaluate(snapshot))
        minimum_net_edge = max(0.0, float(os.getenv("FOREX_MIN_NET_EDGE_BPS", "8")))
        if float(proposal.get("expected_net_bps") or 0) < minimum_net_edge:
            raise MultiAssetRejected(
                f"estimated net edge below {minimum_net_edge:.2f} bps execution buffer")
        proposal["model_version"] = FOREX_MODEL_VERSION
        alignment, _, _ = self.engine.alignment(snapshot)
        proposal["entry_reason"] = (
            f"{alignment.replace('_', ' ').title()} {proposal['side']} signal; "
            f"score {float(proposal['score']):.2f} met minimum {self.engine.policy.minimum_score:.2f}; "
            f"horizon agreement {float(proposal.get('horizon_agreement') or 0) * 100:.1f}%; "
            f"estimated net value {float(proposal.get('expected_net_bps') or 0):.2f} bps."
        )
        calendar_ok = calendar_execution_allowed(snapshot)
        mode = "LIVE" if live_armed(self.adapter) else "PRACTICE" if practice_armed(self.adapter) else "PAPER_ONLY"
        if mode in {"LIVE", "PRACTICE"} and not calendar_ok:
            raise MultiAssetRejected(
                "verified economic calendar evidence with no active pair blackout required for broker execution")
        preflight = self.adapter.preflight()
        limits = self.risk_limits(float(preflight.get("nav") or preflight.get("balance") or 0))
        if limits["new_entries_halted"]:
            raise MultiAssetRejected("10% drawdown throttle halts new entries")
        if self.ledger.open_count() >= limits["maximum_open_positions"]:
            raise MultiAssetRejected("position limit reached")
        overlaps = [symbol for symbol in self.ledger.open_symbols()
                    if self.currencies(symbol) & self.currencies(str(proposal["symbol"]))]
        if overlaps:
            raise MultiAssetRejected("currency/correlation overlap guard active")
        if self.ledger.symbol_in_cooldown(str(proposal["symbol"]), int(os.getenv("FOREX_SYMBOL_COOLDOWN_SECONDS", "3600"))):
            raise MultiAssetRejected("symbol cooldown active")
        remaining_risk = limits["combined_risk_usd"] - self.ledger.open_risk()
        trade_risk = min(limits["risk_per_trade_usd"], remaining_risk)
        if trade_risk < max(0.01, float(os.getenv("FOREX_MIN_EXECUTABLE_RISK_USD", "0.10"))):
            raise MultiAssetRejected("combined portfolio risk cap reached")
        proposal["maximum_loss_usd"] = trade_risk
        intent_id = proposal["proposal_id"]
        if self.ledger.has_intent(intent_id):
            return {"status": "DUPLICATE_SUPPRESSED", "id": intent_id}
        open_trades = self.adapter.open_trades() if mode != "PAPER_ONLY" else []
        open_notional = self.open_notional_home(open_trades) if open_trades else 0.0
        notional_remaining = limits["combined_notional_usd"] - open_notional
        single_notional = min(limits["single_notional_usd"], notional_remaining)
        margin_remaining = limits["maximum_margin_used_usd"] - float(preflight.get("margin_used") or 0)
        if single_notional <= 0:
            raise MultiAssetRejected("combined NAV-based notional cap reached")
        if margin_remaining <= 0:
            raise MultiAssetRejected("combined NAV-based margin cap reached")
        proposal["quantity"] = safe_quantity(
            self.adapter, proposal, trade_risk, margin_remaining, single_notional)
        if mode == "PAPER_ONLY":
            self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            self.ledger.event("PAPER_FILL", proposal)
            return {"status": "PAPER_FILL", "id": intent_id}
        if preflight["balance"] <= 0 or preflight["margin_available"] <= 0:
            raise MultiAssetRejected("live account has no available capital")
        if preflight["open_trade_count"] >= limits["maximum_open_positions"]:
            raise MultiAssetRejected("broker position limit reached")
        self.ledger.add_intent(proposal, mode, "SUBMITTING")
        response = self.adapter.create_order(proposal, client_order_id=broker_client_id(intent_id))
        rejected = response.get("orderRejectTransaction")
        if rejected:
            self.ledger.update_intent(intent_id, "REJECTED")
            self.ledger.event("ORDER_REJECTED", {"intent_id": intent_id, "response": response})
            raise BrokerError(str(rejected.get("rejectReason") or "order rejected"))
        cancelled = response.get("orderCancelTransaction")
        if cancelled and not response.get("orderFillTransaction"):
            self.ledger.update_intent(intent_id, "CANCELLED")
            self.ledger.event("ORDER_CANCELLED", {"intent_id": intent_id, "response": response})
            raise BrokerError(str(cancelled.get("reason") or "order cancelled without fill"))
        created = response.get("orderCreateTransaction") or {}
        filled = response.get("orderFillTransaction") or {}
        order_id = str(created.get("id") or filled.get("orderID") or "")
        trade_id = str((filled.get("tradeOpened") or {}).get("tradeID") or "")
        status = "OPEN" if trade_id else "SUBMITTED"
        self.ledger.update_intent(intent_id, status, order_id, trade_id)
        self.ledger.event("ORDER_ACCEPTED", {"intent_id": intent_id, "order_id": order_id, "trade_id": trade_id, "response": response})
        if trade_id:
            protected = {}
            for _ in range(3):
                protected = next((item for item in self.adapter.open_trades() if str(item.get("id")) == trade_id), {})
                if protected.get("stopLossOrder") and protected.get("takeProfitOrder"): break
                time.sleep(1)
            if not protected.get("stopLossOrder") or not protected.get("takeProfitOrder"):
                close = self.adapter.close_trade(trade_id)
                self.ledger.update_intent(intent_id, "EMERGENCY_CLOSED", order_id, trade_id)
                self.ledger.event("ENTRY_PROTECTION_FAILED", {"intent_id": intent_id, "trade_id": trade_id, "close": close})
                raise BrokerError("entry protection missing; trade closed")
        return {"status": status, "id": intent_id, "order_id": order_id, "trade_id": trade_id}

    def supervise_live_profit(self, reconciliation: dict, snapshots: list[dict]) -> list[dict]:
        """Actively retain live profit without weakening the broker stop/target."""
        if not (live_armed(self.adapter) or practice_armed(self.adapter)):
            return []
        if os.getenv("FOREX_LIVE_PROFIT_RETENTION_ENABLED", "true").lower() != "true":
            return []
        marks = {str(item.get("symbol") or ""): item for item in snapshots}
        intents = {str(item.get("broker_trade_id") or ""): item
                   for item in self.ledger.broker_positions() if item.get("broker_trade_id")}
        actions = []
        for raw in reconciliation.get("open_trades", []):
            trade_id = str(raw.get("id") or "")
            symbol = str(raw.get("instrument") or "")
            snapshot = marks.get(symbol, {})
            intent = intents.get(trade_id, {})
            normalized = normalize_open_trade(raw, snapshot)
            risk = float(intent.get("maximum_loss_usd") or 0)
            if risk <= 0:
                continue
            ledger_peak_r = max(0.0, float(intent.get("max_favorable_pnl_usd") or 0) / risk)
            key = "live_profit_peak_r:" + trade_id
            prior_peak = max(ledger_peak_r, float(self.ledger.setting(key, "0") or 0))
            decision = live_profit_exit_decision(
                normalized, snapshot, prior_peak, self.engine.policy.minimum_score)
            if decision.get("eligible"):
                self.ledger.set_setting(key, str(decision["maximum_favorable_r"]))
            self.ledger.event("LIVE_PROFIT_RETENTION_EVALUATED", decision)
            try:
                opened = datetime.fromisoformat(str(intent.get("created_at") or "").replace("Z", "+00:00"))
                elapsed = (datetime.now(UTC) - opened.astimezone(UTC)).total_seconds() / 60
            except (ValueError, TypeError):
                elapsed = 0
            thesis = early_thesis_failure_shadow(
                intent, snapshot, float(normalized.get("unrealized_pnl_usd") or 0),
                prior_peak * risk, elapsed)
            if self.ledger.record_shadow_exit(trade_id, str(intent.get("strategy") or "FOREX_CONTROL"),
                                              symbol, float(normalized.get("current_price") or 0),
                                              float(normalized.get("unrealized_pnl_usd") or 0), thesis):
                self.ledger.event("SHADOW_EARLY_THESIS_FAILURE", thesis)
            if not decision.get("execute"):
                actions.append(decision)
                continue
            response = self.adapter.close_trade(trade_id)
            event = {**decision, "trade_id": trade_id, "instrument": symbol,
                     "executed_at": utcnow(), "broker_response": response}
            self.ledger.event("LIVE_PROFIT_RETENTION_CLOSE_REQUESTED", event)
            actions.append(event)
        return actions

    def supervise_paper(self, snapshots: list[dict]) -> list[dict]:
        marks = {str(item.get("symbol")): item for item in snapshots}
        closes = []
        checkpoint_rows = [dict(row) for row in self.ledger.db.execute(
            """SELECT * FROM intents WHERE strategy IN (?,?) AND
            (status='PAPER_OPEN' OR (closed_at IS NOT NULL AND closed_at>=datetime('now','-5 hours')))""",
            (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchall()]
        checkpoint_rows.extend(dict(row) for row in self.ledger.db.execute(
            """SELECT * FROM intents WHERE strategy=? AND
            (status='PAPER_OPEN' OR (closed_at IS NOT NULL AND closed_at>=datetime('now','-5 hours')))""",
            (BRYNE_LIQUIDITY_V5_STRATEGY,)).fetchall())
        for position in checkpoint_rows:
            snapshot = marks.get(position["symbol"], {})
            # A long can be sold at bid; a short must be bought back at ask.
            executable = snapshot.get("bid") if position["side"] == "BUY" else snapshot.get("ask")
            price = float(executable or snapshot.get("price") or 0)
            if price <= 0: continue
            opened = datetime.fromisoformat(str(position["created_at"]).replace("Z", "+00:00"))
            elapsed = max(0, (datetime.now(UTC) - opened.astimezone(UTC)).total_seconds() / 60)
            checkpoint_tolerance = max(2.0, float(os.getenv("FOREX_CHECKPOINT_TOLERANCE_MINUTES", "2")))
            for checkpoint in (0, 15, 30, 60, 120, 240):
                # Keep the first executable observation after each horizon and
                # disclose its delay; never present a late quote as exact history.
                delay_seconds = max(0.0, (elapsed - checkpoint) * 60)
                if checkpoint <= elapsed and (position.get("status") == "PAPER_OPEN" or
                                               elapsed < checkpoint + checkpoint_tolerance):
                    self.ledger.record_checkpoint(position, checkpoint, snapshot, price, delay_seconds)
            if position.get("status") != "PAPER_OPEN":
                continue
            side = position["side"]; stop = float(position["stop_price"]); target = float(position["target_price"])
            current_pnl = five_streak_position_pnl(position, price) if position.get("strategy") in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY} else 0.0
            with self.ledger.db:
                self.ledger.db.execute("""UPDATE intents SET
                    max_favorable_pnl_usd=MAX(COALESCE(max_favorable_pnl_usd,0),?),
                    max_adverse_pnl_usd=MIN(COALESCE(max_adverse_pnl_usd,0),?),
                    max_favorable_at=CASE WHEN ?>COALESCE(max_favorable_pnl_usd,0) THEN ? ELSE max_favorable_at END,
                    max_adverse_at=CASE WHEN ?<COALESCE(max_adverse_pnl_usd,0) THEN ? ELSE max_adverse_at END
                    WHERE id=?""", (current_pnl, current_pnl, current_pnl, utcnow(),
                    current_pnl, utcnow(), position["id"]))
            reason = "STOP" if (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop) else \
                     "TARGET" if (side == "BUY" and price >= target) or (side == "SELL" and price <= target) else ""
            maximum_loss = float(position["maximum_loss_usd"])
            maximum_favorable = max(current_pnl, float(position.get("max_favorable_pnl_usd") or 0))
            thesis = early_thesis_failure_shadow(position, snapshot, current_pnl,
                                                  maximum_favorable, elapsed)
            if self.ledger.record_shadow_exit(str(position["id"]), str(position.get("strategy") or ""),
                                              str(position["symbol"]), price, current_pnl, thesis):
                self.ledger.event("SHADOW_EARLY_THESIS_FAILURE", thesis)
            floor_r = five_streak_profit_floor_r(maximum_favorable / maximum_loss) if maximum_loss > 0 else 0
            profit_protection_strategies = {
                FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY,
            }
            if (not reason and position.get("strategy") in profit_protection_strategies
                    and os.getenv("FOREX_BRYNE_PROFIT_PROTECTION", "true").lower() == "true"
                    and floor_r > 0 and current_pnl <= floor_r * maximum_loss):
                reason = f"PROFIT_PROTECTION_{floor_r:.2f}R"
            if reason:
                direction = 1 if side == "BUY" else -1
                if position.get("strategy") in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY, BRYNE_LIQUIDITY_V5_STRATEGY}:
                    pnl = five_streak_position_pnl(position, price)
                else:
                    pnl = (price - float(position["entry_price"])) * float(position["quantity"]) * direction
                self.ledger.update_intent(position["id"], "PAPER_CLOSED")
                with self.ledger.db:
                    self.ledger.db.execute("UPDATE intents SET realized_pnl_usd=?,closed_at=?,close_reason=?,close_price=? WHERE id=?",
                                           (round(pnl, 8), utcnow(), reason, price, position["id"]))
                event = {"intent_id": position["id"], "symbol": position["symbol"], "fill_price": price,
                         "reason": reason, "realized_pnl_usd": round(pnl, 8),
                         "strategy": position.get("strategy"), "closed_at": utcnow()}
                self.ledger.event("PAPER_CLOSE", event); closes.append(event)
        return closes

    def process_five_streak_paper(self, snapshots: list[dict], nav: float) -> list[dict]:
        # V4 is archived.  Keep this method as a compatibility boundary for
        # callers/tests, but never allow it to create another intent.  Existing
        # V4 positions are still supervised and checkpointed by supervise_paper.
        return [{"status": "ARCHIVED", "reason": "V4 new entries are permanently disabled"}]
        if not five_streak_enabled():  # pragma: no cover - archived implementation below
            return []
        outcomes = []
        max_open = max(1, min(8, int(os.getenv("FOREX_FIVE_STREAK_MAX_OPEN", "8"))))
        per_trade_risk = max(.01, nav * min(.0025, max(.0005, float(os.getenv(
            "FOREX_FIVE_STREAK_RISK_PCT", ".0025")))))
        total_risk_cap = nav * .02
        for snapshot in snapshots:
            try:
                signals = five_streak_signals(snapshot)
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)[:300]})
                continue
            if not signals:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "NO_SIGNAL",
                                 "reason": "newest closed M5 candle has no qualifying five-candle streak"})
            for proposal in signals:
                if self.ledger.has_intent(proposal["proposal_id"]):
                    continue
                same_symbol = self.ledger.db.execute(
                    "SELECT 1 FROM intents WHERE strategy=? AND symbol=? AND status='PAPER_OPEN' LIMIT 1",
                    (FIVE_STREAK_FILTERED_STRATEGY, proposal["symbol"])).fetchone()
                if same_symbol or self.ledger.symbol_in_cooldown(proposal["symbol"], 3600):
                    outcomes.append({"symbol": proposal["symbol"], "status": "COOLDOWN_REJECTED",
                                     "reason": "Filtered V4 Ratchet permits one position per symbol and a 60-minute cooldown"})
                    continue
                row = self.ledger.db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(maximum_loss_usd),0) FROM intents WHERE strategy=? AND status='PAPER_OPEN'",
                    (FIVE_STREAK_FILTERED_STRATEGY,)).fetchone()
                if int(row[0]) >= max_open or float(row[1]) + per_trade_risk > total_risk_cap:
                    outcomes.append({"symbol": proposal["symbol"], "status": "RISK_CAP_REJECTED",
                                     "signal_time": proposal["signal_time"]})
                    continue
                proposal["maximum_loss_usd"] = per_trade_risk
                proposal.setdefault("entry_reason", "Filtered V4 Ratchet five-candle entry with recorded market evidence.")
                self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
                self.ledger.event("FIVE_STREAK_PAPER_FILL", proposal)
                outcomes.append({"symbol": proposal["symbol"], "side": proposal["side"],
                                 "status": "PAPER_FILL", "signal_time": proposal["signal_time"],
                                 "intent_id": proposal["proposal_id"], "entry_reason": proposal["entry_reason"],
                                 "entry": proposal["reference_price"], "stop": proposal["stop_price"],
                                 "target": proposal["target_price"], "maximum_loss_usd": per_trade_risk})
        return outcomes

    def process_bryne_liquidity_paper(self, snapshots: list[dict], nav: float) -> list[dict]:
        outcomes = []
        per_trade_risk = max(.01, nav * min(.0025, max(.0005, float(os.getenv(
            "FOREX_BRYNE_V5_RISK_PCT", ".0025")))))
        for snapshot in snapshots:
            try:
                signals = bryne_liquidity_signals(snapshot)
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)[:300]})
                continue
            if not signals:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "NO_SIGNAL",
                                 "reason": "no confirmed H1 range sweep and structure-break retest"})
            for proposal in signals:
                if self.ledger.has_intent(proposal["proposal_id"]):
                    continue
                open_same = self.ledger.db.execute(
                    "SELECT 1 FROM intents WHERE strategy=? AND symbol=? AND status='PAPER_OPEN' LIMIT 1",
                    (BRYNE_LIQUIDITY_V5_STRATEGY, proposal["symbol"])).fetchone()
                if open_same:
                    outcomes.append({"symbol": proposal["symbol"], "status": "OPEN_POSITION_REJECTED",
                                     "reason": "Bryne V5 already has an open position for this pair"})
                    continue
                proposal["maximum_loss_usd"] = per_trade_risk
                self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
                self.ledger.event("BRYNE_LIQUIDITY_V5_PAPER_FILL", proposal)
                outcomes.append({"symbol": proposal["symbol"], "side": proposal["side"],
                                 "status": "PAPER_FILL", "signal_time": proposal["signal_time"],
                                 "intent_id": proposal["proposal_id"], "entry_reason": proposal["entry_reason"],
                                 "entry": proposal["reference_price"], "stop": proposal["stop_price"],
                                 "target": proposal["target_price"], "maximum_loss_usd": per_trade_risk})
        return outcomes

    def scan(self) -> None:
        if not 0 <= self.engine.policy.minimum_score <= 100:
            raise BrokerError(f"FOREX_MIN_SCORE must be between 0 and 100; got {self.engine.policy.minimum_score:g}")
        reconciliation = self.reconcile()
        payload = fetch_json(os.environ["MULTI_ASSET_FEED_URL"])
        snapshots = validated_snapshots(payload)
        live_profit_actions = self.supervise_live_profit(reconciliation, snapshots)
        closes = self.supervise_paper(snapshots)
        five_streak_outcomes = [{"status": "ARCHIVED",
                                 "reason": "V4 new entries are permanently disabled"}]
        bryne_liquidity_outcomes = self.process_bryne_liquidity_paper(
            snapshots, float(reconciliation["summary"]["nav"]))
        outcomes = []
        for snapshot in snapshots:
            score = round(self.engine.score(snapshot), 2)
            alignment, alignment_points, proposed_side = self.engine.alignment(snapshot)
            diagnostics = {"alignment": alignment, "alignment_points": alignment_points,
                           "proposed_side": proposed_side,
                           "horizon_agreement": snapshot.get("horizon_agreement"),
                           "liquidity_score": snapshot.get("liquidity_score"),
                           "atr_14": snapshot.get("atr_14"),
                           "ewma_volatility_price": snapshot.get("ewma_volatility_price")}
            try:
                outcomes.append({"symbol": snapshot.get("symbol"), "score": score,
                                 "minimum_score": self.engine.policy.minimum_score, **diagnostics,
                                 **self.process(snapshot)})
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "score": score,
                                 "minimum_score": self.engine.policy.minimum_score,
                                 **diagnostics,
                                 "status": "REJECTED", "reason": str(exc)[:300]})
        self.ledger.event("SCAN", {"outcomes": outcomes, "paper_closes": closes})
        broker_open_trades = [normalize_open_trade(item, next((snapshot for snapshot in snapshots
            if snapshot.get("symbol") == item.get("instrument")), {}))
            for item in reconciliation["open_trades"]]
        intent_by_trade = {str(item.get("broker_trade_id") or ""): item
                           for item in self.ledger.broker_positions() if item.get("broker_trade_id")}
        raw_notionals = []
        for raw, normalized in zip(reconciliation["open_trades"], broker_open_trades):
            symbol = str(raw.get("instrument") or "")
            units = abs(float(raw.get("currentUnits") or raw.get("initialUnits") or 0))
            notional = 0.0
            try:
                quote = self.adapter.price(symbol)
                bids, asks = quote.get("bids", []), quote.get("asks", [])
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                factor = float(quote.get("quoteHomeConversionFactors", {}).get("negativeUnits") or 0)
                notional = units * mid * factor
            except Exception:
                pass
            raw_notionals.append(notional)
            intent = intent_by_trade.get(str(normalized.get("trade_id") or ""), {})
            normalized["notional_exposure_usd"] = notional or None
            normalized["maximum_planned_loss_usd"] = intent.get("maximum_loss_usd")
            expected = float(intent.get("entry_price") or 0)
            actual = float(normalized.get("entry_price") or 0)
            direction = 1 if normalized.get("side") == "BUY" else -1
            normalized["expected_entry_price"] = expected or None
            normalized["entry_slippage_bps"] = (((actual - expected) / expected) * direction * 10_000
                                                   if expected and actual else None)
            normalized["commission_usd"] = float(raw.get("commission") or 0)
        total_notional = sum(raw_notionals)
        total_margin = float(reconciliation["summary"].get("margin_used") or 0)
        for normalized, notional in zip(broker_open_trades, raw_notionals):
            margin = normalized.get("broker_initial_margin_usd")
            if margin is None and total_notional > 0:
                margin = total_margin * notional / total_notional
            pnl = float(normalized.get("unrealized_pnl_usd") or 0)
            risk_amount = float(normalized.get("maximum_planned_loss_usd") or 0)
            normalized["estimated_margin_allocation_usd"] = margin
            normalized["return_on_notional_pct"] = (pnl / notional * 100) if notional else None
            normalized["return_on_planned_risk_pct"] = (pnl / risk_amount * 100) if risk_amount else None
            try:
                opened = datetime.fromisoformat(str(normalized.get("open_time") or "").replace("Z", "+00:00"))
                elapsed = max(0, (datetime.now(UTC) - opened.astimezone(UTC)).total_seconds() / 60)
                tolerance = max(2.0, float(os.getenv("FOREX_CHECKPOINT_TOLERANCE_MINUTES", "2")))
                for checkpoint in (0, 15, 30, 60, 120, 240):
                    if checkpoint <= elapsed < checkpoint + tolerance:
                        self.ledger.record_live_checkpoint(normalized, checkpoint)
            except (TypeError, ValueError):
                pass
        live_profit_shadows = []
        for trade in broker_open_trades:
            key = "live_profit_shadow_peak_r:" + str(trade.get("trade_id") or "")
            prior_peak = float(self.ledger.setting(key, "0") or 0)
            shadow = live_profit_protection_shadow(trade, prior_peak)
            if shadow.get("eligible"):
                self.ledger.set_setting(key, str(shadow["maximum_favorable_r"]))
            live_profit_shadows.append(shadow)
        report = {"generated_at": utcnow(), "deployment": deployment_info(), "mode": "LIVE_ARMED" if live_armed(self.adapter) else
                  "PRACTICE_ARMED" if practice_armed(self.adapter) else "PAPER_ONLY",
                  "executor_ready": True, "last_scan": utcnow(), "last_error": "",
                  "broker": reconciliation["summary"],
                  "open_trade_count": len(reconciliation["open_trades"]),
                  "pending_order_count": len(reconciliation["pending_orders"]),
                  "transaction_count_since_prior_scan": len(reconciliation["transactions"]),
                  "broker_open_trades": broker_open_trades,
                  "cross_strategy_learning": {
                      "name": "Cost-aware profit retention",
                      "mode": "ACTIVE_GUARDED", "version": "LIVE_PROFIT_RETENTION_V2",
                      "source": "Bryne and Lot-Bill Filtered V4 Ratchet paper strategy",
                      "live_forex_observations": live_profit_shadows,
                      "live_execution_changed": True,
                      "live_profit_actions": live_profit_actions,
                      "reason": "Keep the full target while aligned; retain profit after ratchet breach or confirmed thesis deterioration",
                      "adoption_gate": "30-50 independent closed shadow observations with positive cost-stressed expectancy",
                  },
                  "snapshots": snapshots, "outcomes": outcomes, "paper_closes": closes,
                  "trade_checkpoints": self.ledger.trade_checkpoints(),
                  "live_trade_checkpoints": self.ledger.live_trade_checkpoints(),
                  "shadow_exit_observations": self.ledger.shadow_exit_observations(),
                  "five_streak": {"name": FIVE_STREAK_DISPLAY_NAME, "mode": "PAPER_ONLY",
                                  "version": "Liquidity Range V5", "timeframe": "H1 structure / executable quote entry",
                                  "enabled": five_streak_enabled(),
                                  "exit_policy": "stop beyond the liquidity sweep or 2R target; no arbitrary time exit",
                                  "cost_model": "completed H1 structure; executable bid/ask entry and exit",
                                  "outcomes": bryne_liquidity_outcomes,
                                  "performance": {**self.ledger.strategy_stats(BRYNE_LIQUIDITY_V5_STRATEGY),
                                      "unrealized_pnl_usd": round(sum(
                                          five_streak_position_pnl(position, next((float(s.get("price") or 0)
                                              for s in snapshots if s.get("symbol") == position.get("symbol")), 0))
                                          for position in self.ledger.paper_positions()
                                          if position.get("strategy") == BRYNE_LIQUIDITY_V5_STRATEGY), 8)},
                                  "trades": self.ledger.strategy_intents(BRYNE_LIQUIDITY_V5_STRATEGY),
                                  "promotion_checkpoint": {
                                      "scope": "Liquidity Range V5 prospective closes only",
                                      "required_closed_observations": 30,
                                      "profitable_closes_observed": self.ledger.strategy_stats(BRYNE_LIQUIDITY_V5_STRATEGY)["wins"],
                                      "closed_observations": self.ledger.strategy_stats(BRYNE_LIQUIDITY_V5_STRATEGY)["closed"],
                                      "net_pnl_usd": self.ledger.strategy_stats(BRYNE_LIQUIDITY_V5_STRATEGY)["net_pnl_usd"],
                                      "eligible_for_live_review": False,
                                      "automatic_live_promotion": False,
                                  },
                                  "profit_protection_shadow": {
                                      "mode": "ACTIVE_V5_PAPER_EXIT_LIVE_SHADOW_ONLY",
                                      "v5_paper_exit_enforced": True,
                                      "live_order_changes_enabled": False,
                                      "activation_range_r": [0.5, 0.75],
                                      "required_closed_observations": "30-50",
                                      "automatic_exit_changes": False,
                                      "open_positions": [{
                                          "intent_id": position.get("id"),
                                          "symbol": position.get("symbol"),
                                          "maximum_favorable_r": round(
                                              float(position.get("max_favorable_pnl_usd") or 0) /
                                              max(float(position.get("maximum_loss_usd") or 0), 1e-12), 6),
                                          "shadow_floor_r": five_streak_profit_floor_r(
                                              float(position.get("max_favorable_pnl_usd") or 0) /
                                              max(float(position.get("maximum_loss_usd") or 0), 1e-12)),
                                      } for position in self.ledger.paper_positions()
                                       if position.get("strategy") == BRYNE_LIQUIDITY_V5_STRATEGY],
                                  },
                                  "v4_ratchet_archived": {"new_entries_enabled": False,
                                      "performance": self.ledger.strategy_stats(FIVE_STREAK_FILTERED_STRATEGY),
                                      "trades": self.ledger.strategy_intents(FIVE_STREAK_FILTERED_STRATEGY)},
                                  "filtered_v3_archived": {"new_entries_enabled": False,
                                      "performance": self.ledger.strategy_stats(FIVE_STREAK_FILTERED_V3_STRATEGY),
                                      "trades": self.ledger.strategy_intents(FIVE_STREAK_FILTERED_V3_STRATEGY)},
                                  "baseline_v2_archived": {"new_entries_enabled": False,
                                      "performance": self.ledger.strategy_stats(FIVE_STREAK_STRATEGY),
                                      "trades": self.ledger.strategy_intents(FIVE_STREAK_STRATEGY)}},
                  "intents": self.ledger.recent_intents(), "events": self.ledger.recent_events(),
                  "realized_pnl_usd": self.ledger.realized_pnl(),
                  "model_review": self.ledger.model_review(self.engine.policy.minimum_score),
                  "risk_configuration": {
                      "minimum_score": self.engine.policy.minimum_score,
                      "daily_baseline_nav": float(self.ledger.setting(
                          "daily_baseline_nav", str(reconciliation["summary"]["nav"]))),
                      **self.risk_limits(float(reconciliation["summary"]["nav"])),
                      "grandfathered_positions_above_limit": max(
                          0, len(reconciliation["open_trades"]) -
                          self.risk_limits(float(reconciliation["summary"]["nav"]))["maximum_open_positions"]),
                      "current_open_risk_usd": self.ledger.open_risk(),
                      "current_open_notional_usd": self.open_notional_home(reconciliation["open_trades"]),
                      "currency_overlap_guard": True,
                      "correlation_guard": "shared base or quote currency prohibited",
                      "minimum_reward_risk_ratio": 2.0,
                      "high_impact_calendar_blackout": True,
                  },
                  "capital_baseline_nav": float(os.getenv("FOREX_LIVE_BASELINE_USD", "0") or 0),
                  "daily_baseline_nav": float(self.ledger.setting("daily_baseline_nav", str(reconciliation["summary"]["nav"])))}
        report["recentActions"] = [{
            "action": ("PAPER CLOSED" if item.get("status") == "PAPER_CLOSED" else
                       f"PAPER {item.get('side')}" if str(item.get("status") or "").startswith("PAPER_") else
                       "LIVE CLOSED" if item.get("status") == "BROKER_CLOSED" else
                       f"LIVE {item.get('side')}"),
            "at": item.get("closed_at") or item.get("created_at"),
            "symbol": item.get("symbol"),
            "strategy": item.get("strategy"),
            "reason": item.get("close_reason") or item.get("entry_reason") or "Trade ledger action",
            "realizedPnlUsd": item.get("realized_pnl_usd"),
        } for item in report["intents"]]
        trade_actions = confirmed_trade_actions(
            reconciliation["transactions"], reconciliation["summary"],
            reconciliation["open_trades"], reconciliation["pending_orders"],
            report["risk_configuration"], snapshots, report["intents"], outcomes)
        trade_actions.extend(five_streak_email_actions(
            bryne_liquidity_outcomes, closes, report["intents"], reconciliation["summary"]))
        # Entry alerts used to exist only in the in-memory scan outcome.  If
        # delivery failed on that scan, only the later close could be mailed.
        # Replay the newest V5 open from the durable intent ledger; the email
        # action ID makes the backfill idempotent across scans/restarts.
        latest_five_open = next((item for item in report["five_streak"]["trades"]
                                 if item.get("status") == "PAPER_OPEN"), None)
        if latest_five_open:
            trade_actions.extend(five_streak_email_actions([{
                "status": "PAPER_FILL", "intent_id": latest_five_open.get("id"),
                "symbol": latest_five_open.get("symbol"), "side": latest_five_open.get("side"),
                "signal_time": latest_five_open.get("created_at"),
                "entry": latest_five_open.get("entry_price"),
                "entry_reason": latest_five_open.get("entry_reason"),
            }], [], report["intents"], reconciliation["summary"]))
        # Versioned handoff: include the latest closed paper result on every
        # scan. The emailer's persisted action IDs make this a one-time,
        # restart-safe backfill and prevent repeated historical reports.
        latest_five_closed = next((item for item in report["five_streak"]["trades"]
                                   if item.get("status") == "PAPER_CLOSED"), None)
        if latest_five_closed:
            trade_actions.extend(five_streak_email_actions([], [{
                "intent_id": latest_five_closed.get("id"),
                "symbol": latest_five_closed.get("symbol"),
                "fill_price": latest_five_closed.get("close_price"),
                "reason": latest_five_closed.get("close_reason"),
                "realized_pnl_usd": latest_five_closed.get("realized_pnl_usd"),
                "strategy": BRYNE_LIQUIDITY_V5_STRATEGY,
                "closed_at": latest_five_closed.get("closed_at"),
            }], report["intents"], reconciliation["summary"]))
        delivery = self.emailer.status()
        delivery_payload = dict(report)
        delivery_payload["_trade_actions"] = trade_actions
        delivery["attempt"] = self.emailer.maybe_send(delivery_payload)
        delivery["confirmed_actions_this_scan"] = len(trade_actions)
        report["email_delivery"] = delivery
        with LOCK:
            STATE["report"] = report
        print(json.dumps({"event": "FOREX_EXECUTOR_SCAN", "outcomes": outcomes, "paper_closes": closes}), flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/status", "/report", "/report.json"}:
            self.send_error(404); return
        with LOCK:
            state = dict(STATE)
        if self.path == "/report":
            report = dict(state.get("report") or {})
            report.update(mode=state["mode"], executor_ready=state["ok"], last_scan=state["last_scan"],
                          last_error=state["last_error"])
            body = render_forex_report(report).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/report.json":
            payload = dict(state.get("report") or {})
            payload.update(mode=state["mode"], executor_ready=state["ok"], last_scan=state["last_scan"],
                           last_error=state["last_error"])
            status = 200
        elif self.path == "/health":
            payload = {"ok": True, "service": "forex-executor", "mode": state["mode"],
                       "executor_ready": state["ok"], "last_scan": state["last_scan"],
                       "last_error": state["last_error"], "open_positions": state["open_positions"]}
            status = 200
        else:
            payload = state
            status = 200 if state["ok"] else 503
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): return


def main() -> None:
    if not truthy("FOREX_EXECUTOR_ENABLED"):
        raise SystemExit("FOREX_EXECUTOR_ENABLED is not true")
    executor = Executor()
    with LOCK:
        STATE["mode"] = "LIVE_ARMED" if live_armed(executor.adapter) else "PRACTICE_ARMED" if practice_armed(executor.adapter) else "PAPER_ONLY"
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"event": "FOREX_EXECUTOR_HTTP_READY", "host": "0.0.0.0", "port": port,
                      "mode": STATE["mode"]}), flush=True)
    interval = max(15, int(os.getenv("FOREX_EXECUTOR_INTERVAL_SECONDS", "30")))
    while True:
        try:
            executor.scan()
            with LOCK: STATE.update(ok=True, last_scan=utcnow(), last_error="")
        except Exception as exc:
            with LOCK: STATE.update(ok=False, last_scan=utcnow(), last_error=str(exc)[:500])
            print(json.dumps({"event": "FOREX_EXECUTOR_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
