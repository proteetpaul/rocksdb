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


_PERCENTILES_RE = re.compile(
    r"P50:\s*([\d.]+).*?P99:\s*([\d.]+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_read_write_histograms(text: str) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Return ((read_p50, read_p99), (write_p50, write_p99))."""

    read_pair: tuple[float, float] | None = None
    write_pair: tuple[float, float] | None = None

    read_match = re.search(
        r"Microseconds per read:\s*(.*?)(?=Microseconds per write:|Microseconds per |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if read_match:
        percentile_match = _PERCENTILES_RE.search(read_match.group(1))
        if percentile_match:
            read_pair = (float(percentile_match.group(1)), float(percentile_match.group(2)))

    for write_match in re.finditer(
        r"Microseconds per write:\s*(.*?)(?=Microseconds per |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        percentile_match = _PERCENTILES_RE.search(write_match.group(1))
        if percentile_match:
            write_pair = (float(percentile_match.group(1)), float(percentile_match.group(2)))

    return read_pair, write_pair


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

