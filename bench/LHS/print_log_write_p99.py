#!/usr/bin/env python3
"""Parse .log files and print write P99 latency (microseconds)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_repo_on_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    return root


def _iter_logs(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix == ".log" else []
    return sorted(p for p in path.rglob("*.log") if p.is_file())


def main() -> int:
    _ensure_repo_on_path()
    from bench.LHS.parse_db_bench import parse_read_write_histograms

    parser = argparse.ArgumentParser(
        description="Parse all .log files and print write p99 latency (us)."
    )
    parser.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path("bench/LHS/logs"),
        help="Log file or directory to scan (default: bench/LHS/logs)",
    )
    args = parser.parse_args()

    target = args.target.expanduser().resolve()
    logs = _iter_logs(target)
    if not logs:
        print(f"No .log files found under: {target}", file=sys.stderr)
        return 1

    for log_path in logs:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        _read_pair, write_pair = parse_read_write_histograms(text)
        write_p99 = write_pair[1] if write_pair else None
        value = "NA" if write_p99 is None else f"{write_p99:g}"
        print(f"{log_path}\t{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
