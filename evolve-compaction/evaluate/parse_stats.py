"""Parsers for compaction-focused RocksDB STATISTICS output."""

from __future__ import annotations

from typing import Dict


def parse_compaction_statistics(text: str) -> Dict[str, float]:
    """
    Parse RocksDB statistics lines and return compaction-only metrics.

    The parser keeps only keys whose base metric name contains
    ``compact`` or ``compaction`` and emits lower-cased metric keys.
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
        if "compact" not in lowered and "compaction" not in lowered:
            continue
        for suffix, value in values.items():
            metrics[f"{lowered}.{suffix.lower()}"] = value
    return metrics


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
