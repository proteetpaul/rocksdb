"""Bandit arms, quantile bucketing, successive halving, and UCB sampling."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .parameter_space import (
    DEFAULT_BENCHMARK_SHAPE,
    BenchmarkShape,
    DremelConfig,
    FusedFeatures,
    compute_fused_features,
)


@dataclass
class Arm:
    key: tuple[int, int, int, int]
    configs: list[DremelConfig]
    rewards: list[float] = field(default_factory=list)

    def mean_reward(self) -> float:
        return sum(self.rewards) / len(self.rewards) if self.rewards else 0.0

    def stddev_reward(self, fallback: float) -> float:
        if len(self.rewards) < 2:
            return fallback
        mean = self.mean_reward()
        variance = sum((x - mean) ** 2 for x in self.rewards) / len(self.rewards)
        return math.sqrt(variance)

    def ucb(self, fallback_stddev: float) -> float:
        return self.mean_reward() + self.stddev_reward(fallback_stddev)


@dataclass(frozen=True)
class EvaluationResult:
    config: DremelConfig
    reward: float
    duration_sec: int
    status: str = "ok"


def build_arms(
    configs: Sequence[DremelConfig],
    *,
    buckets_per_feature: int = 2,
    benchmark_shape: BenchmarkShape = DEFAULT_BENCHMARK_SHAPE,
    memory_budget_bytes: int = 1024 * 1024 * 1024,
) -> list[Arm]:
    """Bucket configurations by fused features and return one arm per bucket."""

    if buckets_per_feature <= 0:
        raise ValueError("buckets_per_feature must be positive")
    if not configs:
        return []

    features = {
        config.index: compute_fused_features(
            config.params, benchmark_shape, memory_budget_bytes
        )
        for config in configs
    }
    thresholds = _quantile_thresholds(features.values(), buckets_per_feature)
    by_key: dict[tuple[int, int, int, int], list[DremelConfig]] = {}
    for config in configs:
        key = _bucket_key(features[config.index], thresholds)
        by_key.setdefault(key, []).append(config)
    return [Arm(key=key, configs=items) for key, items in sorted(by_key.items())]


def heuristic_config(
    arm: Arm,
    benchmark_shape: BenchmarkShape = DEFAULT_BENCHMARK_SHAPE,
    memory_budget_bytes: int = 1024 * 1024 * 1024,
) -> DremelConfig:
    """Choose the config with the smallest write-buffer-full frequency."""

    return min(
        arm.configs,
        key=lambda config: compute_fused_features(
            config.params, benchmark_shape, memory_budget_bytes
        ).write_buffer_full_frequency,
    )


def successive_halving(
    configs: Sequence[DremelConfig],
    time_points_sec: Sequence[int],
    evaluate: Callable[[DremelConfig, int], EvaluationResult],
) -> list[EvaluationResult]:
    """Evaluate candidates at increasing fidelities, dropping the lower half."""

    active = list(configs)
    all_results: list[EvaluationResult] = []
    for duration in time_points_sec:
        round_results = [evaluate(config, duration) for config in active]
        all_results.extend(round_results)
        successful = [result for result in round_results if result.status == "ok"]
        if not successful:
            break
        keep = max(1, math.ceil(len(successful) / 2))
        successful.sort(key=lambda result: result.reward, reverse=True)
        active = [result.config for result in successful[:keep]]
        if len(active) <= 1:
            break
    return all_results


def select_ucb_arms(
    arms: Sequence[Arm],
    *,
    top_k: int,
) -> list[Arm]:
    """Rank arms by mean + stddev and return the top arms."""

    if top_k <= 0:
        return []
    all_rewards = [reward for arm in arms for reward in arm.rewards]
    fallback = _stddev(all_rewards) if len(all_rewards) >= 2 else 0.0
    return sorted(arms, key=lambda arm: arm.ucb(fallback), reverse=True)[:top_k]


def sample_untried_config(arm: Arm, tried_indices: Iterable[int], rng: random.Random) -> DremelConfig | None:
    tried = set(tried_indices)
    choices = [config for config in arm.configs if config.index not in tried]
    if not choices:
        return None
    return rng.choice(choices)


def _quantile_thresholds(
    features: Iterable[FusedFeatures],
    buckets_per_feature: int,
) -> list[list[float]]:
    columns = list(zip(*(feature.as_tuple() for feature in features)))
    thresholds: list[list[float]] = []
    for column in columns:
        ordered = sorted(column)
        feature_thresholds: list[float] = []
        for bucket in range(1, buckets_per_feature):
            idx = min(len(ordered) - 1, math.ceil(len(ordered) * bucket / buckets_per_feature) - 1)
            feature_thresholds.append(ordered[idx])
        thresholds.append(feature_thresholds)
    return thresholds


def _bucket_key(features: FusedFeatures, thresholds: list[list[float]]) -> tuple[int, int, int, int]:
    key = []
    for value, feature_thresholds in zip(features.as_tuple(), thresholds):
        bucket = 0
        while bucket < len(feature_thresholds) and value > feature_thresholds[bucket]:
            bucket += 1
        key.append(bucket)
    return tuple(key)  # type: ignore[return-value]


def _stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

