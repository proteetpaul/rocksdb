"""Parsers for Bloom-focused RocksDB STATISTICS and table properties output."""

from __future__ import annotations

import re
from typing import Dict

_LEVEL_PREFIX_RE = re.compile(r"Level\[(\d+)\]")
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


def parse_live_sst_filter_bytes(text: str) -> float:
    """
    Parse text output and estimate current total live SST filter bytes.

    Matches **table property** labels (``TablePropertiesNames::kFilterSize`` /
    ``rocksdb.filter.size``). In-tree ``db_bench`` prints the same ``Level[N]:``
    lines once at end of each benchmark when ``--show_table_properties`` is
    set (and still may print them during periodic stats if enabled). This is
    not the final ``STATISTICS:`` ticker dump (that block is from
    ``statistics.h`` only).

    Preference order:
    1) Sum of the latest per-level values from lines with ``Level[N]`` prefixes.
    2) The latest global line matching ``rocksdb.filter.size``, ``filter_size``,
       or ``filter block size`` (as formatted in table property output).
    """
    level_values: dict[int, float] = {}
    global_values: list[float] = []

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
            global_values.append(value)

    if level_values:
        return float(sum(level_values.values()))
    if global_values:
        return float(global_values[-1])
    return 0.0


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

