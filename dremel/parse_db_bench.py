"""Parse db_bench output metrics used by Dremel."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DbBenchMetrics:
    throughput_qps: float | None
    read_p50_us: float | None
    read_p99_us: float | None
    write_p50_us: float | None
    write_p99_us: float | None


def parse_throughput_readrandomwriterandom(text: str) -> float | None:
    """Parse aggregate ops/sec from a readrandomwriterandom summary line."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("readrandomwriterandom"):
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


_PERCENTILES_RE = re.compile(r"P50:\s*([\d.]+).*?P99:\s*([\d.]+)", re.IGNORECASE)


def parse_read_write_histograms(text: str) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Return ((read_p50, read_p99), (write_p50, write_p99))."""

    read_pair: tuple[float, float] | None = None
    write_pair: tuple[float, float] | None = None

    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        lowered = line.lower()
        if lowered.startswith("microseconds per read:") and read_pair is None:
            read_pair = _extract_percentiles_from_following_lines(lines, i + 1)
        elif lowered.startswith("microseconds per write:"):
            # Keep last write block seen, matching previous behavior.
            write_pair = _extract_percentiles_from_following_lines(lines, i + 1)
        i += 1

    return read_pair, write_pair


def _extract_percentiles_from_following_lines(
    lines: list[str], start_idx: int
) -> tuple[float, float] | None:
    """Scan a histogram block until the next section header and parse P50/P99."""
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
    percentile_match = _PERCENTILES_RE.search(" ".join(block))
    if not percentile_match:
        return None
    return (float(percentile_match.group(1)), float(percentile_match.group(2)))


def parse_db_bench_output(text: str) -> DbBenchMetrics:
    # Parses the entire `text` as one blob: no load vs workload phase boundary.
    # Read histograms use the first "Microseconds per read" block; write uses the
    # last "Microseconds per write" match. Throughput uses the first
    # readrandomwriterandom summary line. If load and run output are concatenated,
    # metrics may mix phases unless earlier phases omit these patterns.
    read_pair, write_pair = parse_read_write_histograms(text)
    return DbBenchMetrics(
        throughput_qps=parse_throughput_readrandomwriterandom(text),
        read_p50_us=read_pair[0] if read_pair else None,
        read_p99_us=read_pair[1] if read_pair else None,
        write_p50_us=write_pair[0] if write_pair else None,
        write_p99_us=write_pair[1] if write_pair else None,
    )

