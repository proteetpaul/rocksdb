#!/usr/bin/env python3
"""
Draw Latin Hypercube samples over the default RocksDB option search space and
write them to a JSON file (Linux, repo checkout).

Usage (from RocksDB repo root):

  python3 bench/LHS/sample_lhs_configs.py --n 32 --seed 0 --out bench/LHS/samples.json
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
    _ensure_repo_on_path()
    from bench.LHS.lhs_core import draw_samples, write_samples_file
    from bench.LHS.parameter_space import SEARCH_SPACE

    p = argparse.ArgumentParser(description="LHS sample RocksDB option configs to JSON.")
    p.add_argument("--n", type=int, required=True, help="Number of LHS samples")
    p.add_argument("--seed", type=int, default=0, help="RNG seed")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("bench/LHS/samples.json"),
        help="Output JSON path",
    )
    args = p.parse_args()
    if args.n <= 0:
        print("--n must be positive", file=sys.stderr)
        return 1

    specs = list(SEARCH_SPACE)
    samples = draw_samples(specs, args.n, args.seed)
    write_samples_file(args.out, specs, samples, args.seed)
    print(f"Wrote {len(samples)} samples to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
