"""OpenEvolve evaluator that runs RocksDB db_bench workloads for cache tier sizing."""

from __future__ import annotations

import json
import logging
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EVOLVE_ROOT = Path(__file__).resolve().parents[1]
EVOLVE_EVAL = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EVOLVE_FILTERS_ROOT = WORKSPACE_ROOT / "evolve-filters"
EVOLVE_FILTERS_EVAL = EVOLVE_FILTERS_ROOT / "evaluate"
_OPENVOLVE_PKG_ROOT = EVOLVE_FILTERS_ROOT / "openevolve"


def _prepend_sys_path(path: Path) -> None:
    s = str(path.resolve())
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)


for _root in (
    EVOLVE_EVAL,
    WORKSPACE_ROOT,
    EVOLVE_ROOT,
    EVOLVE_FILTERS_EVAL,
    EVOLVE_FILTERS_ROOT,
    _OPENVOLVE_PKG_ROOT,
):
    _prepend_sys_path(_root)
_prepend_sys_path(EVOLVE_EVAL)

logger = logging.getLogger(__name__)


def _load_evaluation_result_class():
    eval_result_path = _OPENVOLVE_PKG_ROOT / "openevolve" / "evaluation_result.py"
    if eval_result_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "_openevolve_evaluation_result", eval_result_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.EvaluationResult
    from openevolve.evaluation_result import EvaluationResult  # type: ignore

    return EvaluationResult


EvaluationResult = _load_evaluation_result_class()

from checkpoint_db import CheckpointError, create_checkpoint
from cleanup_checkpoint import remove_checkpoint_tree
from dremel.options_file import write_options_file
from dremel.parse_db_bench import parse_db_bench_output

from parse_stats import (
    derive_cache_hit_rate_metrics,
    parse_block_cache_statistics,
    parse_cache_tier_statistics,
    parse_read_block_get_histogram,
)

_EVOLVE_CACHE_POLICY_H_RELATIVE = Path("cache/hit_rate_ghost_policy.h")

_BYTES_DB_BENCH_FLAGS = frozenset(
    {
        "cache_size",
        "compressed_secondary_cache_size",
    }
)

_DEFAULT_TIERED_CACHE_DB_BENCH_FLAGS: dict[str, str] = {
    "use_tiered_cache": "true",
    "use_compressed_secondary_cache": "true",
    "cache_size": "256M",
    "compressed_secondary_cache_size": "256M",
    "cache_tier_controller_enabled": "true",
    "cache_tier_controller_warmup_sec": "20",
    "cache_tier_controller_policy_interval_sec": "10",
    "cache_tier_controller_sample_interval_sec": "1",
}

_DEFAULT_TIERED_CACHE_TOTAL_BYTES = 512 * 1024 * 1024

_DBOPTIONS_SECTION = "[DBOptions]"

# Applied last so eval_config cannot weaken these assumptions (see config.yaml).
_ENFORCED_DB_BENCH_FLAGS: dict[str, str] = {
    "use_direct_reads": "true",
    "use_direct_io_for_flush_and_compaction": "true",
    "cache_index_and_filter_blocks": "false",
}
_ENFORCED_DBOPTIONS_OVERRIDES: dict[str, str] = {
    "use_direct_reads": "true",
    "use_direct_io_for_flush_and_compaction": "true",
}
_ENFORCED_BLOCK_BASED_TABLE_OVERRIDES: dict[str, str] = {
    "cache_index_and_filter_blocks": "false",
    "pin_l0_filter_and_index_blocks_in_cache": "false",
}

_DEFAULT_EVAL_WARMUP_SEC = 180
_DEFAULT_EVAL_MEASURE_SEC = 60
_FAILURE_COMBINED_SCORE = float("-inf")


def _failure_metrics(**overrides: float) -> dict[str, float]:
    """Same keys as the success path so MAP-Elites feature_dimensions always resolve."""
    metrics: dict[str, float] = {
        "combined_score": _FAILURE_COMBINED_SCORE,
        "rocksdb_build_success": 0.0,
        "throughput_qps": 0.0,
        "mean_read_latency": 0.0,
        "read_p99_us": 0.0,
        "block_cache_hit_rate": 0.0,
        "secondary_cache_hit_rate": 0.0,
    }
    metrics.update(overrides)
    return metrics


def _cmake_workspace_root() -> Path:
    """RocksDB checkout root (contains CMakeLists.txt and build/)."""
    for key in ("WORKSPACE", "WORKSPACE_ROOT"):
        v = os.environ.get(key)
        if v:
            return Path(v).expanduser().resolve()
    return WORKSPACE_ROOT


def _build_rocksdb_with_cmake() -> tuple[bool, dict[str, str]]:
    workspace = _cmake_workspace_root()
    build_dir = workspace / "build"
    cmakelists = workspace / "CMakeLists.txt"
    artifacts: dict[str, str] = {}

    if not cmakelists.is_file():
        artifacts["error_type"] = "MissingCMakeProject"
        artifacts["error_message"] = (
            f"Expected RocksDB CMake project at {cmakelists}; set WORKSPACE to the repo root."
        )
        return False, artifacts

    build_dir.mkdir(parents=True, exist_ok=True)
    ncpu = max(1, (os.cpu_count() or 1))
    jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", str(ncpu))
    configure_timeout = int(os.environ.get("ROCKSDB_CMAKE_CONFIGURE_TIMEOUT_SEC", "600"))
    build_timeout = int(os.environ.get("ROCKSDB_CMAKE_BUILD_TIMEOUT_SEC", "7200"))

    try:
        configure = subprocess.run(
            ["cmake", "-S", str(workspace), "-B", str(build_dir)],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=configure_timeout,
        )
    except FileNotFoundError:
        artifacts["error_type"] = "CMakeNotFound"
        artifacts["error_message"] = (
            "`cmake` executable not found on PATH; install CMake to build RocksDB."
        )
        artifacts["build_directory"] = str(build_dir)
        return False, artifacts
    except subprocess.TimeoutExpired:
        artifacts["error_type"] = "CMakeConfigureTimeout"
        artifacts["error_message"] = f"CMake configure exceeded {configure_timeout}s."
        artifacts["build_directory"] = str(build_dir)
        return False, artifacts

    if configure.returncode != 0:
        artifacts["error_type"] = "CMakeConfigureFailed"
        artifacts["error_message"] = "CMake configuration failed (see cmake_stdout / cmake_stderr)."
        artifacts["cmake_stdout"] = configure.stdout or ""
        artifacts["cmake_stderr"] = configure.stderr or ""
        artifacts["build_directory"] = str(build_dir)
        return False, artifacts

    try:
        build = subprocess.run(
            ["cmake", "--build", str(build_dir), "--parallel", jobs],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired:
        artifacts["error_type"] = "CMakeBuildTimeout"
        artifacts["error_message"] = f"CMake build exceeded {build_timeout}s."
        artifacts["build_directory"] = str(build_dir)
        return False, artifacts

    if build.returncode != 0:
        artifacts["error_type"] = "CMakeBuildFailed"
        artifacts["error_message"] = "CMake build failed (see build_stdout / build_stderr)."
        artifacts["build_stdout"] = build.stdout or ""
        artifacts["build_stderr"] = build.stderr or ""
        artifacts["build_directory"] = str(build_dir)
        return False, artifacts

    return True, {}


def _install_evolved_cache_policy_source(
    program_path: str, workspace: Path
) -> tuple[bool, dict[str, str]]:
    artifacts: dict[str, str] = {}
    src = Path(program_path).expanduser().resolve()
    if not src.is_file():
        artifacts["error_type"] = "EvolvedSourceNotFound"
        artifacts["error_message"] = f"program_path is not a readable file: {src}"
        return False, artifacts
    try:
        body = src.read_text(encoding="utf-8")
    except OSError as exc:
        artifacts["error_type"] = "EvolvedSourceReadError"
        artifacts["error_message"] = str(exc)
        artifacts["evolved_source_path"] = str(src)
        return False, artifacts

    dest = workspace / _EVOLVE_CACHE_POLICY_H_RELATIVE
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    except OSError as exc:
        artifacts["error_type"] = "CachePolicyInstallError"
        artifacts["error_message"] = str(exc)
        artifacts["cache_policy_destination"] = str(dest)
        artifacts["evolved_source_path"] = str(src)
        return False, artifacts

    logger.info("Installed evolved cache policy source %s -> %s", src, dest)
    return True, {}


def evaluate(program_path: str) -> EvaluationResult:
    """OpenEvolve-compatible evaluator entrypoint for cache tier sizing."""
    logger.info("evaluate() starting program_path=%s", program_path)
    try:
        workspace = _cmake_workspace_root()
        install_ok, install_artifacts = _install_evolved_cache_policy_source(
            program_path, workspace
        )
        if not install_ok:
            msg = install_artifacts.get("error_message", "Failed to install evolved source")
            merged: dict[str, str] = {"status": "evolved_source_install_failed", "error": msg}
            merged.update(install_artifacts)
            logger.warning("Evolved hit_rate_ghost_policy.h install failed: %s", msg)
            return EvaluationResult(metrics=_failure_metrics(), artifacts=merged)

        build_ok, build_artifacts = _build_rocksdb_with_cmake()
        if not build_ok:
            msg = build_artifacts.get("error_message", "RocksDB CMake build failed")
            merged = {"status": "rocksdb_cmake_failed", "error": msg}
            merged.update(build_artifacts)
            logger.warning("RocksDB CMake build failed: %s", msg)
            return EvaluationResult(metrics=_failure_metrics(), artifacts=merged)

        runtime_config_path = _openevolve_eval_config_path()
        eval_config = _load_json(runtime_config_path)
        _force_tiered_cache_eval(eval_config)
        eval_warmup_sec, eval_measure_sec, eval_total_sec = _apply_two_phase_eval_config(
            eval_config
        )
        evolved_resolved = Path(program_path).expanduser().resolve()
        logger.info(
            "Loaded eval config from %s; evolved source %s; two-phase eval "
            "warmup=%ss measure=%ss total=%ss",
            runtime_config_path,
            evolved_resolved,
            eval_warmup_sec,
            eval_measure_sec,
            eval_total_sec,
        )

        db_bench = _required_executable_from_env("DB_BENCH")
        db_dir = _required_env_path("DB_DIR")
        _ensure_systemd_run()

        memory_limit_text = _required_str(eval_config, "memory_limit")
        memory_limit_bytes = _parse_size_bytes(memory_limit_text)
        timeout_sec = _optional_float(eval_config.get("timeout_sec"))
        logger.info(
            "db_bench=%s db_dir=%s memory_limit=%s (%s bytes) timeout_sec=%s",
            db_bench,
            db_dir,
            memory_limit_text,
            memory_limit_bytes,
            timeout_sec,
        )

        bench_values = _load_simple_ini(
            _resolve_path(eval_config, "bench_ini", default="bench/bench.ini")
        )
        load_values = _load_simple_ini(
            _resolve_path(eval_config, "load_ini", default="bench/workloads/load.ini")
        )
        workload_values = _load_simple_ini(_resolve_path(eval_config, "workload_ini"))
        workload_values["duration"] = str(eval_total_sec)

        db_bench_flags = _eval_db_bench_flags(eval_config)
        common_flags = _common_db_bench_flags(
            eval_config, bench_values, db_dir, db_bench_flags
        )
        options_file_path = _maybe_build_options_file(eval_config)

        if options_file_path is not None:
            common_flags.append(f"--options_file={options_file_path}")
            logger.info("Using patched options file %s", options_file_path)

        load_common_flags = _with_flag_overrides(
            common_flags,
            {
                "statistics": "false",
                "show_table_properties": "false",
                "report_interval_seconds": "0",
                "stats_interval": "0",
            },
        )
        logger.info("Load flags: %s", load_common_flags)

        workload_common_flags = _with_flag_overrides(
            common_flags,
            {
                "statistics": "true",
                "show_table_properties": "true",
                "report_interval_seconds": "0",
                "stats_interval": "0",
            },
        )
        logger.info("Workload flags: %s", workload_common_flags)

        load_flags = _ini_to_argv(load_values)
        workload_flags = _ini_to_argv(workload_values)

        use_checkpoint = _config_bool(eval_config.get("use_checkpoint"))
        checkpoint_protected: frozenset[Path] = frozenset()
        ldb_bin: Path | None = None
        golden_path: Path | None = None

        if use_checkpoint:
            golden_raw = (
                eval_config.get("golden_db_dir")
                or os.environ.get("GOLDEN_DB_DIR")
                or os.environ.get("CHECKPOINT_SOURCE_DB")
            )
            ldb_raw = eval_config.get("ldb") or os.environ.get("LDB")
            if not golden_raw or not str(golden_raw).strip():
                return _error_result(
                    "checkpoint_config",
                    "use_checkpoint requires golden_db_dir or GOLDEN_DB_DIR / "
                    "CHECKPOINT_SOURCE_DB",
                )
            if not ldb_raw or not str(ldb_raw).strip():
                return _error_result(
                    "checkpoint_config",
                    "use_checkpoint requires ldb in eval config or LDB env",
                )
            golden_path = _resolve_repo_relative_path(str(golden_raw).strip())
            ldb_bin = _resolve_repo_relative_path(str(ldb_raw).strip())
            if not golden_path.is_dir():
                return _error_result(
                    "invalid_golden_db",
                    f"golden_db_dir is not a directory: {golden_path}",
                )
            if not ldb_bin.is_file() or not os.access(ldb_bin, os.X_OK):
                return _error_result(
                    "invalid_ldb",
                    f"ldb is not an executable file: {ldb_bin}",
                )
            if db_dir.resolve() == golden_path:
                return _error_result(
                    "invalid_db_dir",
                    "DB_DIR must not equal golden_db_dir when use_checkpoint is true",
                )
            checkpoint_protected = frozenset({golden_path})

        try:
            if use_checkpoint:
                assert golden_path is not None and ldb_bin is not None
                _remove_db_dir_for_checkpoint(db_dir)
                logger.info(
                    "Removed scratch DB directory %s before checkpoint (directory must "
                    "not exist for ldb)",
                    db_dir,
                )
                try:
                    create_checkpoint(ldb_bin, golden_path, db_dir)
                except CheckpointError as exc:
                    msg = str(exc)
                    extra_ck: dict[str, str] = {}
                    if exc.returncode is not None:
                        extra_ck["checkpoint_return_code"] = str(exc.returncode)
                    if exc.stderr:
                        extra_ck["checkpoint_stderr"] = exc.stderr[:8000]
                    if exc.stdout:
                        extra_ck["checkpoint_stdout"] = exc.stdout[:8000]
                    logger.warning("Checkpoint failed: %s", msg)
                    return _error_result(
                        "checkpoint_failed", msg, extra_artifacts=extra_ck
                    )
                logger.info("Checkpoint from %s to %s completed", golden_path, db_dir)
            else:
                _recreate_dir(db_dir)
                logger.info("Recreated database directory %s", db_dir)

            if not use_checkpoint:
                load_argv = _scoped_argv(
                    db_bench,
                    load_common_flags + load_flags,
                    memory_limit_bytes,
                )
                logger.info(
                    "Starting db_bench load phase (MemoryMax=%s)", memory_limit_bytes
                )
                load_result = _run(load_argv, timeout_sec=timeout_sec)
                if load_result.returncode != 0:
                    logger.warning(
                        "Load phase failed returncode=%s db_bench=%s",
                        load_result.returncode,
                        db_bench,
                    )
                    return _error_result(
                        "load_failed",
                        load_result.stdout or "",
                        extra_artifacts={
                            "load_return_code": str(load_result.returncode)
                        },
                    )
                logger.info("Load phase completed successfully")

            run_argv = _scoped_argv(
                db_bench,
                workload_common_flags + workload_flags,
                memory_limit_bytes,
            )
            logger.info(
                "Starting db_bench workload phase (MemoryMax=%s)", memory_limit_bytes
            )
            run_result = _run(run_argv, timeout_sec=timeout_sec)
            output_text = run_result.stdout or ""
            if run_result.returncode != 0:
                logger.warning(
                    "Workload phase failed returncode=%s db_bench=%s",
                    run_result.returncode,
                    db_bench,
                )
                return _error_result(
                    "run_failed",
                    output_text,
                    extra_artifacts={"run_return_code": str(run_result.returncode)},
                )
            logger.info("Workload phase completed successfully")
            perf_metrics = parse_db_bench_output(output_text)
            block_cache_stats = parse_block_cache_statistics(output_text)
            cache_tier_stats = parse_cache_tier_statistics(output_text)
            read_block_get_stats = parse_read_block_get_histogram(output_text)
            hit_rates = derive_cache_hit_rate_metrics(block_cache_stats, cache_tier_stats)

            throughput = perf_metrics.throughput_qps or 0.0
            mean_read = (
                perf_metrics.read_mean_us or perf_metrics.read_p50_us or 0.0
            )
            read_p50 = perf_metrics.read_p50_us or 0.0
            read_p99 = perf_metrics.read_p99_us or 0.0

            metrics: dict[str, float] = {
                # OpenEvolve maximizes combined_score; negate mean read latency (micros).
                "combined_score": -mean_read,
                "rocksdb_build_success": 1.0,
                "throughput_qps": throughput,
                "mean_read_latency": mean_read,
                "read_p50_us": read_p50,
                "read_p99_us": read_p99,
            }
            metrics.update(hit_rates)
            metrics.update(block_cache_stats)
            metrics.update(cache_tier_stats)
            metrics.update(read_block_get_stats)

            logger.info(
                "Evaluation ok throughput_qps=%s mean_read_latency=%s block_cache_hit_rate=%s",
                throughput,
                mean_read,
                hit_rates.get("block_cache_hit_rate"),
            )
            return EvaluationResult(
                metrics=metrics,
                artifacts={
                    "status": "ok",
                    "runtime_config_path": str(runtime_config_path),
                    "evolved_source_path": str(evolved_resolved),
                    "memory_limit": memory_limit_text,
                    "db_bench_path": str(db_bench),
                    "db_dir": str(db_dir),
                    "log": "",
                },
            )
        finally:
            if options_file_path is not None and options_file_path.is_file():
                options_file_path.unlink()
            _cleanup_db_dir_if_requested(
                eval_config, db_dir, protected_roots=checkpoint_protected
            )
    except subprocess.TimeoutExpired as exc:
        logger.warning("Evaluation timed out: %s", exc)
        return _error_result("timeout", str(exc))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Evaluation failed: %s", exc, exc_info=True)
        return _error_result("error", str(exc))


def _force_tiered_cache_eval(eval_config: dict[str, Any]) -> None:
    """
    Force tiered cache + cache tier controller db_bench flags, static Bloom filter,
    and eval assumptions (data-only block cache, direct IO).

    Mutates eval_config in-place.
    """
    db_flags = eval_config.get("db_bench_flags")
    if db_flags is None:
        db_flags = {}
    if not isinstance(db_flags, dict):
        raise ValueError("db_bench_flags must be a JSON object")

    merged_flags = dict(_DEFAULT_TIERED_CACHE_DB_BENCH_FLAGS)
    merged_flags.update({str(k): _to_cli_value(v) for k, v in db_flags.items()})
    _apply_initial_tiered_cache_split(merged_flags)
    merged_flags.update(_ENFORCED_DB_BENCH_FLAGS)
    eval_config["db_bench_flags"] = merged_flags

    if "base_options_file" not in eval_config:
        logger.warning(
            "base_options_file is not set; cannot patch options file for Bloom filter "
            "or cache/direct-IO assumptions (db_bench flags still enforced)"
        )
        return

    base_options = _resolve_path(eval_config, "base_options_file")
    block_sections = _find_block_based_table_sections(base_options)
    if not block_sections:
        block_sections = ['[TableOptions/BlockBasedTable "default"]']

    existing_overrides = eval_config.get("options_overrides", {})
    if existing_overrides is None:
        existing_overrides = {}
    if not isinstance(existing_overrides, dict):
        raise ValueError("options_overrides must be a JSON object")

    db_overrides = existing_overrides.get(_DBOPTIONS_SECTION, {})
    if db_overrides is None:
        db_overrides = {}
    if not isinstance(db_overrides, dict):
        raise ValueError(f"options_overrides[{_DBOPTIONS_SECTION}] must be a JSON object")
    db_overrides.update(_ENFORCED_DBOPTIONS_OVERRIDES)
    existing_overrides[_DBOPTIONS_SECTION] = db_overrides

    for section in block_sections:
        section_overrides = existing_overrides.get(section, {})
        if section_overrides is None:
            section_overrides = {}
        if not isinstance(section_overrides, dict):
            raise ValueError(f"options_overrides[{section}] must be a JSON object")
        section_overrides.setdefault("filter_policy", "bloomfilter:10")
        section_overrides.update(_ENFORCED_BLOCK_BASED_TABLE_OVERRIDES)
        existing_overrides[section] = section_overrides

    eval_config["options_overrides"] = existing_overrides


def _tiered_cache_total_bytes(db_flags: dict[str, str]) -> int:
    """Total tiered-cache DRAM budget before 50-50 split."""
    primary_raw = db_flags.get("cache_size")
    secondary_raw = db_flags.get("compressed_secondary_cache_size")
    primary = _parse_size_bytes(str(primary_raw)) if primary_raw else None
    secondary = _parse_size_bytes(str(secondary_raw)) if secondary_raw else None
    if primary is not None and secondary is not None:
        return primary + secondary
    if primary is not None:
        return primary
    if secondary is not None:
        return secondary
    return _DEFAULT_TIERED_CACHE_TOTAL_BYTES


def _apply_initial_tiered_cache_split(db_flags: dict[str, str]) -> None:
    """Normalize cache_size flags to a 50-50 primary/secondary split."""
    total = _tiered_cache_total_bytes(db_flags)
    primary = total // 2
    secondary = total - primary
    db_flags["cache_size"] = str(primary)
    db_flags["compressed_secondary_cache_size"] = str(secondary)


def _optional_positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("expected integer, got bool")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        result = int(text)
    else:
        raise ValueError(f"expected integer, got {type(value)}")
    if result <= 0:
        raise ValueError("value must be positive")
    return result


def _apply_two_phase_eval_config(
    eval_config: dict[str, Any],
) -> tuple[int, int, int]:
    """
    Configure x+y second benchmark: x seconds tuning, y seconds measurement.

    Sets eval_warmup_sec db_bench flag, bench_overrides.duration, and returns
    (warmup_sec, measure_sec, total_sec).
    """
    warmup_sec = _optional_positive_int(
        eval_config.get("eval_warmup_sec"), _DEFAULT_EVAL_WARMUP_SEC
    )
    measure_sec = _optional_positive_int(
        eval_config.get("eval_measure_sec"), _DEFAULT_EVAL_MEASURE_SEC
    )
    total_sec = warmup_sec + measure_sec

    db_flags = eval_config.get("db_bench_flags")
    if db_flags is None:
        db_flags = {}
        eval_config["db_bench_flags"] = db_flags
    if not isinstance(db_flags, dict):
        raise ValueError("db_bench_flags must be a JSON object")
    db_flags["eval_warmup_sec"] = str(warmup_sec)

    bench_overrides = eval_config.get("bench_overrides")
    if bench_overrides is None:
        bench_overrides = {}
        eval_config["bench_overrides"] = bench_overrides
    if not isinstance(bench_overrides, dict):
        raise ValueError("bench_overrides must be a JSON object")
    bench_overrides["duration"] = str(total_sec)

    timeout_sec = _optional_float(eval_config.get("timeout_sec"))
    if timeout_sec is not None and timeout_sec < total_sec + 60:
        logger.warning(
            "timeout_sec=%s may be tight for two-phase eval total=%ss; "
            "consider >= %ss",
            timeout_sec,
            total_sec,
            total_sec + 60,
        )

    return warmup_sec, measure_sec, total_sec


def _openevolve_eval_config_path() -> Path:
    raw = os.environ.get("OPENEVOLVE_EVAL_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    default_path = (EVOLVE_ROOT / "eval_config.json").resolve()
    if default_path.is_file():
        return default_path
    raise ValueError(
        "OPENEVOLVE_EVAL_CONFIG must be set, or place eval_config.json under "
        f"{EVOLVE_ROOT}"
    )


def _required_env_path(var_name: str) -> Path:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        raise ValueError(f"{var_name} must be set")
    return Path(raw).expanduser().resolve()


def _required_executable_from_env(var_name: str) -> Path:
    path = _required_env_path(var_name)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError(f"{var_name} is not an executable file: {path}")
    return path


def _ensure_systemd_run() -> None:
    if shutil.which("systemd-run") is None:
        raise RuntimeError("systemd-run is required for mandatory cgroup memory limits")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _resolve_path(config: dict[str, Any], key: str, default: str | None = None) -> Path:
    value = config.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _cmake_workspace_root() / path
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{key} does not exist: {resolved}")
    return resolved


def _load_simple_ini(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _eval_db_bench_flags(eval_config: dict[str, Any]) -> list[str]:
    flags = eval_config.get("db_bench_flags", {})
    if flags is None:
        return []
    if not isinstance(flags, dict):
        raise ValueError("eval config db_bench_flags must be a JSON object")
    out: list[str] = []
    for key, value in sorted(flags.items()):
        out.append(f"--{key}={_db_bench_flag_cli_value(key, value)}")
    return out


def _common_db_bench_flags(
    eval_config: dict[str, Any],
    bench_values: dict[str, str],
    db_dir: Path,
    extra_db_bench_flags: list[str],
) -> list[str]:
    merged: dict[str, Any] = dict(bench_values)
    if "bench_overrides" in eval_config:
        overrides = eval_config["bench_overrides"]
        if not isinstance(overrides, dict):
            raise ValueError("bench_overrides must be a JSON object")
        merged.update(overrides)

    merged.pop("db", None)
    merged.pop("report_dir", None)
    merged["db"] = str(db_dir)
    merged["statistics"] = "true"
    merged["show_table_properties"] = "true"

    user_common = eval_config.get("common_db_bench_flags", {})
    if user_common is not None:
        if not isinstance(user_common, dict):
            raise ValueError("common_db_bench_flags must be a JSON object")
        merged.update(user_common)
    merged.pop("report_dir", None)
    merged["statistics"] = "true"
    merged["show_table_properties"] = "true"
    merged["report_interval_seconds"] = "0"

    cli_flags = [f"--{key}={_to_cli_value(value)}" for key, value in sorted(merged.items())]
    return cli_flags + extra_db_bench_flags


def _maybe_build_options_file(eval_config: dict[str, Any]) -> Path | None:
    if "base_options_file" not in eval_config:
        return None
    base_options = _resolve_path(eval_config, "base_options_file")
    overrides = eval_config.get("options_overrides", {})
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise ValueError("options_overrides must be a JSON object")

    section_map: dict[str, dict[str, str]] = {}
    for section, values in overrides.items():
        if not isinstance(section, str):
            raise ValueError("options_overrides keys must be section strings")
        if not isinstance(values, dict):
            raise ValueError(f"options_overrides[{section}] must be a JSON object")
        section_map[section] = {}
        for key, value in values.items():
            section_map[section][str(key)] = _to_cli_value(value)

    if not section_map:
        logger.warning(
            "base_options_file is set but options_overrides produced no sections; skipping options patch"
        )
        return None
    fd, out_path_raw = tempfile.mkstemp(prefix="openevolve_eval_options_", suffix=".ini")
    os.close(fd)
    out_path = Path(out_path_raw)
    write_options_file(base_options, out_path, section_map)
    logger.info(
        "Wrote options patch from %s to %s (%s section(s))",
        base_options,
        out_path,
        len(section_map),
    )
    return out_path


def _find_block_based_table_sections(path: Path) -> list[str]:
    sections: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[TableOptions/BlockBasedTable") and line.endswith("]"):
            sections.append(line)
    return sections


def _ini_to_argv(values: dict[str, str]) -> list[str]:
    return [f"--{key}={value}" for key, value in sorted(values.items())]


def _with_flag_overrides(flags: list[str], overrides: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for flag in flags:
        if not flag.startswith("--") or "=" not in flag:
            out.append(flag)
            continue
        key, _, value = flag[2:].partition("=")
        if key in overrides:
            out.append(f"--{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(f"--{key}={value}")

    for key, value in overrides.items():
        if key not in seen:
            out.append(f"--{key}={value}")
    return out


def _scoped_argv(db_bench: Path, db_bench_flags: list[str], memory_limit_bytes: int) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--scope",
        "-p",
        f"MemoryMax={memory_limit_bytes}",
        "--",
        str(db_bench),
        *db_bench_flags,
    ]


def _run(argv: list[str], *, timeout_sec: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_sec,
    )


def _error_result(
    status: str, message: str, extra_artifacts: dict[str, str] | None = None
) -> EvaluationResult:
    logger.warning(
        "Returning error result status=%s combined_score=-inf extra=%s",
        status,
        extra_artifacts or {},
    )
    artifacts: dict[str, str] = {"status": status, "error": message}
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    return EvaluationResult(metrics=_failure_metrics(), artifacts=artifacts)


def _required_str(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        return float(value)
    raise ValueError("timeout_sec must be numeric")


def _to_cli_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise ValueError(f"Unsupported CLI value type: {type(value)}")


def _db_bench_flag_cli_value(key: str, value: Any) -> str:
    if key in _BYTES_DB_BENCH_FLAGS and isinstance(value, str):
        return str(_parse_size_bytes(value))
    return _to_cli_value(value)


def _parse_size_bytes(raw: str) -> int:
    text = raw.strip().replace(" ", "")
    if not text:
        raise ValueError("size value is empty")
    if text.isdigit():
        value = int(text)
        if value <= 0:
            raise ValueError("size value must be positive")
        return value
    if len(text) < 2 or not text[:-1].isdigit():
        raise ValueError(f"invalid size value: {raw}")
    suffix = text[-1].upper()
    multiplier = {
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
    }.get(suffix)
    if multiplier is None:
        raise ValueError(f"invalid size suffix: {raw}")
    value = int(text[:-1]) * multiplier
    if value <= 0:
        raise ValueError("size value must be positive")
    return value


def _recreate_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _remove_db_dir_for_checkpoint(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _resolve_repo_relative_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _cmake_workspace_root() / p
    return p.resolve()


def _cleanup_db_dir_if_requested(
    eval_config: dict[str, Any],
    db_dir: Path,
    *,
    protected_roots: frozenset[Path] | None = None,
) -> None:
    cleanup = eval_config.get("cleanup_db_dir", True)
    if not bool(cleanup):
        logger.info(
            "Leaving database directory %s in place (cleanup_db_dir=false)", db_dir
        )
        return
    roots = protected_roots if protected_roots is not None else frozenset()
    try:
        remove_checkpoint_tree(db_dir, protected_roots=roots, ignore_errors=True)
    except ValueError as exc:
        logger.warning("Skipping database directory cleanup: %s", exc)
        return
    logger.info("Removed database directory %s (cleanup_db_dir=true)", db_dir)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Run cache tier OpenEvolve evaluator once")
    parser.add_argument("program_path", help="Path to evolved hit_rate_ghost_policy.h")
    args = parser.parse_args()
    result = evaluate(args.program_path)
    print(json.dumps({"metrics": result.metrics, "artifacts": result.artifacts}, indent=2))
