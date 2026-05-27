"""Parse db_bench output metrics used by Dremel."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HistogramStats:
    mean_us: float | None
    p50_us: float | None
    p99_us: float | None


@dataclass(frozen=True)
class DbBenchMetrics:
    throughput_qps: float | None
    read_mean_us: float | None
    read_p50_us: float | None
    read_p99_us: float | None
    write_mean_us: float | None
    write_p50_us: float | None
    write_p99_us: float | None


def parse_throughput_benchmark(text: str, benchmark: str) -> float | None:
    """Parse aggregate ops/sec from a db_bench benchmark summary line."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(benchmark):
            continue
        parts = stripped.split()
        for i, token in enumerate(parts):
            if token == "ops/sec" and i > 0:
                try:
                    return float(parts[i - 1])
                except ValueError:
                    return None
        return None
    return None


def parse_throughput_readrandomwriterandom(text: str) -> float | None:
    """Parse aggregate ops/sec from a readrandomwriterandom summary line."""
    return parse_throughput_benchmark(text, "readrandomwriterandom")


def parse_throughput_readrandom(text: str) -> float | None:
    """Parse aggregate ops/sec from a readrandom summary line."""
    return parse_throughput_benchmark(text, "readrandom")


_AVERAGE_RE = re.compile(r"Average:\s*([\d.]+)", re.IGNORECASE)
_PERCENTILES_RE = re.compile(r"P50:\s*([\d.]+).*?P99:\s*([\d.]+)", re.IGNORECASE)


def parse_read_write_histograms(
    text: str,
) -> tuple[HistogramStats | None, HistogramStats | None]:
    """Return (read histogram stats, write histogram stats)."""

    read_stats: HistogramStats | None = None
    write_stats: HistogramStats | None = None

    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        lowered = line.lower()
        if lowered.startswith("microseconds per read:") and read_stats is None:
            read_stats = _extract_histogram_from_following_lines(lines, i + 1)
        elif lowered.startswith("microseconds per write:"):
            # Keep last write block seen, matching previous behavior.
            write_stats = _extract_histogram_from_following_lines(lines, i + 1)
        i += 1

    return read_stats, write_stats


def _extract_histogram_from_following_lines(
    lines: list[str], start_idx: int
) -> HistogramStats | None:
    """Scan a histogram block until the next section header."""
    block: list[str] = []
    i = start_idx
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        lowered = stripped.lower()
        if lowered.startswith("microseconds per "):
            break
        block.append(stripped)
        i += 1

    if not block:
        return None

    joined = " ".join(block)
    mean_us: float | None = None
    average_match = _AVERAGE_RE.search(joined)
    if average_match:
        mean_us = float(average_match.group(1))

    p50_us: float | None = None
    p99_us: float | None = None
    percentile_match = _PERCENTILES_RE.search(joined)
    if percentile_match:
        p50_us = float(percentile_match.group(1))
        p99_us = float(percentile_match.group(2))

    if mean_us is None and p50_us is None and p99_us is None:
        return None
    return HistogramStats(mean_us=mean_us, p50_us=p50_us, p99_us=p99_us)


def parse_db_bench_output(text: str) -> DbBenchMetrics:
    # Parses the entire `text` as one blob: no load vs workload phase boundary.
    # Read histograms use the first "Microseconds per read" block; write uses the
    # last "Microseconds per write" match. Throughput uses the first
    # readrandomwriterandom or readrandom summary line. If load and run output are
    # concatenated, metrics may mix phases unless earlier phases omit these patterns.
    read_stats, write_stats = parse_read_write_histograms(text)
    throughput_qps = parse_throughput_readrandomwriterandom(text)
    if throughput_qps is None:
        throughput_qps = parse_throughput_readrandom(text)
    return DbBenchMetrics(
        throughput_qps=throughput_qps,
        read_mean_us=read_stats.mean_us if read_stats else None,
        read_p50_us=read_stats.p50_us if read_stats else None,
        read_p99_us=read_stats.p99_us if read_stats else None,
        write_mean_us=write_stats.mean_us if write_stats else None,
        write_p50_us=write_stats.p50_us if write_stats else None,
        write_p99_us=write_stats.p99_us if write_stats else None,
    )

