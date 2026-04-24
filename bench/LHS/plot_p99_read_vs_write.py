#!/usr/bin/env python3
"""Scatter plot: p99 read latency vs p99 write latency from LHS results CSV.

Usage:
  python3 bench/LHS/plot_p99_read_vs_write.py \
    --csv bench/LHS/results.csv \
    --output bench/LHS/p99_read_vs_write.png
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def load_points(csv_path: Path, ok_only: bool) -> tuple[list[float], list[float], list[int]]:
    x_read: list[float] = []
    y_write: list[float] = []
    sample_ids: list[int] = []

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if ok_only and row.get("status", "") != "ok":
                continue
            try:
                rp99 = float(row["read_p99_us"])
                wp99 = float(row["write_p99_us"])
                sid = int(row.get("sample_index", len(sample_ids)))
            except (KeyError, ValueError, TypeError):
                continue
            x_read.append(rp99)
            y_write.append(wp99)
            sample_ids.append(sid)
    return x_read, y_write, sample_ids


def main() -> int:
    p = argparse.ArgumentParser(description="Plot p99 read vs p99 write scatter from LHS results.csv")
    p.add_argument("--csv", type=Path, default=Path("bench/LHS/results.csv"), help="Input results CSV")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("bench/LHS/p99_read_vs_write.png"),
        help="Output PNG path",
    )
    p.add_argument(
        "--include-failed",
        action="store_true",
        help="Include non-ok rows when p99 values are present",
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

    x_read, y_write, sample_ids = load_points(args.csv, ok_only=not args.include_failed)
    if not x_read:
        print("No valid (read_p99_us, write_p99_us) rows found in CSV", file=sys.stderr)
        return 1

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(x_read, y_write, alpha=0.8, s=22)
    from matplotlib.ticker import NullFormatter, ScalarFormatter

    ax.set_xscale("log",base=2)
    ax.set_yscale("log",base=2)

    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_formatter(ScalarFormatter())
        axis.set_minor_formatter(NullFormatter())
    ax.ticklabel_format(style="plain")

    ax.set_xlim(left=128)
    ax.set_ylim(bottom=8)
    ax.set_xlabel("p99 read latency (us)")
    ax.set_ylabel("p99 write latency (us)")
    ax.set_title("p99 Read vs p99 Write Latency")
    ax.grid(True, which="major", alpha=0.3)

    if args.annotate:
        for x, y, sid in zip(x_read, y_write, sample_ids):
            ax.annotate(str(sid), (x, y), fontsize=7, alpha=0.75)

    fig.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=150)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
