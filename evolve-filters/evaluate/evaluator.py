"""OpenEvolve evaluator that runs RocksDB db_bench workloads."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVOLVE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
for candidate_root in (WORKSPACE_ROOT, EVOLVE_ROOT):
    if str(candidate_root) not in sys.path:
        sys.path.insert(0, str(candidate_root))

from dremel.options_file import write_options_file
from dremel.parse_db_bench import parse_db_bench_output

from evaluate.parse_stats import parse_bloom_statistics, parse_live_sst_filter_bytes

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    metrics: dict[str, float]
    artifacts: dict[str, str] = field(default_factory=dict)


def _load_openevolve_evaluation_result() -> type[EvaluationResult]:
    module_path = EVOLVE_ROOT / "openevolve" / "openevolve" / "evaluation_result.py"
    if not module_path.is_file():
        logger.warning(
            "OpenEvolve EvaluationResult module not found at %s; using local fallback.",
            module_path,
        )
        return EvaluationResult

    spec = importlib.util.spec_from_file_location(
        "openevolve_evaluation_result_local",
        str(module_path),
    )
    if spec is None or spec.loader is None:
        logger.warning(
            "Could not create import spec for OpenEvolve EvaluationResult at %s; using local fallback.",
            module_path,
        )
        return EvaluationResult

    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "Failed to load OpenEvolve EvaluationResult from %s; using local fallback.",
            module_path,
            exc_info=True,
        )
        return EvaluationResult

    loaded_cls = getattr(module, "EvaluationResult", None)
    if isinstance(loaded_cls, type):
        logger.info("Loaded OpenEvolve EvaluationResult from %s", module_path)
        return loaded_cls
    logger.warning(
        "Module at %s has no usable EvaluationResult class; using local fallback.",
        module_path,
    )
    return EvaluationResult


EvaluationResult = _load_openevolve_evaluation_result()

# Evolved C++ is installed here relative to the CMake workspace root (see _cmake_workspace_root()).
_FILTER_POLICY_CC_RELATIVE = Path("table/block_based/filter_policy.cc")


def _cmake_workspace_root() -> Path:
    """RocksDB checkout root (contains CMakeLists.txt and build/)."""
    for key in ("WORKSPACE", "WORKSPACE_ROOT"):
        v = os.environ.get(key)
        if v:
            return Path(v).expanduser().resolve()
    return WORKSPACE_ROOT


def _build_rocksdb_with_cmake() -> tuple[bool, dict[str, str]]:
    """
    Configure and build RocksDB under <workspace>/build using CMake.

    Returns:
        (success, artifacts). On failure, artifacts hold stderr/stdout and error_type for OpenEvolve.
    """
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


def _install_evolved_filter_policy_source(
    program_path: str, workspace: Path
) -> tuple[bool, dict[str, str]]:
    """
    Copy LLM-produced C++ from program_path into workspace's filter_policy.cc.

    Concurrent evaluations on the same workspace overwrite the same file; use
    isolated WORKSPACE checkouts or serialize evaluators.

    Returns:
        (success, artifacts). On failure, artifacts describe the error for OpenEvolve.
    """
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

    dest = workspace / _FILTER_POLICY_CC_RELATIVE
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    except OSError as exc:
        artifacts["error_type"] = "FilterPolicyInstallError"
        artifacts["error_message"] = str(exc)
        artifacts["filter_policy_destination"] = str(dest)
        artifacts["evolved_source_path"] = str(src)
        return False, artifacts

    logger.info("Installed evolved filter policy source %s -> %s", src, dest)
    return True, {}


def evaluate(program_path: str) -> EvaluationResult:
    """OpenEvolve-compatible evaluator entrypoint.

    ``program_path`` must be a file whose contents replace ``table/block_based/filter_policy.cc``
    under the CMake workspace (``WORKSPACE`` / ``WORKSPACE_ROOT`` or the repo root). That file is
    written before CMake builds RocksDB.

    Fixed bench/db_bench/options JSON is loaded from ``OPENEVOLVE_EVAL_CONFIG`` **after** a
    successful CMake build (merge former per-candidate ``db_bench_flags`` / ``options_overrides``
    into that JSON).
    """
    logger.info("evaluate() starting program_path=%s", program_path)
    try:
        workspace = _cmake_workspace_root()
        install_ok, install_artifacts = _install_evolved_filter_policy_source(
            program_path, workspace
        )
        if not install_ok:
            msg = install_artifacts.get("error_message", "Failed to install evolved source")
            merged: dict[str, str] = {"status": "evolved_source_install_failed", "error": msg}
            merged.update(install_artifacts)
            logger.warning("Evolved filter_policy.cc install failed: %s", msg)
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts=merged,
            )

        build_ok, build_artifacts = _build_rocksdb_with_cmake()
        if not build_ok:
            msg = build_artifacts.get("error_message", "RocksDB CMake build failed")
            merged = {"status": "rocksdb_cmake_failed", "error": msg}
            merged.update(build_artifacts)
            logger.warning("RocksDB CMake build failed: %s", msg)
            return EvaluationResult(
                metrics={"combined_score": 0.0, "rocksdb_build_success": 0.0},
                artifacts=merged,
            )

        runtime_config_path = _required_env_path("OPENEVOLVE_EVAL_CONFIG")
        eval_config = _load_json(runtime_config_path)
        _force_evolve_dummy_filter_policy(eval_config)
        evolved_resolved = Path(program_path).expanduser().resolve()
        logger.info(
            "Loaded eval config from %s; evolved source %s",
            runtime_config_path,
            evolved_resolved,
        )

        db_bench = _required_executable_from_env("DB_BENCH")
        db_dir = _required_env_path("DB_DIR")
        _ensure_systemd_run()

        memory_limit_text = _required_str(eval_config, "memory_limit")
        memory_limit_bytes = _parse_memory_limit_bytes(memory_limit_text)
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

        db_bench_flags = _eval_db_bench_flags(eval_config)
        common_flags = _common_db_bench_flags(
            eval_config, bench_values, db_dir, db_bench_flags
        )
        options_file_path = _maybe_build_options_file(eval_config)

        if options_file_path is not None:
            common_flags.append(f"--options_file={options_file_path}")
            logger.info("Using patched options file %s", options_file_path)

        load_flags = _ini_to_argv(load_values)
        workload_flags = _ini_to_argv(workload_values)

        full_log: list[str] = []
        try:
            _recreate_dir(db_dir)
            logger.info("Recreated database directory %s", db_dir)

            load_argv = _scoped_argv(
                db_bench, common_flags + load_flags, memory_limit_bytes
            )
            full_log.append(_argv_line(load_argv))
            logger.info("Starting db_bench load phase (MemoryMax=%s)", memory_limit_bytes)
            load_result = _run(load_argv, timeout_sec=timeout_sec)
            full_log.append(load_result.stdout or "")
            if load_result.returncode != 0:
                logger.warning(
                    "Load phase failed returncode=%s db_bench=%s",
                    load_result.returncode,
                    db_bench,
                )
                return _error_result(
                    "load_failed",
                    "".join(full_log),
                    extra_artifacts={"load_return_code": str(load_result.returncode)},
                )
            logger.info("Load phase completed successfully")

            run_argv = _scoped_argv(
                db_bench, common_flags + workload_flags, memory_limit_bytes
            )
            full_log.append("\n" + _argv_line(run_argv))
            logger.info(
                "Starting db_bench workload phase (MemoryMax=%s)", memory_limit_bytes
            )
            run_result = _run(run_argv, timeout_sec=timeout_sec)
            full_log.append(run_result.stdout or "")
            if run_result.returncode != 0:
                logger.warning(
                    "Workload phase failed returncode=%s db_bench=%s",
                    run_result.returncode,
                    db_bench,
                )
                return _error_result(
                    "run_failed",
                    "".join(full_log),
                    extra_artifacts={"run_return_code": str(run_result.returncode)},
                )
            logger.info("Workload phase completed successfully")

            output_text = "".join(full_log)
            perf_metrics = parse_db_bench_output(output_text)
            bloom_stats = parse_bloom_statistics(output_text)
            live_sst_filter_bytes = parse_live_sst_filter_bytes(output_text)

            throughput = perf_metrics.throughput_qps or 0.0
            read_p99 = perf_metrics.read_p99_us or 0.0
            write_p99 = perf_metrics.write_p99_us or 0.0

            # observed_fp_rate = bloom_stats.get("bloom.full.observed_fp_rate", 0.0)
            # read_latency_factor = 1.0 / (1.0 + (read_p99 / 1000.0))
            # write_latency_factor = 1.0 / (1.0 + (write_p99 / 1000.0))
            # latency_factor = (read_latency_factor + write_latency_factor) / 2.0

            # fp_factor = max(0.0, 1.0 - observed_fp_rate)
            # memory_factor = 1.0 / (1.0 + (live_sst_filter_bytes / (64.0 * 1024 * 1024)))
            # combined_score = throughput * memory_factor

            metrics: dict[str, float] = {
                "combined_score": throughput,
                "rocksdb_build_success": 1.0,
                "throughput_qps": throughput,
                "read_p99_us": read_p99,
                "write_p99_us": write_p99,
                "filter_memory_usage": live_sst_filter_bytes,
            }
            metrics.update(bloom_stats)

            logger.info(
                "Evaluation ok throughput_qps=%s read_p99_us=%s bloom_stat_keys=%s filter_bytes=%s",
                perf_metrics.throughput_qps,
                perf_metrics.read_p99_us,
                len(bloom_stats),
                live_sst_filter_bytes,
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
                    "log": output_text,
                },
            )
        finally:
            if options_file_path is not None and options_file_path.is_file():
                options_file_path.unlink()
            _cleanup_db_dir_if_requested(eval_config, db_dir)
    except subprocess.TimeoutExpired as exc:
        logger.warning("Evaluation timed out: %s", exc)
        return _error_result("timeout", str(exc))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Evaluation failed: %s", exc, exc_info=True)
        return _error_result("error", str(exc))


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
        out.append(f"--{key}={_to_cli_value(value)}")
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
    merged["db"] = str(db_dir)
    merged["statistics"] = "true"
    merged["show_table_properties"] = "true"
    merged["stats_per_interval"] = "true"
    merged.setdefault("stats_interval", "1")

    user_common = eval_config.get("common_db_bench_flags", {})
    if user_common is not None:
        if not isinstance(user_common, dict):
            raise ValueError("common_db_bench_flags must be a JSON object")
        merged.update(user_common)
    merged["statistics"] = "true"
    merged["show_table_properties"] = "true"
    merged["stats_per_interval"] = "true"
    merged.setdefault("stats_interval", "1")

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


def _force_evolve_dummy_filter_policy(eval_config: dict[str, Any]) -> None:
    """
    Force the table filter policy to rocksdb.EvolveDummyFilter for evaluation.

    Mutates eval_config in-place by injecting options_overrides entries for all
    block-based table sections found in base_options_file.
    """
    if "base_options_file" not in eval_config:
        logger.warning(
            "base_options_file is not set; cannot force filter_policy=rocksdb.EvolveDummyFilter"
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

    for section in block_sections:
        section_overrides = existing_overrides.get(section, {})
        if section_overrides is None:
            section_overrides = {}
        if not isinstance(section_overrides, dict):
            raise ValueError(f"options_overrides[{section}] must be a JSON object")
        section_overrides["filter_policy"] = "rocksdb.EvolveDummyFilter"
        existing_overrides[section] = section_overrides

    eval_config["options_overrides"] = existing_overrides


def _find_block_based_table_sections(path: Path) -> list[str]:
    sections: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[TableOptions/BlockBasedTable") and line.endswith("]"):
            sections.append(line)
    return sections


def _ini_to_argv(values: dict[str, str]) -> list[str]:
    return [f"--{key}={value}" for key, value in sorted(values.items())]


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


def _argv_line(argv: list[str]) -> str:
    return " ".join(argv) + "\n\n"


def _error_result(
    status: str, message: str, extra_artifacts: dict[str, str] | None = None
) -> EvaluationResult:
    logger.warning(
        "Returning error result status=%s combined_score=0.0 extra=%s",
        status,
        extra_artifacts or {},
    )
    artifacts: dict[str, str] = {"status": status, "error": message}
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    return EvaluationResult(metrics={"combined_score": 0.0}, artifacts=artifacts)


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


def _parse_memory_limit_bytes(raw: str) -> int:
    text = raw.strip().replace(" ", "")
    if not text:
        raise ValueError("memory_limit is empty")
    if text.isdigit():
        value = int(text)
        if value <= 0:
            raise ValueError("memory_limit must be positive")
        return value
    if len(text) < 2 or not text[:-1].isdigit():
        raise ValueError(f"invalid memory_limit: {raw}")
    suffix = text[-1].upper()
    multiplier = {
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
    }.get(suffix)
    if multiplier is None:
        raise ValueError(f"invalid memory_limit suffix: {raw}")
    value = int(text[:-1]) * multiplier
    if value <= 0:
        raise ValueError("memory_limit must be positive")
    return value


def _recreate_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _cleanup_db_dir_if_requested(eval_config: dict[str, Any], db_dir: Path) -> None:
    cleanup = eval_config.get("cleanup_db_dir", True)
    if bool(cleanup):
        shutil.rmtree(db_dir, ignore_errors=True)
        logger.info("Removed database directory %s (cleanup_db_dir=true)", db_dir)
    else:
        logger.info("Leaving database directory %s in place (cleanup_db_dir=false)", db_dir)

