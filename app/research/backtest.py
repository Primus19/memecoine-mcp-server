"""
Event-driven backtester with the production cost model.

Rules of the road (these are what make a backtest believable):
  * decisions are computed on bar i's close and executed at bar i+1's open
  * stops and targets are evaluated intra-bar using high/low, with gap handling
    (a gap through the stop fills at the open, not at the stop price)
  * every fill pays half the spread, slippage and the per-side fee
  * position size comes from the same RiskManager used for research risk gates
  * metrics include the production promotion gate (app.validation.promotion_gate)
    on cost-stressed per-trade returns so results are comparable to the forward ledgers
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from ..validation import promotion_gate, walk_forward_splits
from .config import CostConfig, RiskConfig, asset_class_for
from .indicators import periods_per_year
from .risk import PositionState, RiskManager


@dataclass
class TradeRecord:
    symbol: str
    side: int
    strategy: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float            # net return on notional, after costs
    r_multiple: float            # pnl / initial risk
    costs: float
    bars: int
    exit_reason: str

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_time"] = self.exit_time.isoformat()
        return d


@dataclass
class BacktestResult:
    trades: List[TradeRecord]
    equity: pd.Series
    metrics: dict
    per_symbol: dict = field(default_factory=dict)
    per_strategy: dict = field(default_factory=dict)
    rejected: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"metrics": self.metrics, "per_symbol": self.per_symbol, "per_strategy": self.per_strategy,
                "rejected": self.rejected, "trade_count": len(self.trades),
                "trades": [t.to_dict() for t in self.trades[-200:]]}


DecisionFn = Callable[[str, pd.DataFrame], pd.DataFrame]
"""A decision function receives (symbol, ohlcv) and returns a decision frame aligned to
ohlcv.index with the columns documented in strategies.Strategy plus 'atr' and 'vol'."""


def compute_metrics(trades: List[TradeRecord], equity: pd.Series, periods_per_year_: int,
                    starting_equity: float, cost_stress_bps: float = 0.0) -> dict:
    if equity.empty:
        return {"trades": 0}
    rets = equity.pct_change().dropna()
    total_return = equity.iloc[-1] / starting_equity - 1.0
    years = max(len(equity) / periods_per_year_, 1e-9)
    cagr = (equity.iloc[-1] / starting_equity) ** (1 / years) - 1.0 if equity.iloc[-1] > 0 else -1.0
    vol = rets.std(ddof=0) * math.sqrt(periods_per_year_) if len(rets) > 1 else 0.0
    sharpe = (rets.mean() * periods_per_year_) / vol if vol > 0 else 0.0
    downside = rets[rets < 0].std(ddof=0) * math.sqrt(periods_per_year_) if (rets < 0).any() else 0.0
    sortino = (rets.mean() * periods_per_year_) / downside if downside > 0 else 0.0
    peak = equity.cummax()
    dd = (equity / peak - 1.0)
    max_dd = float(dd.min())
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    trade_returns = [t.return_pct for t in trades]
    r_multiples = [t.r_multiple for t in trades]
    gate = promotion_gate(trade_returns, minimum_samples=100, cost_stress=cost_stress_bps / 10_000)
    exposure = float((equity.diff().abs() > 0).mean()) if len(equity) > 1 else 0.0
    return {
        "trades": len(trades),
        "trades_per_year": round(len(trades) / years, 1),
        "total_return_pct": round(float(total_return) * 100, 2),
        "cagr_pct": round(float(cagr) * 100, 2),
        "annual_vol_pct": round(float(vol) * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "sortino": round(float(sortino), 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "calmar": round(float(cagr / abs(max_dd)), 2) if max_dd < 0 else None,
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else (None if not wins else float("inf")),
        "avg_trade_return_bps": round(float(np.mean(trade_returns)) * 10_000, 1) if trade_returns else None,
        "median_trade_return_bps": round(float(np.median(trade_returns)) * 10_000, 1) if trade_returns else None,
        "expectancy_r": round(float(np.mean(r_multiples)), 3) if r_multiples else None,
        "avg_win_bps": round(float(np.mean([t.return_pct for t in trades if t.pnl > 0])) * 10_000, 1) if wins else None,
        "avg_loss_bps": round(float(np.mean([t.return_pct for t in trades if t.pnl < 0])) * 10_000, 1) if losses else None,
        "avg_bars_in_trade": round(float(np.mean([t.bars for t in trades])), 1) if trades else None,
        "total_costs": round(sum(t.costs for t in trades), 2),
        "exposure": round(exposure, 3),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "promotion_gate": {"eligible": gate.eligible, "reasons": list(gate.reasons),
                           "lower_confidence_bound_bps": round(gate.lower_confidence_bound * 10_000, 1),
                           "cost_stressed_mean_bps": round(gate.stressed_mean_return * 10_000, 1)},
        "exit_reasons": pd.Series([t.exit_reason for t in trades]).value_counts().to_dict() if trades else {},
    }


def run_backtest(data: Dict[str, pd.DataFrame], decide: DecisionFn, *, starting_equity: float = 10_000.0,
                 risk: Optional[RiskConfig] = None, costs: Optional[Dict[str, CostConfig]] = None,
                 timeframe: str = "1d", cost_stress_bps: float = 0.0,
                 eligibility: Optional[pd.DataFrame] = None) -> BacktestResult:
    """Portfolio backtest across `data` (symbol -> ohlcv). `eligibility` is an optional
    boolean frame (index=time, columns=symbols) that gates entries (e.g. cross-sectional
    momentum universe)."""
    risk = risk or RiskConfig()
    costs = costs or {}
    rm = RiskManager(risk, starting_equity)
    decisions: Dict[str, pd.DataFrame] = {}
    for symbol, df in data.items():
        d = decide(symbol, df)
        decisions[symbol] = d.reindex(df.index)
    index = sorted(set().union(*[set(df.index) for df in data.values()]))
    positions: Dict[str, PositionState] = {}
    pending: Dict[str, dict] = {}       # entries/exits to execute at next open
    trades: List[TradeRecord] = []
    equity_points = []
    rejected: Dict[str, int] = {}
    cash = starting_equity
    asset_class = {s: asset_class_for(s) for s in data}
    cost_of = {s: costs.get(asset_class[s]) or CostConfig.for_asset_class(asset_class[s]) for s in data}
    bar_no = {s: 0 for s in data}
    loc = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in data.items()}

    def fill_price(symbol: str, raw: float, side: int, is_entry: bool) -> float:
        c = cost_of[symbol]
        adverse = (c.spread_bps / 2 + c.slippage_bps_per_side) / 10_000
        direction = side if is_entry else -side
        return raw * (1 + direction * adverse)

    def fee(symbol: str, notional: float) -> float:
        return notional * cost_of[symbol].fee_bps_per_side / 10_000

    def close_position(symbol: str, pos: PositionState, raw_price: float, ts: datetime, reason: str, i: int):
        nonlocal cash
        px = fill_price(symbol, raw_price, pos.side, is_entry=False)
        notional_out = px * pos.qty
        f_out = fee(symbol, notional_out)
        f_in = fee(symbol, pos.entry_price * pos.qty)
        gross = (px - pos.entry_price) * pos.qty * pos.side
        pnl = gross - f_out - f_in
        if pos.side > 0:
            cash += notional_out - f_out
        else:
            cash -= notional_out + f_out
        risk_amount = pos.initial_risk * pos.qty
        trades.append(TradeRecord(symbol, pos.side, pos.strategy, pos.opened_at, ts, pos.entry_price, px, pos.qty,
                                  pnl, pnl / (pos.entry_price * pos.qty), pnl / risk_amount if risk_amount else 0.0,
                                  f_in + f_out + abs(px - raw_price) * pos.qty, pos.bars_held, reason))
        rm.record_close(pnl, i, symbol, stopped_out=reason in ("stop", "trailing_stop"))
        del positions[symbol]

    for gi, ts in enumerate(index):
        rm.new_bar(ts)
        # 1) execute pending orders at this bar's open
        for symbol, order in list(pending.items()):
            if ts not in loc[symbol]:
                continue
            row = data[symbol].loc[ts]
            i = loc[symbol][ts]
            if order["type"] == "exit" and symbol in positions:
                close_position(symbol, positions[symbol], float(row["open"]), ts, order["reason"], i)
            elif order["type"] == "entry" and symbol not in positions:
                ok, why = rm.can_open(symbol, i, positions)
                if not ok:
                    rejected[why] = rejected.get(why, 0) + 1
                else:
                    side = order["side"]
                    raw_open = float(row["open"])
                    px = fill_price(symbol, raw_open, side, is_entry=True)
                    atr_v = order["atr"]
                    stop = px - side * order["atr_mult"] * atr_v
                    target = px + side * order["target_r"] * order["atr_mult"] * atr_v if order["target_r"] and not np.isnan(order["target_r"]) else None
                    edge = rm.expected_edge_bps(order["confidence"], px, stop, target, cost_of[symbol])
                    if edge < risk.min_expected_edge_bps:
                        rejected["ev below minimum"] = rejected.get("ev below minimum", 0) + 1
                    else:
                        qty = rm.position_size(px, stop, order["vol"], i, order["confidence"])
                        notional = qty * px
                        if qty <= 0 or notional < 5.0 or notional + fee(symbol, notional) > max(cash, 0.0) * risk.max_gross_leverage:
                            rejected["size/cash"] = rejected.get("size/cash", 0) + 1
                        else:
                            if side > 0:
                                cash -= notional + fee(symbol, notional)
                            else:
                                cash += notional - fee(symbol, notional)
                            positions[symbol] = PositionState(symbol, side, qty, px, stop, abs(px - stop), target,
                                                              order["trailing"], order["atr_mult"], order["max_hold"],
                                                              order["strategy"], ts, 0, px)
            del pending[symbol]
        # 2) intra-bar stop/target checks and trailing updates for open positions
        for symbol, pos in list(positions.items()):
            if ts not in loc[symbol]:
                continue
            row = data[symbol].loc[ts]
            i = loc[symbol][ts]
            if pos.opened_at == ts:
                continue  # entered this bar at the open; evaluate from next bar
            pos.bars_held += 1
            hi, lo, op, cl = float(row["high"]), float(row["low"]), float(row["open"]), float(row["close"])
            if pos.side > 0:
                if lo <= pos.stop_price:
                    close_position(symbol, pos, min(op, pos.stop_price), ts, "trailing_stop" if pos.trailing and pos.stop_price > pos.entry_price - pos.initial_risk + 1e-12 else "stop", i)
                    continue
                if pos.target_price and hi >= pos.target_price:
                    close_position(symbol, pos, max(op, pos.target_price), ts, "target", i)
                    continue
                pos.best_price = max(pos.best_price, hi)
            else:
                if hi >= pos.stop_price:
                    close_position(symbol, pos, max(op, pos.stop_price), ts, "trailing_stop" if pos.trailing and pos.stop_price < pos.entry_price + pos.initial_risk - 1e-12 else "stop", i)
                    continue
                if pos.target_price and lo <= pos.target_price:
                    close_position(symbol, pos, min(op, pos.target_price), ts, "target", i)
                    continue
                pos.best_price = min(pos.best_price, lo)
            d = decisions[symbol].loc[ts]
            atr_v = float(d.get("atr", np.nan))
            if pos.trailing and not np.isnan(atr_v):
                if pos.side > 0:
                    pos.stop_price = max(pos.stop_price, pos.best_price - pos.atr_mult * atr_v)
                else:
                    pos.stop_price = min(pos.stop_price, pos.best_price + pos.atr_mult * atr_v)
            exit_signal = bool(d.get("long_exit", False)) if pos.side > 0 else bool(d.get("short_exit", False))
            if exit_signal:
                pending[symbol] = {"type": "exit", "reason": "signal"}
            elif pos.max_hold and pos.bars_held >= pos.max_hold:
                pending[symbol] = {"type": "exit", "reason": "time"}
        # 3) new entry decisions at close
        for symbol in data:
            if ts not in loc[symbol] or symbol in positions or symbol in pending:
                continue
            d = decisions[symbol].loc[ts]
            long_e = bool(d.get("long_entry", False))
            short_e = bool(d.get("short_entry", False))
            if not (long_e or short_e):
                continue
            if eligibility is not None and symbol in eligibility.columns:
                if ts in eligibility.index and not bool(eligibility.loc[ts, symbol]):
                    rejected["not in eligible universe"] = rejected.get("not in eligible universe", 0) + 1
                    continue
            atr_v = float(d.get("atr", np.nan))
            if np.isnan(atr_v) or atr_v <= 0:
                continue
            pending[symbol] = {"type": "entry", "side": 1 if long_e else -1, "atr": atr_v, "vol": float(d.get("vol", np.nan)),
                               "atr_mult": float(d.get("stop_atr_mult", 2.5)), "target_r": d.get("target_r", np.nan),
                               "trailing": bool(d.get("trailing", False)), "max_hold": int(d.get("max_hold", 0) or 0),
                               "confidence": float(d.get("confidence", 0.6)), "strategy": str(d.get("strategy", ""))}
        # 4) mark to market
        mtm = cash
        for symbol, pos in positions.items():
            px = float(data[symbol].loc[ts, "close"]) if ts in loc[symbol] else pos.entry_price
            mtm += px * pos.qty * pos.side
        rm.mark_equity(mtm)
        equity_points.append((ts, mtm))
    # close remaining positions at last close for reporting
    for symbol, pos in list(positions.items()):
        last_ts = data[symbol].index[-1]
        close_position(symbol, pos, float(data[symbol].loc[last_ts, "close"]), last_ts, "end", len(data[symbol]) - 1)
    equity = pd.Series([e for _, e in equity_points], index=[t for t, _ in equity_points], dtype=float)
    if not equity.empty:
        equity.iloc[-1] = cash
    any_symbol = next(iter(data))
    ppy = periods_per_year(timeframe, asset_class[any_symbol])
    metrics = compute_metrics(trades, equity, ppy, starting_equity, cost_stress_bps)
    per_symbol, per_strategy = {}, {}
    for key, getter, bucket in (("symbol", lambda t: t.symbol, per_symbol), ("strategy", lambda t: t.strategy, per_strategy)):
        for name in sorted({getter(t) for t in trades}):
            sub = [t for t in trades if getter(t) == name]
            wins = [t.pnl for t in sub if t.pnl > 0]
            losses = [t.pnl for t in sub if t.pnl < 0]
            bucket[name] = {"trades": len(sub), "net_pnl": round(sum(t.pnl for t in sub), 2),
                            "win_rate": round(len(wins) / len(sub), 3),
                            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
                            "avg_return_bps": round(float(np.mean([t.return_pct for t in sub])) * 10_000, 1),
                            "expectancy_r": round(float(np.mean([t.r_multiple for t in sub])), 3)}
    return BacktestResult(trades, equity, metrics, per_symbol, per_strategy, rejected)


def walk_forward_report(data: Dict[str, pd.DataFrame], decide: DecisionFn, *, train_bars: int, test_bars: int,
                        embargo: int = 5, **kwargs) -> List[dict]:
    """Evaluate the decision function on chronological out-of-sample windows.
    The strategies here are rule-based (no fitting), so 'train' only sets the warm-up
    history each window needs; the point is to see whether expectancy is stable over time."""
    reference = max(data.values(), key=len)
    reports = []
    for train_range, test_range in walk_forward_splits(len(reference), train=train_bars, test=test_bars, embargo=embargo):
        start, end = reference.index[test_range.start], reference.index[test_range.stop - 1]
        window = {s: df[(df.index <= end)] for s, df in data.items()}
        window = {s: df for s, df in window.items() if len(df) > train_bars // 2}
        if not window:
            continue
        result = run_backtest(window, decide, **kwargs)
        in_window = [t for t in result.trades if t.entry_time >= start]
        rets = [t.return_pct for t in in_window]
        reports.append({"test_start": start.isoformat(), "test_end": end.isoformat(), "trades": len(in_window),
                        "avg_return_bps": round(float(np.mean(rets)) * 10_000, 1) if rets else None,
                        "win_rate": round(float(np.mean([r > 0 for r in rets])), 3) if rets else None,
                        "net_pnl": round(sum(t.pnl for t in in_window), 2)})
    return reports
