"""db_bench execution helpers for the Dremel controller."""

from __future__ import annotations

import os
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .options_file import write_options_file
from .parameter_space import DremelConfig, cli_flags, option_overrides
from .parse_db_bench import DbBenchMetrics, parse_db_bench_output

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DbBenchRunResult:
    config: DremelConfig
    duration_sec: int
    metrics: DbBenchMetrics
    status: str
    log_path: Path

    @property
    def reward(self) -> float:
        return self.metrics.throughput_qps or 0.0


@dataclass(frozen=True)
class DbBenchContext:
    db_bench: Path
    bench_ini: Path
    base_options: Path
    workload_dir: Path
    out_dir: Path
    num: str = "1000000"
    key_size: str = "16"
    value_size: str = "1024"
    threads: str = "16"
    compression_type: str = "none"
    histogram: str = "true"
    statistics: str = "false"
    report_interval_seconds: str = "1"
    report_dir: str = "bench/reports"
    memory_limit: str | None = None
    timeout_sec: float | None = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def find_db_bench(repo: Path, override: Path | None = None) -> Path:
    if override is not None:
        candidate = override.expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise FileNotFoundError(f"db_bench not executable: {candidate}")

    env_bin = os.environ.get("DB_BENCH")
    if env_bin:
        candidate = Path(env_bin).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    for candidate in (repo / "build" / "db_bench", repo / "db_bench"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    which = shutil.which("db_bench")
    if which:
        return Path(which).resolve()
    raise FileNotFoundError("db_bench not found. Build it with `make db_bench` or pass --db-bench.")


def load_simple_ini(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def run_db_bench_evaluation(
    context: DbBenchContext,
    config: DremelConfig,
    *,
    duration_sec: int,
) -> DbBenchRunResult:
    """Run load + workload for one Dremel candidate and parse throughput."""

    logger.info(
        "Evaluating config %s for %ss with db_bench",
        config.index,
        duration_sec,
    )
    context.out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = context.out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"config_{config.index}_duration_{duration_sec}.log"

    db_path = Path(os.environ.get("DB_DIR") or "/tmp/rocksdb_dremel").expanduser()

    load_flags = _ini_to_argv(load_simple_ini(context.workload_dir / "load.ini"))
    workload_flags = _ini_to_argv(load_simple_ini(context.workload_dir / "workload_a.ini"))
    extra_flags = cli_flags(config.params)
    logger.info(
        "Running config %s under systemd scope%s",
        config.index,
        f" with MemoryMax={context.memory_limit}" if context.memory_limit else "",
    )

    fd, options_path_raw = tempfile.mkstemp(
        prefix=f"dremel_options_{config.index}_",
        suffix=".ini",
        dir=tempfile.gettempdir(),
    )
    os.close(fd)
    options_path = Path(options_path_raw)
    full_log: list[str] = []

    try:
        write_options_file(context.base_options, options_path, option_overrides(config.params))
        shutil.rmtree(db_path, ignore_errors=True)
        db_path.mkdir(parents=True, exist_ok=True)

        common_flags = [
            f"--db={db_path}",
            f"--options_file={options_path}",
            f"--num={context.num}",
            f"--key_size={context.key_size}",
            f"--value_size={context.value_size}",
            f"--compression_type={context.compression_type}",
            f"--histogram={context.histogram}",
            f"--statistics={context.statistics}",
            *extra_flags,
        ]

        load_argv = _scoped_argv(context, common_flags + load_flags)
        full_log.append(_argv_line(load_argv))
        load_cp = _run(load_argv, timeout_sec=context.timeout_sec)
        full_log.append(load_cp.stdout or "")
        if load_cp.returncode != 0:
            logger.warning(
                "Load phase failed for config %s with exit code %s",
                config.index,
                load_cp.returncode,
            )
            return _finish(config, duration_sec, "load_failed", full_log, log_path)

        run_flags = [
            *common_flags,
            f"--threads={context.threads}",
            f"--duration={duration_sec}",
            *_report_flags(context, config, duration_sec),
            *workload_flags,
        ]
        run_argv = _scoped_argv(context, run_flags)
        full_log.append("\n" + _argv_line(run_argv))
        run_cp = _run(run_argv, timeout_sec=context.timeout_sec)
        full_log.append(run_cp.stdout or "")
        status = "ok" if run_cp.returncode == 0 else "run_failed"
        if run_cp.returncode != 0:
            logger.warning(
                "Run phase failed for config %s at %ss with exit code %s",
                config.index,
                duration_sec,
                run_cp.returncode,
            )
        if status == "ok" and parse_db_bench_output("".join(full_log)).throughput_qps is None:
            status = "parse_incomplete"
            logger.warning(
                "Could not parse throughput for config %s at %ss; see %s",
                config.index,
                duration_sec,
                log_path,
            )
        return _finish(config, duration_sec, status, full_log, log_path)
    except subprocess.TimeoutExpired as exc:
        full_log.append(str(exc))
        logger.warning(
            "db_bench timed out for config %s at %ss after %s seconds",
            config.index,
            duration_sec,
            context.timeout_sec,
        )
        return _finish(config, duration_sec, "timeout", full_log, log_path)
    finally:
        if options_path.is_file():
            options_path.unlink()
        shutil.rmtree(db_path, ignore_errors=True)


def result_row(result: DbBenchRunResult) -> dict[str, Any]:
    metrics = result.metrics
    row: dict[str, Any] = {
        "config_index": result.config.index,
        "duration_sec": result.duration_sec,
        "reward_iops": result.reward,
        "throughput_qps": "" if metrics.throughput_qps is None else metrics.throughput_qps,
        "read_p50_us": "" if metrics.read_p50_us is None else metrics.read_p50_us,
        "read_p99_us": "" if metrics.read_p99_us is None else metrics.read_p99_us,
        "write_p50_us": "" if metrics.write_p50_us is None else metrics.write_p50_us,
        "write_p99_us": "" if metrics.write_p99_us is None else metrics.write_p99_us,
        "status": result.status,
        "log_path": str(result.log_path),
    }
    row.update({name: value for name, value in sorted(result.config.params.items())})
    return row


def _ini_to_argv(values: dict[str, str]) -> list[str]:
    return [f"--{key}={value}" for key, value in values.items()]


def _report_flags(context: DbBenchContext, config: DremelConfig, duration_sec: int) -> list[str]:
    try:
        interval = int(context.report_interval_seconds)
    except ValueError:
        interval = 0
    if interval <= 0:
        return []

    report_dir = Path(context.report_dir)
    if not report_dir.is_absolute():
        report_dir = repo_root() / report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"dremel_config_{config.index}_duration_{duration_sec}.csv"
    return [
        f"--report_interval_seconds={interval}",
        f"--report_file={report_file}",
    ]


def _scoped_argv(context: DbBenchContext, db_bench_flags: list[str]) -> list[str]:
    inner = [str(context.db_bench), *db_bench_flags]
    if context.memory_limit is None:
        return ["systemd-run", "--user", "--scope", "--", *inner]
    return ["systemd-run", "--user", "--scope", "-p", f"MemoryMax={context.memory_limit}", "--", *inner]


def _run(argv: list[str], *, timeout_sec: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_sec,
    )


def _finish(
    config: DremelConfig,
    duration_sec: int,
    status: str,
    full_log: list[str],
    log_path: Path,
) -> DbBenchRunResult:
    text = "".join(full_log)
    log_path.write_text(text, encoding="utf-8")
    return DbBenchRunResult(
        config=config,
        duration_sec=duration_sec,
        metrics=parse_db_bench_output(text),
        status=status,
        log_path=log_path,
    )


def _argv_line(argv: list[str]) -> str:
    return " ".join(argv) + "\n\n"

