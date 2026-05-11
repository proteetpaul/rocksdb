"""Parse db_bench stdout for readrandomwriterandom throughput and histograms."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class DbBenchMetrics:
    throughput_qps: float | None
    read_p50_us: float | None
    read_p99_us: float | None
    write_p50_us: float | None
    write_p99_us: float | None
    statistics: dict[str, dict[str, Any]]


def parse_throughput_readrandomwriterandom(text: str) -> float | None:
    """Parse aggregate ops/sec from the summary line for readrandomwriterandom."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("readrandomwriterandom"):
            parts = s.split()
            for i, tok in enumerate(parts):
                if tok == "ops/sec" and i > 0:
                    try:
                        return float(parts[i - 1])
                    except ValueError:
                        return None
            return None
    return None


_percentiles_re = re.compile(
    r"P50:\s*([\d.]+).*?P99:\s*([\d.]+)",
    re.IGNORECASE | re.DOTALL,
)

_statistics_ticker_re = re.compile(
    r"^(rocksdb\.[a-z0-9._-]+)\s+COUNT\s*:\s*([0-9]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_statistics_histogram_re = re.compile(
    r"^(rocksdb\.[a-z0-9._-]+)\s+P50\s*:\s*([0-9.eE+-]+)\s+P95\s*:\s*([0-9.eE+-]+)\s+"
    r"P99\s*:\s*([0-9.eE+-]+)\s+P100\s*:\s*([0-9.eE+-]+)\s+COUNT\s*:\s*([0-9]+)\s+"
    r"SUM\s*:\s*([0-9]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_read_write_histograms(text: str) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """
    Return ((read_p50, read_p99), (write_p50, write_p99)) using Microseconds per
    read/write sections. Each inner tuple may be None if missing.
    """
    read_pair: tuple[float, float] | None = None
    write_pair: tuple[float, float] | None = None

    m_read = re.search(
        r"Microseconds per read:\s*(.*?)(?=Microseconds per write:|Microseconds per |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m_read:
        pm = _percentiles_re.search(m_read.group(1))
        if pm:
            read_pair = (float(pm.group(1)), float(pm.group(2)))

    m_write = None
    for m in re.finditer(
        r"Microseconds per write:\s*(.*?)(?=Microseconds per |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        m_write = m
    if m_write:
        pm = _percentiles_re.search(m_write.group(1))
        if pm:
            write_pair = (float(pm.group(1)), float(pm.group(2)))

    return read_pair, write_pair


def parse_rocksdb_statistics(text: str) -> dict[str, dict[str, Any]]:
    """Parse all RocksDB statistics lines from db_bench output."""
    parsed: dict[str, dict[str, Any]] = {}
    for m in _statistics_ticker_re.finditer(text):
        name = m.group(1).lower()
        parsed[name] = {
            "kind": "ticker",
            "count": int(m.group(2)),
        }

    for m in _statistics_histogram_re.finditer(text):
        name = m.group(1).lower()
        parsed[name] = {
            "kind": "histogram",
            "p50": float(m.group(2)),
            "p95": float(m.group(3)),
            "p99": float(m.group(4)),
            "p100": float(m.group(5)),
            "count": int(m.group(6)),
            "sum": int(m.group(7)),
        }
    return parsed


def parse_db_bench_output(text: str) -> DbBenchMetrics:
    tp = parse_throughput_readrandomwriterandom(text)
    rp, wp = parse_read_write_histograms(text)
    stats = parse_rocksdb_statistics(text)
    return DbBenchMetrics(
        throughput_qps=tp,
        read_p50_us=rp[0] if rp else None,
        read_p99_us=rp[1] if rp else None,
        write_p50_us=wp[0] if wp else None,
        write_p99_us=wp[1] if wp else None,
        statistics=stats,
    )


def parse_du_sh_stdout(stdout: str) -> str | None:
    """First field of `du -sh` output (human size), e.g. '1.2G'."""
    line = stdout.strip().splitlines()
    if not line:
        return None
    parts = line[0].split(None, 1)
    return parts[0] if parts else None
