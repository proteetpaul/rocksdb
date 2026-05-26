# OpenEvolve db_bench Evaluator (Cache Tier Sizing)

This directory provides an OpenEvolve-compatible `evaluate(program_path)` that
runs a RocksDB db_bench load + workload sequence with tiered block cache and
cache tier controller enabled.

## Required environment variables

- `OPENEVOLVE_EVAL_CONFIG`: path to evaluator runtime JSON config (optional if
  `evolve-cache/eval_config.json` exists)
- `DB_BENCH`: absolute path to executable `db_bench`
- `DB_DIR`: database directory for db_bench runs (scratch; must differ from
  golden DB when using checkpoints)

## Optional environment variables (checkpoint workflow)

When `use_checkpoint: true` in the eval config, the load phase is skipped and
`DB_DIR` is populated from a golden database via `ldb checkpoint`. Set either
the env vars below or the equivalent JSON fields (`golden_db_dir`, `ldb`):

- `GOLDEN_DB_DIR` or `CHECKPOINT_SOURCE_DB`: path to the source database directory
- `LDB`: absolute path to executable `ldb`

## Hard requirements enforced by evaluator

- `statistics=true` is forced for the **workload** db_bench run (load uses
  `statistics=false`).
- `show_table_properties=true` is forced for the **workload** run only.
- each db_bench invocation is wrapped in `systemd-run --user --scope`.
- cgroup memory limit (`MemoryMax`) is always applied from config `memory_limit`.
- tiered cache + compressed secondary cache + cache tier controller flags are
  injected via `_force_tiered_cache_eval()`.
- data-only block cache: `cache_index_and_filter_blocks=false` (options file +
  db_bench flag, cannot be overridden by eval config).
- direct IO: `use_direct_reads=true` and
  `use_direct_io_for_flush_and_compaction=true` (options file + db_bench flags).
- initial tiered cache split: `cache_size` and `compressed_secondary_cache_size`
  are normalized to a 50-50 split; total DRAM budget is the sum of configured
  sizes (or 512M when neither is set).

## Runtime config file (`OPENEVOLVE_EVAL_CONFIG`)

Use [`evaluate/config.example.json`](config.example.json) as a template.

Important fields:

- `workload_ini` (required): target workload definition (`db_bench` flags).
  Default for evolve-cache: `bench/workloads/readrandom.ini`
  (100% `readrandom`, read-only cache tier sizing).
- `memory_limit` (required): fixed cgroup budget (`8G`, `512M`, or bytes)
- `eval_warmup_sec` (optional, default `180`): tuning/convergence window before measurement
- `eval_measure_sec` (optional, default `60`): measurement-only window; workload `duration` is set to their sum
- `load_ini` (optional, default `bench/workloads/load.ini`)
- `bench_ini` (optional, default `bench/bench.ini`)
- `base_options_file` (optional): base RocksDB options INI; used with `options_overrides`
- `db_bench_flags` (optional): flat map merged into db_bench flags
- `timeout_sec` (optional): timeout for each subprocess call
- `use_checkpoint` (optional, default `false`): skip load; copy `golden_db_dir`
  into `DB_DIR` via `ldb` (see env vars above)
- `golden_db_dir` (optional): same as `GOLDEN_DB_DIR` / `CHECKPOINT_SOURCE_DB`
- `ldb` (optional): same as `LDB` env var

## `program_path` (evolved source) contract

`program_path` is a **text file** (typically `.h`) whose contents **replace**
`cache/hit_rate_ghost_policy.h` under the CMake workspace before `cmake` builds
the tree.

Evaluation order: install evolved source → CMake build → load
`OPENEVOLVE_EVAL_CONFIG` → db_bench.

## Output metrics

The returned `EvaluationResult.metrics` includes:

- `combined_score` (= `-mean_read_latency`; higher is better)
- `throughput_qps`
- `mean_read_latency` (mean point-get read latency in **microseconds**, from db_bench `Microseconds per read:` histogram)
- `read_p50_us`, `read_p99_us` (microseconds)
- `primary_cache_hit_rate` (primary-tier only), `secondary_cache_hit_rate`
- block cache and secondary-cache ticker counts from STATISTICS output

Any non-success status is returned as `combined_score=-inf` with details in
`artifacts`.

On success, artifacts also include parsed `CacheTierMemoryController` info-log
lines from `$DB_DIR/LOG*`:

- `controller_log`: newline-separated matching log lines
- `controller_adjustments`: JSON list of parsed adjustments/failures
- `controller_adjustment_count`, `controller_adjustment_failures`
- `controller_final_secondary_ratio`: last successful target ratio (empty if none)

## LZ4

Tiered compressed secondary cache requires LZ4 at build and run time.
