"""
Portfolio risk gates and position sizing for research backtests.

This mirrors what production already enforces (per-trade risk, drawdown throttles,
position limits, EV gate after costs) so that backtest numbers are comparable to the
forward paper ledgers. It reuses app.quant.expected_net_value for the EV gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

import numpy as np

from ..quant import expected_net_value
from .config import CostConfig, RiskConfig


@dataclass
class PositionState:
    symbol: str
    side: int
    qty: float
    entry_price: float
    stop_price: float
    initial_risk: float           # |entry - stop| in price units
    target_price: Optional[float]
    trailing: bool
    atr_mult: float
    max_hold: int
    strategy: str
    opened_at: datetime
    bars_held: int = 0
    best_price: float = 0.0
    cluster: str = ""


@dataclass
class RiskState:
    equity: float
    peak_equity: float
    day_start_equity: float
    day: Optional[str] = None
    consecutive_losses: int = 0
    throttle_until_bar: int = -1
    halted: bool = False
    cooldown_until: Dict[str, int] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg: RiskConfig, starting_equity: float):
        self.cfg = cfg
        self.state = RiskState(starting_equity, starting_equity, starting_equity)

    # ---- bookkeeping ---------------------------------------------------------
    def new_bar(self, timestamp: datetime) -> None:
        day = timestamp.strftime("%Y-%m-%d")
        if self.state.day != day:
            self.state.day = day
            self.state.day_start_equity = self.state.equity

    def mark_equity(self, equity: float) -> None:
        s = self.state
        s.equity = equity
        s.peak_equity = max(s.peak_equity, equity)
        if s.peak_equity > 0 and (s.peak_equity - equity) / s.peak_equity >= self.cfg.max_drawdown_halt_pct:
            s.halted = True

    def record_close(self, pnl: float, bar_index: int, symbol: str, stopped_out: bool) -> None:
        s = self.state
        s.consecutive_losses = s.consecutive_losses + 1 if pnl < 0 else 0
        if s.consecutive_losses >= self.cfg.consecutive_loss_throttle:
            s.throttle_until_bar = bar_index + 24  # bars; ~1 day on hourly, ~1 month on daily
        if stopped_out:
            s.cooldown_until[symbol] = bar_index + self.cfg.symbol_cooldown_bars

    # ---- gates ----------------------------------------------------------------
    def drawdown(self) -> float:
        s = self.state
        return 0.0 if s.peak_equity <= 0 else max(0.0, (s.peak_equity - s.equity) / s.peak_equity)

    def size_multiplier(self, bar_index: int) -> float:
        mult = 1.0
        if self.drawdown() >= self.cfg.drawdown_throttle_pct:
            mult *= 0.5
        if bar_index <= self.state.throttle_until_bar:
            mult *= 0.5
        return mult

    def can_open(self, symbol: str, bar_index: int, open_positions: Dict[str, PositionState]) -> tuple[bool, str]:
        s = self.state
        if s.halted:
            return False, "max drawdown halt"
        if s.day_start_equity > 0 and (s.day_start_equity - s.equity) / s.day_start_equity >= self.cfg.daily_loss_limit_pct:
            return False, "daily loss limit"
        if len(open_positions) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if symbol in open_positions:
            return False, "already in position"
        if bar_index < s.cooldown_until.get(symbol, -1):
            return False, "symbol cooldown"
        return True, ""

    def expected_edge_bps(self, confidence: float, entry: float, stop: float, target: Optional[float],
                          cost: CostConfig) -> float:
        loss_bps = abs(entry - stop) / entry * 10_000
        gain_bps = abs(target - entry) / entry * 10_000 if target else loss_bps * 2.0  # trend: assume 2R avg win
        # confidence (0.55-0.95) is mapped conservatively to P(win) in [0.35, 0.65]
        p = float(np.clip(0.35 + (confidence - 0.5) * 0.6, 0.35, 0.65))
        return expected_net_value(win_probability=p, expected_gain_bps=gain_bps, expected_loss_bps=loss_bps,
                                  fee_bps=2 * cost.fee_bps_per_side, spread_bps=cost.spread_bps,
                                  slippage_bps=2 * cost.slippage_bps_per_side).expected_net_bps

    def position_size(self, entry: float, stop: float, annual_vol: float, bar_index: int,
                      confidence: float = 0.6) -> float:
        """Quantity such that a stop-out loses risk_per_trade_pct of equity, capped by
        volatility targeting, notional cap and a fractional-Kelly ceiling."""
        eq = self.state.equity
        risk_dist = abs(entry - stop)
        if risk_dist <= 0 or entry <= 0 or eq <= 0:
            return 0.0
        qty_risk = eq * self.cfg.risk_per_trade_pct / risk_dist
        vol = max(float(annual_vol) if annual_vol and not np.isnan(annual_vol) else 0.5, 0.05)
        qty_vol = eq * self.cfg.target_annual_vol / vol / entry
        qty_notional = eq * self.cfg.max_position_notional_pct / entry
        # Kelly with conservative p in [0.35,0.65] and reward:risk 2:1 -> f* = p - (1-p)/2
        p = float(np.clip(0.35 + (confidence - 0.5) * 0.6, 0.35, 0.65))
        kelly = max(0.0, p - (1 - p) / 2.0) * self.cfg.kelly_fraction_cap
        qty_kelly = eq * kelly / risk_dist if kelly > 0 else 0.0
        qty = min(qty_risk, qty_vol, qty_notional, qty_kelly if qty_kelly > 0 else qty_risk)
        return max(0.0, qty * self.size_multiplier(bar_index))
