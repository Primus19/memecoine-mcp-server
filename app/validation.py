from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: tuple[str, ...]
    sample_size: int
    mean_return: float
    standard_error: float
    lower_confidence_bound: float
    stressed_mean_return: float


def promotion_gate(returns: Sequence[float], *, minimum_samples: int = 100,
                   confidence_z: float = 1.96, cost_stress: float = 0.0) -> PromotionDecision:
    """Fail-closed gate for shadow challengers. Returns and cost stress use the same units."""
    values = [float(value) for value in returns if math.isfinite(float(value))]
    reasons: list[str] = []
    if len(values) < minimum_samples:
        reasons.append(f"sample size below {minimum_samples}")
    mean = statistics.mean(values) if values else 0.0
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    standard_error = deviation / math.sqrt(len(values)) if values else float("inf")
    lower = mean - confidence_z * standard_error if math.isfinite(standard_error) else float("-inf")
    stressed = mean - abs(float(cost_stress))
    if lower <= 0:
        reasons.append("lower confidence bound is not positive")
    if stressed <= 0:
        reasons.append("cost-stressed mean is not positive")
    return PromotionDecision(not reasons, tuple(reasons), len(values), mean, standard_error, lower, stressed)


def walk_forward_splits(length: int, *, train: int, test: int, embargo: int = 0) -> list[tuple[range, range]]:
    """Create chronological, non-overlapping test windows with an optional embargo."""
    if min(length, train, test) <= 0 or embargo < 0:
        return []
    splits: list[tuple[range, range]] = []
    train_end = train
    while train_end + embargo + test <= length:
        splits.append((range(0, train_end), range(train_end + embargo, train_end + embargo + test)))
        train_end += test
    return splits
