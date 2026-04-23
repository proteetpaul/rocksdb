"""Parse db_bench stdout for readrandomwriterandom throughput and histograms."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DbBenchMetrics:
    throughput_qps: float | None
    read_p50_us: float | None
    read_p99_us: float | None
    write_p50_us: float | None
    write_p99_us: float | None


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

    m_write = re.search(
        r"Microseconds per write:\s*(.*?)(?=Microseconds per |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m_write:
        pm = _percentiles_re.search(m_write.group(1))
        if pm:
            write_pair = (float(pm.group(1)), float(pm.group(2)))

    return read_pair, write_pair


def parse_db_bench_output(text: str) -> DbBenchMetrics:
    tp = parse_throughput_readrandomwriterandom(text)
    rp, wp = parse_read_write_histograms(text)
    return DbBenchMetrics(
        throughput_qps=tp,
        read_p50_us=rp[0] if rp else None,
        read_p99_us=rp[1] if rp else None,
        write_p50_us=wp[0] if wp else None,
        write_p99_us=wp[1] if wp else None,
    )


def parse_du_sh_stdout(stdout: str) -> str | None:
    """First field of `du -sh` output (human size), e.g. '1.2G'."""
    line = stdout.strip().splitlines()
    if not line:
        return None
    parts = line[0].split(None, 1)
    return parts[0] if parts else None
