"""Parsers for RocksDB STATISTICS snippets (block cache, secondary cache tiers)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Dict, Iterator, List

_CACHE_TIER_CONTROLLER_ADJUSTED_RE = re.compile(
    r"Cache tier controller: adjusted secondary ratio from "
    r"([0-9.]+) to ([0-9.]+)"
)
_CACHE_TIER_CONTROLLER_FAILED_RE = re.compile(
    r"Cache tier controller: failed to adjust secondary ratio "
    r"from ([0-9.]+) to ([0-9.]+): (.*)"
)

_BLOCK_CACHE_TICKERS = frozenset({"rocksdb.block.cache.hit", "rocksdb.block.cache.miss"})
_CACHE_TIER_TICKERS = frozenset(
    {
        "rocksdb.secondary.cache.hits",
        "rocksdb.compressed.secondary.cache.hits",
        "rocksdb.compressed.secondary.cache.dummy.hits",
    }
)
_READ_BLOCK_GET_HISTOGRAM = "rocksdb.read.block.get.micros"
_READ_BLOCK_GET_HISTOGRAM_SUFFIXES = frozenset({"p50", "p99"})


def parse_block_cache_statistics(text: str) -> Dict[str, float]:
    """
    Parse RocksDB STATISTICS ticker lines for aggregate block cache hit/miss.

    Matches ``rocksdb.block.cache.hit`` / ``rocksdb.block.cache.miss`` from
    ``Statistics::ToString()`` (see ``TickersNameMap``).
    """
    metrics: Dict[str, float] = {}
    for line in _iter_statistics_lines(text):
        parsed = _parse_stats_line(line)
        if not parsed:
            continue
        base_name, values = parsed
        lowered = base_name.lower()
        if lowered not in _BLOCK_CACHE_TICKERS:
            continue
        for suffix, value in values.items():
            metrics[f"{lowered}.{suffix.lower()}"] = value
    return metrics


def parse_cache_tier_statistics(text: str) -> Dict[str, float]:
    """
    Parse secondary / compressed-secondary cache ticker lines from STATISTICS output.
    """
    metrics: Dict[str, float] = {}
    for line in _iter_statistics_lines(text):
        parsed = _parse_stats_line(line)
        if not parsed:
            continue
        base_name, values = parsed
        lowered = base_name.lower()
        if lowered not in _CACHE_TIER_TICKERS:
            continue
        for suffix, value in values.items():
            metrics[f"{lowered}.{suffix.lower()}"] = value
    return metrics


def parse_read_block_get_histogram(text: str) -> Dict[str, float]:
    """
    Parse cumulative READ_BLOCK_GET_MICROS histogram from STATISTICS output.

    Proxies block-cache-miss SST read latency (see cache_tier_memory_controller.cc).
    """
    metrics: Dict[str, float] = {}
    for line in _iter_statistics_lines(text):
        parsed = _parse_stats_line(line)
        if not parsed:
            continue
        base_name, values = parsed
        if base_name.lower() != _READ_BLOCK_GET_HISTOGRAM:
            continue
        for suffix, value in values.items():
            lowered_suffix = suffix.lower()
            if lowered_suffix not in _READ_BLOCK_GET_HISTOGRAM_SUFFIXES:
                continue
            metrics[f"{_READ_BLOCK_GET_HISTOGRAM}.{lowered_suffix}"] = value
    return metrics


@dataclass(frozen=True)
class CacheTierControllerLogEntry:
    level: str
    from_ratio: float
    to_ratio: float
    message: str
    error: str | None = None


def parse_cache_tier_controller_logs(text: str) -> List[CacheTierControllerLogEntry]:
    """
    Parse ``CacheTierMemoryController::MaybeAdjust`` info/warn log lines.

    Matches RocksDB info-log output (file or stderr) from
    ``cache/cache_tier_memory_controller.cc``.
    """
    entries: List[CacheTierControllerLogEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "Cache tier controller:" not in line:
            continue
        failed = _CACHE_TIER_CONTROLLER_FAILED_RE.search(line)
        if failed is not None:
            entries.append(
                CacheTierControllerLogEntry(
                    level="warn",
                    from_ratio=float(failed.group(1)),
                    to_ratio=float(failed.group(2)),
                    message=line,
                    error=failed.group(3).strip(),
                )
            )
            continue
        adjusted = _CACHE_TIER_CONTROLLER_ADJUSTED_RE.search(line)
        if adjusted is not None:
            entries.append(
                CacheTierControllerLogEntry(
                    level="info",
                    from_ratio=float(adjusted.group(1)),
                    to_ratio=float(adjusted.group(2)),
                    message=line,
                )
            )
    return entries


def controller_log_artifacts(entries: List[CacheTierControllerLogEntry]) -> Dict[str, str]:
    """Build OpenEvolve artifact strings from parsed controller log entries."""
    adjustments = [entry for entry in entries if entry.level == "info"]
    failures = [entry for entry in entries if entry.level == "warn"]
    payload = {
        "controller_log": "\n".join(entry.message for entry in entries),
        "controller_adjustments": json.dumps([asdict(entry) for entry in entries]),
        "controller_adjustment_count": str(len(adjustments)),
        "controller_adjustment_failures": str(len(failures)),
    }
    if adjustments:
        payload["controller_final_secondary_ratio"] = str(adjustments[-1].to_ratio)
    else:
        payload["controller_final_secondary_ratio"] = ""
    return payload


def derive_cache_hit_rate_metrics(
    block_cache_stats: Dict[str, float],
    cache_tier_stats: Dict[str, float],
) -> Dict[str, float]:
    """
    Derive hit rates from cumulative ticker counts at end of workload.

    ``rocksdb.block.cache.hit`` counts primary and secondary successes;
    ``rocksdb.block.cache.miss`` counts disk reads only. ``primary_cache_hit_rate``
    is primary-tier only: (hits - secondary_hits) / (hits + misses).
    ``secondary_cache_hit_rate`` is secondary_hits / (secondary_hits + misses).
    """
    hits = block_cache_stats.get("rocksdb.block.cache.hit.count", 0.0)
    misses = block_cache_stats.get("rocksdb.block.cache.miss.count", 0.0)
    lookups = hits + misses
    sec_hits = cache_tier_stats.get("rocksdb.secondary.cache.hits.count", 0.0)
    primary_hits = max(0.0, hits - sec_hits)
    block_hit_rate = primary_hits / lookups if lookups > 0.0 else 0.0

    primary_misses = sec_hits + misses
    secondary_hit_rate = 0.0
    if primary_misses > 0.0:
        secondary_hit_rate = sec_hits / primary_misses

    return {
        "primary_cache_hit_rate": block_hit_rate,
        "secondary_cache_hit_rate": secondary_hit_rate,
    }


def _iter_statistics_lines(text: str) -> Iterator[str]:
    """Yield stripped non-empty lines after the first ``STATISTICS:`` header."""
    in_stats_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "STATISTICS:" in line.upper():
            in_stats_block = True
            continue
        if not in_stats_block:
            continue
        yield line


def _parse_stats_line(line: str) -> tuple[str, Dict[str, float]] | None:
    tokens = line.split()
    if len(tokens) < 4 or ":" not in tokens:
        return None
    base_name = tokens[0]
    values: Dict[str, float] = {}

    index = 1
    while index + 2 < len(tokens):
        key = tokens[index]
        separator = tokens[index + 1]
        value_token = tokens[index + 2]
        if separator != ":":
            index += 1
            continue
        try:
            values[key.lower()] = float(value_token)
        except ValueError:
            pass
        index += 3

    if not values:
        return None
    return base_name, values
