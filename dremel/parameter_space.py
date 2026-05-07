"""Parameter space and Dremel fused features."""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from typing import Any, Sequence

SECTION_DB = "[DBOptions]"
SECTION_CF = '[CFOptions "default"]'
SECTION_TABLE = '[TableOptions/BlockBasedTable "default"]'


@dataclass(frozen=True)
class OptionSpec:
    """One Dremel-controlled knob."""

    name: str
    values: tuple[Any, ...]
    section: str | None = None
    cli_flag: str | None = None

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"{self.name} must have at least one value")
        if (self.section is None) == (self.cli_flag is None):
            raise ValueError(f"{self.name} must map to exactly one destination")


MiB = 1024 * 1024

# Finite values are intentionally modest. The controller searches over arms
# formed from these configurations rather than trying to enumerate continuous
# RocksDB domains.
DEFAULT_SPECS: tuple[OptionSpec, ...] = (
    OptionSpec("max_background_jobs", (1, 2, 4, 8), SECTION_DB),
    OptionSpec("level0_file_num_compaction_trigger", (2, 4, 8, 12, 16), SECTION_CF),
    OptionSpec("level0_slowdown_writes_trigger", (6, 8, 12, 16, 20, 24, 28, 32), SECTION_CF),
    OptionSpec("level0_stop_writes_trigger", (10, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80), SECTION_CF),
    OptionSpec("max_bytes_for_level_multiplier", (4, 6, 8, 10, 12, 16), SECTION_CF),
    OptionSpec("max_bytes_for_level_base", tuple(x * MiB for x in (64, 128, 256, 512, 1024, 2048)), SECTION_CF),
    OptionSpec("target_file_size_multiplier", (1, 2, 4, 8), SECTION_CF),
    OptionSpec("target_file_size_base", tuple(x * MiB for x in (32, 64, 128, 256)), SECTION_CF),
    OptionSpec("num_levels", (4, 5, 6, 7, 8, 10), SECTION_CF),
    OptionSpec("max_write_buffer_number", (2, 4, 6, 8), SECTION_CF),
    OptionSpec("write_buffer_size", tuple(x * MiB for x in (32, 48, 64, 80)), SECTION_CF),
    OptionSpec("min_write_buffer_number_to_merge", (1, 2, 4), SECTION_CF),
    OptionSpec("filter_policy", tuple(f"rocksdb.BloomFilter:{bits}:false" for bits in (8, 10, 14, 16)), SECTION_TABLE),
    OptionSpec("block_size", (2048, 4096, 8192, 16384), SECTION_TABLE),
    OptionSpec("cache_size", tuple(x * MiB for x in (384, 512, 768, 1024)), cli_flag="cache_size"),
)


@dataclass(frozen=True)
class DremelConfig:
    """A candidate RocksDB/db_bench configuration."""

    index: int
    params: dict[str, Any]


@dataclass(frozen=True)
class BenchmarkShape:
    """Workload shape used by Dremel's analytical feature estimates."""

    num_keys: float = 100_000_000.0
    key_size_bytes: float = 16.0
    value_size_bytes: float = 1024.0


@dataclass(frozen=True)
class FusedFeatures:
    compaction_frequency: float
    write_buffer_full_frequency: float
    read_sstable_cost: float
    block_cache_hit_rate_proxy: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (
            self.compaction_frequency,
            self.write_buffer_full_frequency,
            self.read_sstable_cost,
            self.block_cache_hit_rate_proxy,
        )


def generate_candidates(
    specs: Sequence[OptionSpec] = DEFAULT_SPECS,
    *,
    memory_budget_bytes: int = 1024 * MiB,
    limit: int | None = None,
) -> list[DremelConfig]:
    """Enumerate Dremel candidates that pass the rule-based filter."""

    candidates: list[DremelConfig] = []
    names = [spec.name for spec in specs]
    for values in itertools.product(*(spec.values for spec in specs)):
        params = dict(zip(names, values))
        if not passes_rules(params, memory_budget_bytes=memory_budget_bytes):
            continue
        candidates.append(DremelConfig(index=len(candidates), params=params))
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def sample_candidates(
    specs: Sequence[OptionSpec] = DEFAULT_SPECS,
    *,
    memory_budget_bytes: int = 1024 * MiB,
    count: int = 512,
    seed: int = 0,
    max_attempts: int | None = None,
) -> list[DremelConfig]:
    """Randomly sample feasible Dremel candidates from the finite space."""

    if count <= 0:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    attempts = max_attempts if max_attempts is not None else count * 100
    candidates: list[DremelConfig] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for _ in range(attempts):
        params = {spec.name: rng.choice(spec.values) for spec in specs}
        key = tuple(sorted((name, str(value)) for name, value in params.items()))
        if key in seen:
            continue
        seen.add(key)
        if not passes_rules(params, memory_budget_bytes=memory_budget_bytes):
            continue
        candidates.append(DremelConfig(index=len(candidates), params=params))
        if len(candidates) >= count:
            break
    if not candidates:
        raise ValueError("no feasible Dremel candidates found")
    return candidates


def passes_rules(params: dict[str, Any], *, memory_budget_bytes: int) -> bool:
    """Apply Dremel's resource and RocksDB-specific feasibility rules."""

    c = int(params["level0_file_num_compaction_trigger"])
    d = int(params["level0_slowdown_writes_trigger"])
    p = int(params["level0_stop_writes_trigger"])
    t = float(params["max_bytes_for_level_multiplier"])
    r = float(params["target_file_size_multiplier"])
    q = int(params["max_write_buffer_number"])
    w = int(params["write_buffer_size"])
    m = int(params["min_write_buffer_number_to_merge"])
    f = int(params["target_file_size_base"])
    s = int(params["max_bytes_for_level_base"])
    o = int(params["cache_size"])

    if not (c < d < p):
        return False
    if not (4 <= t <= 16):
        return False
    if not (t > r):
        return False
    if not (m < q):
        return False

    # Tuning guide approximation: L1 size should be close to L0 capacity.
    l0_size = c * m * w
    if l0_size <= 0 or not (0.5 * l0_size <= s <= 2.0 * l0_size):
        return False

    bloom_bits = _bloom_bits(params.get("filter_policy"))
    estimated_bloom = _estimated_bloom_bytes(params, bloom_bits)
    if q * w + o + estimated_bloom > memory_budget_bytes:
        return False

    if f <= 0 or int(params["num_levels"]) <= 1:
        return False
    return True


DEFAULT_BENCHMARK_SHAPE = BenchmarkShape()


def compute_fused_features(
    params: dict[str, Any],
    benchmark_shape: BenchmarkShape = DEFAULT_BENCHMARK_SHAPE,
    memory_budget_bytes: int = 1024 * MiB,
) -> FusedFeatures:
    """Compute the four compact Dremel features for one candidate."""

    c = float(params["level0_file_num_compaction_trigger"])
    t = float(params["max_bytes_for_level_multiplier"])
    r = float(params["target_file_size_multiplier"])
    f = float(params["target_file_size_base"])
    q = float(params["max_write_buffer_number"])
    w = float(params["write_buffer_size"])
    m = float(params["min_write_buffer_number_to_merge"])
    b = float(params["block_size"])
    num_levels = float(params["num_levels"])
    bg_jobs = int(params["max_background_jobs"])

    level_count = max(1.0, min(num_levels, _estimated_actual_levels(params, benchmark_shape)))
    flush_threads, compaction_threads = background_job_limits(bg_jobs)

    sstable_sum = inverse_sstable_size_sum(f, r, level_count)
    compaction_frequency = (compaction_threads / level_count) * (t + 1.0) * sstable_sum
    write_buffer_full_frequency = (1.0 / (c * m * w)) + (1.0 / (q * w * flush_threads))
    read_sstable_cost = (c + level_count) / b
    block_cache_hit_rate_proxy = max(0.0, float(memory_budget_bytes) - q * w)

    return FusedFeatures(
        compaction_frequency=compaction_frequency,
        write_buffer_full_frequency=write_buffer_full_frequency,
        read_sstable_cost=read_sstable_cost,
        block_cache_hit_rate_proxy=block_cache_hit_rate_proxy,
    )


def option_overrides(
    params: dict[str, Any],
    specs: Sequence[OptionSpec] = DEFAULT_SPECS,
) -> dict[str, dict[str, str]]:
    """Build section -> option -> value overrides for an OPTIONS file."""

    out: dict[str, dict[str, str]] = {}
    spec_by_name = {spec.name: spec for spec in specs}
    for name, value in params.items():
        spec = spec_by_name.get(name)
        if spec is None or spec.section is None:
            continue
        out.setdefault(spec.section, {})[name] = str(value)
    return out


def cli_flags(
    params: dict[str, Any],
    specs: Sequence[OptionSpec] = DEFAULT_SPECS,
) -> list[str]:
    """Build db_bench flags for parameters not represented in OPTIONS files."""

    flags: list[str] = []
    spec_by_name = {spec.name: spec for spec in specs}
    for name, value in params.items():
        spec = spec_by_name.get(name)
        if spec is not None and spec.cli_flag is not None:
            flags.append(f"--{spec.cli_flag}={value}")
    return flags


def background_job_limits(max_background_jobs: int) -> tuple[float, float]:
    """Mirror RocksDB's max_background_jobs split into flush/compaction limits."""

    # Mirrors DBImpl::GetBGJobLimits() in db/db_impl/db_impl_compaction_flush.cc.
    max_flushes = max(1, max_background_jobs // 4)
    max_compactions = max(1, max_background_jobs - max_flushes)
    return float(max_flushes), float(max_compactions)


def inverse_sstable_size_sum(
    target_file_size_base: float,
    target_file_size_multiplier: float,
    level_count: float,
) -> float:
    """Compute sum(1 / (F * R^(i - 1))) for i in [1, level_count]."""

    if target_file_size_multiplier == 1.0:
        return level_count / target_file_size_base
    return (
        (1.0 - target_file_size_multiplier ** (-level_count))
        / (1.0 - (1.0 / target_file_size_multiplier))
        / target_file_size_base
    )


def _estimated_actual_levels(params: dict[str, Any], benchmark_shape: BenchmarkShape) -> float:
    # Paper equation using the benchmark's configured dataset and KV sizes. The
    # value is only used for relative feature placement; clamp callers to the
    # configured num_levels.
    n = benchmark_shape.num_keys
    key_value_bytes = benchmark_shape.key_size_bytes + benchmark_shape.value_size_bytes
    c = float(params["level0_file_num_compaction_trigger"])
    f = float(params["target_file_size_base"])
    t = float(params["max_bytes_for_level_multiplier"])
    numerator = n * key_value_bytes * (t - 1.0)
    denominator = c * f * t
    if numerator <= denominator or t <= 1.0:
        return 1.0
    return max(1.0, math.log(numerator / denominator, t))


def _bloom_bits(filter_policy: Any) -> int:
    if not isinstance(filter_policy, str):
        return 10
    parts = filter_policy.split(":")
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            return 10
    return 10


def _estimated_bloom_bytes(params: dict[str, Any], bloom_bits: int) -> int:
    target_file_size = int(params["target_file_size_base"])
    block_size = int(params["block_size"])
    # Lightweight upper-bound proxy for filter metadata. Exact filter memory
    # depends on live keys and table layout, which are not known pre-run.
    return max(0, (target_file_size // max(1, block_size)) * bloom_bits)

