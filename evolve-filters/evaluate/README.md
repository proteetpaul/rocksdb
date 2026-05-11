# OpenEvolve db_bench Evaluator (Bloom Focus)

This directory provides an OpenEvolve-compatible `evaluate(program_path)` that
runs a RocksDB db_bench load + workload sequence and returns an
`EvaluationResult`.

## Required environment variables

- `OPENEVOLVE_EVAL_CONFIG`: path to evaluator runtime JSON config
- `DB_BENCH`: absolute path to executable `db_bench`
- `DB_DIR`: database directory for db_bench runs

## Hard requirements enforced by evaluator

- `statistics=true` is always enabled for db_bench.
- each db_bench invocation is wrapped in `systemd-run --user --scope`.
- cgroup memory limit (`MemoryMax`) is always applied from config `memory_limit`.
- table properties output is forced via `show_table_properties=true` and
  `stats_per_interval=true` so live filter-size extraction is available.

## Runtime config file (`OPENEVOLVE_EVAL_CONFIG`)

Use [`evaluate/config.example.json`](config.example.json) as a template. This
JSON is fixed for the whole evolution run (not per-iteration).

Important fields:

- `workload_ini` (required): target workload definition (`db_bench` flags)
- `memory_limit` (required): fixed cgroup budget (`8G`, `512M`, or bytes)
- `load_ini` (optional, default `bench/workloads/load.ini`)
- `bench_ini` (optional, default `bench/bench.ini`)
- `base_options_file` (optional): base RocksDB options INI; used with `options_overrides`
- `db_bench_flags` (optional): flat map merged into db_bench flags (formerly a separate candidate JSON)
- `options_overrides` (optional): nested section map for options file patching
- `timeout_sec` (optional): timeout for each subprocess call
- `bench_overrides` / `common_db_bench_flags` (optional): base flags

If `options_overrides` is non-empty, `base_options_file` must be set.

## `program_path` (evolved source) contract

`program_path` is a **text file** (typically `.cc`) whose contents **replace**
`table/block_based/filter_policy.cc` under the CMake workspace (`WORKSPACE` /
`WORKSPACE_ROOT`, or the RocksDB repo root) before `cmake` builds the tree.

Evaluation order: install evolved source → CMake build → load
`OPENEVOLVE_EVAL_CONFIG` → db_bench.

## Output metrics

The returned `EvaluationResult.metrics` includes:

- `combined_score` (throughput plus Bloom-quality/memory objective)
- `throughput_qps`
- `read_p50_us`, `read_p99_us`, `write_p50_us`, `write_p99_us`
- Bloom counters extracted from `rocksdb.bloom.filter.*`
- derived full-filter FP-rate metrics
- `live_sst_filter_bytes` parsed from aggregated table properties

Any non-success status is returned as `combined_score=0.0` with details in
`artifacts`.

