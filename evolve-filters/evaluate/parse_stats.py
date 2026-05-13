"""Parsers for RocksDB STATISTICS snippets (Bloom, block cache) and table properties."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterator

_LEVEL_PREFIX_RE = re.compile(r"Level\[(\d+)\]")
_BLOCK_CACHE_TICKERS = frozenset({"rocksdb.block.cache.hit", "rocksdb.block.cache.miss"})
_FILTER_VALUE_RE = re.compile(
    r"(?:rocksdb\.filter\.size|filter_size|filter block size)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def parse_bloom_statistics(text: str) -> Dict[str, float]:
    """
    Parse RocksDB STATISTICS lines and return Bloom-only metrics.

    Returns line-level numeric values for:
    - keys beginning with ``rocksdb.bloom.filter.``
    - ``rocksdb.compute.bits.per.key.micros`` histogram entries
    and also emits derived false-positive metrics for point lookups.
    """
    metrics: Dict[str, float] = {}

    for line in _iter_statistics_lines(text):
        parsed = _parse_stats_line(line)
        if not parsed:
            continue
        base_name, values = parsed
        lowered = base_name.lower()
        if not (
            lowered.startswith("rocksdb.bloom.filter.")
            or lowered == "rocksdb.compute.bits.per.key.micros"
        ):
            continue
        for suffix, value in values.items():
            metrics[f"{lowered}.{suffix.lower()}"] = value

    full_positive = metrics.get("rocksdb.bloom.filter.full.positive.count", 0.0)
    full_true_positive = metrics.get(
        "rocksdb.bloom.filter.full.true.positive.count", 0.0
    )
    full_useful = metrics.get("rocksdb.bloom.filter.useful.count", 0.0)
    false_positives = max(0.0, full_positive - full_true_positive)
    full_negative_queries = false_positives + full_useful
    observed_fp_rate = 0.0
    if full_negative_queries > 0:
        observed_fp_rate = false_positives / full_negative_queries

    metrics["bloom.full.false_positive.count"] = false_positives
    metrics["bloom.full.observed_fp_rate"] = observed_fp_rate

    return metrics


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


@dataclass(frozen=True)
class LiveSstFilterBytes:
    """Table-property filter size totals from ``db_bench`` / RocksDB textual output."""

    per_level_bytes: dict[int, float]
    """Most recent parsed filter size bytes for each ``Level[N]`` line."""

    global_bytes: float | None
    """Most recent parsed value from a non-level line (``rocksdb.filter.size`` and synonyms)."""

    def aggregated_bytes(self) -> float:
        """
        Same decision order as the historical single-float helper:

        1) Sum of per-level values when any ``Level[N]`` line was seen.
        2) Else the latest global line value.
        3) Else ``0.0``.
        """
        if self.per_level_bytes:
            return float(sum(self.per_level_bytes.values()))
        if self.global_bytes is not None:
            return float(self.global_bytes)
        return 0.0


def parse_live_sst_filter_bytes(text: str) -> LiveSstFilterBytes:
    """
    Parse table-property filter sizes: per-level, global tail value, and aggregate.

    Matches **table property** labels (``TablePropertiesNames::kFilterSize`` /
    ``rocksdb.filter.size``). In-tree ``db_bench`` prints ``Level[N]:`` lines
    when ``--show_table_properties`` is set.

    Parses every matching line in order so later rows overwrite earlier ones within
    the same category (same level index, or global).
    """
    level_values: dict[int, float] = {}
    global_last: float | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        value = _parse_filter_size_from_line(line)
        if value is None:
            continue

        level_match = _LEVEL_PREFIX_RE.search(line)
        if level_match:
            level_values[int(level_match.group(1))] = value
        else:
            global_last = value

    return LiveSstFilterBytes(
        per_level_bytes=dict(level_values),
        global_bytes=global_last,
    )


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


def _parse_filter_size_from_line(line: str) -> float | None:
    match = _FILTER_VALUE_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None

