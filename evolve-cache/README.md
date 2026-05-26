# OpenEvolve: cache tier sizing heuristics

Evolve `HitRateGhostPolicy::ComputeSecondaryRatio` in [`cache/hit_rate_ghost_policy.h`](../cache/hit_rate_ghost_policy.h) using OpenEvolve and db_bench workloads. See [`thoughts.md`](thoughts.md) for design context.

## Prerequisites

- CMake, C++ toolchain, LZ4 (required for tiered compressed secondary cache)
- Python 3.10+
- `systemd-run` (evaluator applies cgroup `MemoryMax` limits)
- Built or buildable RocksDB via CMake under `<repo>/build/`
- OpenEvolve vendored under [`../evolve-filters/openevolve`](../evolve-filters/openevolve)

## Environment

```bash
export DB_BENCH=/path/to/rocksdb/build/db_bench
export DB_DIR=/path/to/scratch/db
export OPENEVOLVE_EVAL_CONFIG=/path/to/rocksdb/evolve-cache/eval_config.json

# Optional checkpoint workflow (skips load phase)
export GOLDEN_DB_DIR=/path/to/golden/db
export LDB=/path/to/rocksdb/ldb
```

## Run evolution

```bash
cd evolve-filters/openevolve
python openevolve-run.py \
  ../../evolve-cache/initial_program.h \
  ../../evolve-cache/evaluate/evaluator.py \
  --config ../../evolve-cache/config.yaml \
  --iterations 50
```

## Smoke-test evaluator

```bash
python3 evolve-cache/evaluate/evaluator.py evolve-cache/initial_program.h
```

Each evaluation installs the candidate into `cache/hit_rate_ghost_policy.h`, rebuilds RocksDB with CMake, and runs db_bench with tiered cache + cache tier controller enabled.
