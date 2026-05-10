"""OpenEvolve evaluator that runs RocksDB db_bench workloads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dremel.options_file import write_options_file
from dremel.parse_db_bench import parse_db_bench_output

from evaluate.parse_stats import parse_compaction_statistics

logger = logging.getLogger(__name__)

@dataclass
class EvaluationResult:
    metrics: dict[str, float]
    artifacts: dict[str, str] = field(default_factory=dict)


def _load_openevolve_evaluation_result() -> type[EvaluationResult]:
    module_path = (
        REPO_ROOT
        / "evolve-compaction"
        / "openevolve"
        / "openevolve"
        / "evaluation_result.py"
    )
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


def evaluate(program_path: str) -> EvaluationResult:
    """OpenEvolve-compatible evaluator entrypoint."""
    logger.info("evaluate() starting program_path=%s", program_path)
    try:
        runtime_config_path = _required_env_path("OPENEVOLVE_EVAL_CONFIG")
        runtime_config = _load_json(runtime_config_path)
        candidate_config = _load_json(Path(program_path))
        logger.info(
            "Loaded runtime config from %s and candidate config from %s",
            runtime_config_path,
            Path(program_path).resolve(),
        )

        db_bench = _required_executable_from_env("DB_BENCH")
        db_dir = _required_env_path("DB_DIR")
        _ensure_systemd_run()

        memory_limit_text = _required_str(runtime_config, "memory_limit")
        memory_limit_bytes = _parse_memory_limit_bytes(memory_limit_text)
        timeout_sec = _optional_float(runtime_config.get("timeout_sec"))
        logger.info(
            "db_bench=%s db_dir=%s memory_limit=%s (%s bytes) timeout_sec=%s",
            db_bench,
            db_dir,
            memory_limit_text,
            memory_limit_bytes,
            timeout_sec,
        )

        bench_values = _load_simple_ini(_resolve_path(runtime_config, "bench_ini", default="bench/bench.ini"))
        load_values = _load_simple_ini(_resolve_path(runtime_config, "load_ini", default="bench/workloads/load.ini"))
        workload_values = _load_simple_ini(_resolve_path(runtime_config, "workload_ini"))

        db_bench_flags = _candidate_db_bench_flags(candidate_config)
        common_flags = _common_db_bench_flags(runtime_config, bench_values, db_dir, db_bench_flags)
        options_file_path = _maybe_build_options_file(runtime_config, candidate_config)

        if options_file_path is not None:
            common_flags.append(f"--options_file={options_file_path}")
            logger.info("Using patched options file %s", options_file_path)

        load_flags = _ini_to_argv(load_values)
        workload_flags = _ini_to_argv(workload_values)

        full_log: list[str] = []
        try:
            _recreate_dir(db_dir)
            logger.info("Recreated database directory %s", db_dir)

            load_argv = _scoped_argv(db_bench, common_flags + load_flags, memory_limit_bytes)
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

            run_argv = _scoped_argv(db_bench, common_flags + workload_flags, memory_limit_bytes)
            full_log.append("\n" + _argv_line(run_argv))
            logger.info("Starting db_bench workload phase (MemoryMax=%s)", memory_limit_bytes)
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
            compaction_stats = parse_compaction_statistics(output_text)

            throughput = perf_metrics.throughput_qps or 0.0
            read_p99 = perf_metrics.read_p99_us or 0.0
            write_p99 = perf_metrics.write_p99_us or 0.0
            if perf_metrics.throughput_qps is None:
                logger.warning(
                    "Could not parse throughput from db_bench output; combined_score will be 0.0"
                )

            metrics: dict[str, float] = {
                "combined_score": throughput,
                "throughput_qps": throughput,
                "read_p99_us": read_p99,
                "write_p99_us": write_p99,
                "read_p50_us": perf_metrics.read_p50_us or 0.0,
                "write_p50_us": perf_metrics.write_p50_us or 0.0,
            }
            metrics.update(compaction_stats)

            logger.info(
                "Evaluation ok throughput_qps=%s read_p99_us=%s write_p99_us=%s compaction_stat_keys=%s",
                perf_metrics.throughput_qps,
                perf_metrics.read_p99_us,
                perf_metrics.write_p99_us,
                len(compaction_stats),
            )
            return EvaluationResult(
                metrics=metrics,
                artifacts={
                    "status": "ok",
                    "runtime_config_path": str(runtime_config_path),
                    "candidate_config_path": str(Path(program_path).resolve()),
                    "memory_limit": memory_limit_text,
                    "db_bench_path": str(db_bench),
                    "db_dir": str(db_dir),
                    "log": output_text,
                },
            )
        finally:
            if options_file_path is not None and options_file_path.is_file():
                options_file_path.unlink()
            _cleanup_db_dir_if_requested(runtime_config, db_dir)
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
        path = REPO_ROOT / path
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


def _candidate_db_bench_flags(candidate_config: dict[str, Any]) -> list[str]:
    flags = candidate_config.get("db_bench_flags", {})
    if flags is None:
        return []
    if not isinstance(flags, dict):
        raise ValueError("candidate db_bench_flags must be a JSON object")
    out: list[str] = []
    for key, value in sorted(flags.items()):
        out.append(f"--{key}={_to_cli_value(value)}")
    return out


def _common_db_bench_flags(
    runtime_config: dict[str, Any],
    bench_values: dict[str, str],
    db_dir: Path,
    candidate_flags: list[str],
) -> list[str]:
    merged: dict[str, Any] = dict(bench_values)
    if "bench_overrides" in runtime_config:
        overrides = runtime_config["bench_overrides"]
        if not isinstance(overrides, dict):
            raise ValueError("bench_overrides must be a JSON object")
        merged.update(overrides)

    merged.pop("db", None)
    merged["db"] = str(db_dir)
    merged["statistics"] = "true"

    user_common = runtime_config.get("common_db_bench_flags", {})
    if user_common is not None:
        if not isinstance(user_common, dict):
            raise ValueError("common_db_bench_flags must be a JSON object")
        merged.update(user_common)
    merged["statistics"] = "true"

    cli_flags = [f"--{key}={_to_cli_value(value)}" for key, value in sorted(merged.items())]
    return cli_flags + candidate_flags


def _maybe_build_options_file(
    runtime_config: dict[str, Any],
    candidate_config: dict[str, Any],
) -> Path | None:
    if "base_options_file" not in runtime_config:
        return None
    base_options = _resolve_path(runtime_config, "base_options_file")
    overrides = candidate_config.get("options_overrides", {})
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


def _error_result(status: str, message: str, extra_artifacts: dict[str, str] | None = None) -> EvaluationResult:
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


def _cleanup_db_dir_if_requested(runtime_config: dict[str, Any], db_dir: Path) -> None:
    cleanup = runtime_config.get("cleanup_db_dir", True)
    if bool(cleanup):
        shutil.rmtree(db_dir, ignore_errors=True)
        logger.info("Removed database directory %s (cleanup_db_dir=true)", db_dir)
    else:
        logger.info("Leaving database directory %s in place (cleanup_db_dir=false)", db_dir)
