"""Parsers for RocksDB STATISTICS snippets (block cache, secondary cache tiers)."""

from __future__ import annotations

from typing import Dict, Iterator

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


def derive_cache_hit_rate_metrics(
    block_cache_stats: Dict[str, float],
    cache_tier_stats: Dict[str, float],
) -> Dict[str, float]:
    """
    Derive aggregate hit rates from cumulative ticker counts at end of workload.

    ``block_cache_hit_rate`` uses primary block cache hits / (hits + misses).
    ``secondary_cache_hit_rate`` uses ``SECONDARY_CACHE_HITS`` /
    primary misses when misses > 0 (aligned with controller interval semantics).
    """
    hits = block_cache_stats.get("rocksdb.block.cache.hit.count", 0.0)
    misses = block_cache_stats.get("rocksdb.block.cache.miss.count", 0.0)
    lookups = hits + misses
    block_hit_rate = hits / lookups if lookups > 0.0 else 0.0

    sec_hits = cache_tier_stats.get("rocksdb.secondary.cache.hits.count", 0.0)
    secondary_hit_rate = 0.0
    if misses > 0.0:
        secondary_hit_rate = sec_hits / misses

    return {
        "block_cache_hit_rate": block_hit_rate,
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
