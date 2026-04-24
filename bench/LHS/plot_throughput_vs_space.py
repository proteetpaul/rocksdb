#!/usr/bin/env python3
"""Scatter plot: throughput vs space utilization from LHS results CSV.

Usage:
  python3 bench/LHS/plot_throughput_vs_space.py \
    --csv bench/LHS/results.csv \
    --output bench/LHS/throughput_vs_space.png
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGTP]?)(i?B)?\s*$", re.IGNORECASE)
_MULT = {
    "": 1.0,
    "K": 1024.0,
    "M": 1024.0**2,
    "G": 1024.0**3,
    "T": 1024.0**4,
    "P": 1024.0**5,
}


def parse_size_to_gib(s: str) -> float | None:
    """Parse du -sh style value like 900M, 1.2G, 1024 into GiB."""
    m = _SIZE_RE.match((s or "").strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "").upper()
    bytes_val = value * _MULT.get(unit, 1.0)
    return bytes_val / (1024.0**3)


def load_points(csv_path: Path, ok_only: bool) -> tuple[list[float], list[float], list[int]]:
    x_space_gib: list[float] = []
    y_throughput: list[float] = []
    sample_ids: list[int] = []

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if ok_only and row.get("status", "") != "ok":
                continue
            try:
                throughput = float(row["throughput_qps"])
                sid = int(row.get("sample_index", len(sample_ids)))
            except (KeyError, ValueError, TypeError):
                continue
            space = parse_size_to_gib(row.get("db_disk_du_sh", ""))
            if space is None:
                continue
            x_space_gib.append(space)
            y_throughput.append(throughput)
            sample_ids.append(sid)

    return x_space_gib, y_throughput, sample_ids


def main() -> int:
    p = argparse.ArgumentParser(description="Plot throughput vs space from LHS results.csv")
    p.add_argument("--csv", type=Path, default=Path("bench/LHS/results.csv"), help="Input results CSV")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("bench/LHS/throughput_vs_space.png"),
        help="Output PNG path",
    )
    p.add_argument(
        "--include-failed",
        action="store_true",
        help="Include non-ok rows when throughput/space values are present",
    )
    p.add_argument(
        "--annotate",
        action="store_true",
        help="Annotate points with sample_index",
    )
    args = p.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required: pip install matplotlib", file=sys.stderr)
        return 1

    x_space, y_tp, sample_ids = load_points(args.csv, ok_only=not args.include_failed)
    if not x_space:
        print("No valid (db_disk_du_sh, throughput_qps) rows found in CSV", file=sys.stderr)
        return 1

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(x_space, y_tp, alpha=0.8, s=22)
    ax.set_xlabel("Space utilization (GiB, from db_disk_du_sh)")
    ax.set_ylabel("Throughput (ops/sec)")
    ax.set_title("Throughput vs Space Utilization")
    ax.grid(True, alpha=0.3)

    if args.annotate:
        for x, y, sid in zip(x_space, y_tp, sample_ids):
            ax.annotate(str(sid), (x, y), fontsize=7, alpha=0.75)

    fig.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=150)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
