from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct_returns(prices: Sequence[float]) -> list[float]:
    clean = [float(value) for value in prices if float(value) > 0]
    return [(right / left) - 1.0 for left, right in zip(clean, clean[1:])]


def ewma_volatility(prices: Sequence[float], decay: float = 0.94) -> float:
    """Return per-period EWMA volatility without annualization."""
    values = pct_returns(prices)
    if len(values) < 2:
        return 0.0
    decay = clamp(float(decay), 0.50, 0.999)
    variance = values[0] ** 2
    for value in values[1:]:
        variance = decay * variance + (1.0 - decay) * value ** 2
    return math.sqrt(max(0.0, variance))


def average_true_range(candles: Sequence[dict], periods: int = 14) -> float:
    """Calculate Wilder-style simple ATR from OANDA-compatible candles."""
    rows = list(candles)
    if len(rows) < 2:
        return 0.0
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in rows:
        mid = candle.get("mid") or {}
        high, low, close = (float(mid.get(key) or 0) for key in ("h", "l", "c"))
        if min(high, low, close) <= 0:
            continue
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        ranges.append(true_range)
        previous_close = close
    return statistics.mean(ranges[-max(2, int(periods)):]) if ranges else 0.0


def horizon_return(prices: Sequence[float], periods: int) -> float:
    values = [float(value) for value in prices]
    if periods < 1 or len(values) <= periods or values[-1] <= 0 or values[-1 - periods] <= 0:
        return 0.0
    return (values[-1] / values[-1 - periods] - 1.0) * 100.0


def multi_horizon_consensus(changes: Iterable[float], weights: Iterable[float] | None = None) -> dict:
    values = [float(value) for value in changes]
    assigned = [float(value) for value in weights] if weights is not None else [1.0] * len(values)
    if not values or len(values) != len(assigned) or sum(assigned) <= 0:
        return {"direction": 0, "agreement": 0.0, "weighted_signal": 0.0}
    signed = [1 if value > 0 else -1 if value < 0 else 0 for value in values]
    weighted = sum(sign * weight for sign, weight in zip(signed, assigned)) / sum(assigned)
    direction = 1 if weighted > 0 else -1 if weighted < 0 else 0
    agreement = sum(weight for sign, weight in zip(signed, assigned) if sign == direction) / sum(assigned) if direction else 0.0
    return {"direction": direction, "agreement": agreement, "weighted_signal": weighted}


def liquidity_quality(*, spread_bps: float, median_spread_bps: float, bid_liquidity: float,
                      ask_liquidity: float, quote_age_seconds: float, maximum_quote_age: float = 10.0) -> float:
    """A bounded, factual liquidity score. Missing or stale data receives no credit."""
    if spread_bps < 0 or median_spread_bps <= 0 or min(bid_liquidity, ask_liquidity) <= 0:
        return 0.0
    spread_score = clamp(median_spread_bps / max(spread_bps, 1e-9), 0.0, 1.0)
    balance_score = min(bid_liquidity, ask_liquidity) / max(bid_liquidity, ask_liquidity)
    freshness_score = clamp(1.0 - quote_age_seconds / max(maximum_quote_age, 1.0), 0.0, 1.0)
    return round(0.50 * spread_score + 0.30 * balance_score + 0.20 * freshness_score, 6)


@dataclass(frozen=True)
class NetValueEstimate:
    win_probability: float
    expected_gain_bps: float
    expected_loss_bps: float
    total_cost_bps: float
    expected_net_bps: float
    reward_risk: float


def expected_net_value(*, win_probability: float, expected_gain_bps: float,
                       expected_loss_bps: float, fee_bps: float = 0.0,
                       spread_bps: float = 0.0, slippage_bps: float = 0.0,
                       financing_bps: float = 0.0) -> NetValueEstimate:
    probability = clamp(float(win_probability), 0.0, 1.0)
    gain, loss = max(0.0, float(expected_gain_bps)), max(0.0, float(expected_loss_bps))
    costs = sum(max(0.0, float(value)) for value in (fee_bps, spread_bps, slippage_bps, financing_bps))
    net = probability * gain - (1.0 - probability) * loss - costs
    return NetValueEstimate(probability, gain, loss, costs, net, gain / loss if loss else 0.0)


def conservative_probability(score: float, agreement: float, *, floor: float = 0.35, ceiling: float = 0.65) -> float:
    """Shadow-only prior. It intentionally prevents raw scores becoming overconfident probabilities."""
    raw = 0.50 + (float(score) - 75.0) / 250.0 + (clamp(agreement, 0.0, 1.0) - 0.50) / 10.0
    return clamp(raw, floor, ceiling)



def kelly_fraction(win_probability: float, reward_risk: float, *, cap: float = 0.25) -> float:
    """Fractional Kelly for a stop/target trade: f* = p - (1-p)/b, scaled by `cap`.

    Full Kelly is far too aggressive for estimated probabilities; the industry norm is
    quarter Kelly (cap=0.25). Returns 0 when the edge is not positive."""
    p = clamp(float(win_probability), 0.0, 1.0)
    b = float(reward_risk)
    if b <= 0:
        return 0.0
    full = p - (1.0 - p) / b
    return max(0.0, full) * clamp(float(cap), 0.0, 1.0)


def volatility_target_notional(equity: float, annual_volatility: float, *, target_annual_volatility: float = 0.15,
                               max_notional_fraction: float = 1.0) -> float:
    """Notional that scales exposure inversely with realized volatility (vol targeting).

    Positions in a 90%-vol coin get a fraction of the notional a 10%-vol FX pair gets, so
    every position contributes roughly the same risk. Bounded by `max_notional_fraction`
    of equity."""
    equity = max(0.0, float(equity))
    vol = float(annual_volatility)
    if equity <= 0 or vol <= 0 or target_annual_volatility <= 0:
        return 0.0
    fraction = min(max_notional_fraction, target_annual_volatility / vol)
    return equity * max(0.0, fraction)


def stop_distance_quantity(risk_budget: float, entry_price: float, stop_price: float) -> float:
    """Quantity such that a stop-out loses exactly `risk_budget` (before costs)."""
    distance = abs(float(entry_price) - float(stop_price))
    if distance <= 0 or risk_budget <= 0:
        return 0.0
    return float(risk_budget) / distance


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.25 = -25%)."""
    peak, worst = float("-inf"), 0.0
    for value in equity_curve:
        value = float(value)
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe_ratio(period_returns: Sequence[float], periods_per_year: int = 365) -> float:
    """Annualized Sharpe ratio of per-period returns (risk-free rate 0)."""
    values = [float(v) for v in period_returns if math.isfinite(float(v))]
    if len(values) < 2:
        return 0.0
    deviation = statistics.pstdev(values)
    if deviation <= 0:
        return 0.0
    return statistics.mean(values) / deviation * math.sqrt(periods_per_year)


def expectancy_r(r_multiples: Sequence[float]) -> dict:
    """Trade-quality summary in R units: expectancy, win rate, payoff ratio, profit factor."""
    values = [float(v) for v in r_multiples if math.isfinite(float(v))]
    if not values:
        return {"trades": 0, "expectancy_r": 0.0, "win_rate": 0.0, "payoff_ratio": 0.0, "profit_factor": 0.0}
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    average_win = statistics.mean(wins) if wins else 0.0
    average_loss = abs(statistics.mean(losses)) if losses else 0.0
    return {
        "trades": len(values),
        "expectancy_r": statistics.mean(values),
        "win_rate": len(wins) / len(values),
        "payoff_ratio": average_win / average_loss if average_loss else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0),
    }
