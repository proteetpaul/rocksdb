#!/usr/bin/env python3
"""
Run db_bench experiments for each LHS sample in a JSON file (Linux only).

Usage (from RocksDB repo root, after building db_bench):

  python3 bench/LHS/run_lhs_experiments.py \\
    --samples bench/LHS/samples.json \\
    --results-csv bench/LHS/results.csv

Resume: skips indices <= bench/LHS/last_success_index.txt unless --force.

Each db_bench run uses systemd cgroups MemoryMax (see --memory-limit).
"""

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


def main() -> int:
    repo = _ensure_repo_on_path()
    from bench.LHS.experiment_runner import run_all_samples

    bench_default = repo / "bench"
    p = argparse.ArgumentParser(description="Run LHS db_bench experiments from samples JSON.")
    p.add_argument("--samples", type=Path, default=bench_default / "LHS" / "samples.json")
    p.add_argument("--bench-ini", type=Path, default=bench_default / "bench.ini")
    p.add_argument("--base-options", type=Path, default=bench_default / "rocksdb_options.ini")
    p.add_argument("--workload-dir", type=Path, default=bench_default / "workloads")
    p.add_argument("--results-csv", type=Path, default=bench_default / "LHS" / "results.csv")
    p.add_argument(
        "--last-success",
        type=Path,
        default=bench_default / "LHS" / "last_success_index.txt",
    )
    p.add_argument("--logs-dir", type=Path, default=bench_default / "LHS" / "logs")
    p.add_argument("--db-bench", type=Path, default=None, help="Path to db_bench binary")
    p.add_argument("--force", action="store_true", help="Ignore last_success resume file")
    p.add_argument("--start-index", type=int, default=None, help="Only run sample_index >= this")
    p.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Workload A duration in seconds (default 300)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Subprocess timeout per db_bench invocation (seconds); default unlimited",
    )
    p.add_argument(
        "--memory-limit",
        type=str,
        default=None,
        help="Cgroup memory cap per db_bench (e.g. 8G, 512M, or bytes). "
        "Default: env LHS_MEMORY_LIMIT if set, else 32G. Passed to systemd-run MemoryMax.",
    )
    args = p.parse_args()

    run_all_samples(
        samples_path=args.samples.resolve(),
        bench_ini=args.bench_ini.resolve(),
        base_options=args.base_options.resolve(),
        workload_dir=args.workload_dir.resolve(),
        results_csv=args.results_csv.resolve(),
        last_success_path=args.last_success.resolve(),
        logs_dir=args.logs_dir.resolve(),
        db_bench=args.db_bench.resolve() if args.db_bench else None,
        force=args.force,
        start_index=args.start_index,
        workload_duration_sec=args.duration,
        subprocess_timeout_sec=args.timeout,
        memory_limit=args.memory_limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
