"""Run db_bench for each LHS sample: temp options, load, workload A, du, CSV, resume.

Each ``db_bench`` runs under ``systemd-run --user --scope`` with cgroup ``MemoryMax``
(see ``bench/ycsb_bench.sh --memory_limit``). Cap via ``--memory-limit`` or
``LHS_MEMORY_LIMIT`` (default ``32G``).
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .lhs_core import read_samples_file
from .options_writer import params_to_overrides, write_options_file
from .parse_db_bench import DbBenchMetrics, parse_db_bench_output, parse_du_sh_stdout


def repo_root() -> Path:
    """RocksDB repository root (parent of bench/)."""
    return Path(__file__).resolve().parents[2]


def find_db_bench(repo: Path, override: Path | None) -> Path:
    if override is not None:
        p = override.expanduser().resolve()
        if not p.is_file() or not os.access(p, os.X_OK):
            raise FileNotFoundError(f"db_bench not executable: {p}")
        return p
    env_bin = os.environ.get("DB_BENCH")
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
    for candidate in (
        repo / "build" / "db_bench",
        repo / "db_bench",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    which = shutil.which("db_bench")
    if which:
        return Path(which).resolve()
    raise FileNotFoundError(
        "db_bench not found. Build RocksDB (e.g. make db_bench), set DB_BENCH, "
        "or pass --db-bench to run_lhs_experiments.py"
    )


def load_simple_ini(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _ini_to_argv(d: dict[str, str]) -> list[str]:
    return [f"--{k}={v}" for k, v in d.items()]


def parse_memory_limit_bytes(raw: str) -> int:
    """
    Parse a cap like ``8G``, ``512M``, or plain bytes (rules aligned with
    ``bench/ycsb_bench.sh --memory_limit``). Must be positive.
    """
    s = raw.strip().replace(" ", "")
    if not s:
        raise ValueError("memory limit is empty")
    up = s.upper()
    if up in ("NONE", "0"):
        raise ValueError("LHS requires a positive memory limit for cgroups MemoryMax")
    if s.isdigit():
        n = int(s)
        if n <= 0:
            raise ValueError("memory limit in bytes must be positive")
        return n
    if len(s) >= 2 and s[:-1].isdigit():
        suf = s[-1].upper()
        if suf in "KMGTP":
            n = int(s[:-1])
            mult = {
                "K": 1024,
                "M": 1024**2,
                "G": 1024**3,
                "T": 1024**4,
                "P": 1024**5,
            }[suf]
            return n * mult
    raise ValueError(
        f"Invalid memory limit {raw!r}; use e.g. 8G, 512M, or a positive byte count"
    )


def _db_bench_scoped_argv(db_bin: Path, db_bench_flags: list[str], memory_max_bytes: int) -> list[str]:
    """Run db_bench inside a transient user scope with MemoryMax."""
    return [
        "systemd-run",
        "--user",
        "--scope",
        "-p",
        f"MemoryMax={memory_max_bytes}",
        "--",
        str(db_bin),
        *db_bench_flags,
    ]


def _read_last_success(path: Path) -> int:
    if not path.is_file():
        return -1
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return -1


def _write_last_success(path: Path, idx: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(idx) + "\n", encoding="utf-8")


def _run_subprocess(
    argv: list[str],
    *,
    timeout_sec: float | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_sec,
    )


def _du_sh(db_path: Path) -> str | None:
    try:
        cp = subprocess.run(
            ["du", "-sh", str(db_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if cp.returncode != 0:
            return None
        return parse_du_sh_stdout(cp.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return None


def _ensure_csv_header(path: Path, fieldnames: list[str]) -> None:
    if path.is_file() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()


def _append_csv(path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writerow(row)


def run_all_samples(
    *,
    samples_path: Path,
    bench_ini: Path,
    base_options: Path,
    workload_dir: Path,
    results_csv: Path,
    last_success_path: Path,
    logs_dir: Path,
    db_bench: Path | None = None,
    force: bool = False,
    start_index: int | None = None,
    workload_duration_sec: int = 300,
    subprocess_timeout_sec: float | None = None,
    memory_limit: str | None = None,
) -> None:
    """
    Execute each sample: rm DB, temp options INI, load, workload A, du, parse,
    append CSV, unlink options, rm DB. Updates last_success_path on full success
    (load + run OK and throughput parsed).

    ``memory_limit`` sets cgroup MemoryMax for each db_bench (e.g. ``16G``).
    If omitted, uses env ``LHS_MEMORY_LIMIT`` when set, otherwise ``32G``.
    """
    repo = repo_root()
    if shutil.which("systemd-run") is None:
        raise RuntimeError(
            "systemd-run not found on PATH; LHS experiments require it for cgroups "
            "MemoryMax (see bench/ycsb_bench.sh --memory_limit)."
        )
    mem_raw = memory_limit if memory_limit is not None else os.environ.get("LHS_MEMORY_LIMIT", "32G")
    memory_max_bytes = parse_memory_limit_bytes(mem_raw)

    specs, samples, _meta = read_samples_file(samples_path)
    db_bin = find_db_bench(repo, db_bench)

    bench_vars = load_simple_ini(bench_ini)
    db_path = Path(os.environ.get("DB_DIR") or bench_vars.get("db", "/tmp/rocksdb_ycsb_bench")).expanduser()
    num = bench_vars.get("num", "1000000")
    key_size = bench_vars.get("key_size", "16")
    value_size = bench_vars.get("value_size", "1024")
    threads = bench_vars.get("threads", "16")
    compression_type = bench_vars.get("compression_type", "none")
    statistics = bench_vars.get("statistics", "false")

    load_ini = workload_dir / "load.ini"
    workload_a = workload_dir / "workload_a.ini"
    load_flags = _ini_to_argv(load_simple_ini(load_ini))
    wl_flags = _ini_to_argv(load_simple_ini(workload_a))

    param_keys = sorted(s.name for s in specs)
    fieldnames = (
        ["sample_index"]
        + param_keys
        + [
            "read_p50_us",
            "read_p99_us",
            "write_p50_us",
            "write_p99_us",
            "throughput_qps",
            "db_disk_du_sh",
            "status",
            "log_path",
        ]
    )

    last_done = _read_last_success(last_success_path)
    logs_dir.mkdir(parents=True, exist_ok=True)

    for sample in sorted(samples, key=lambda s: int(s["index"])):
        idx = int(sample["index"])
        if start_index is not None and idx < start_index:
            continue
        if not force and idx <= last_done:
            continue

        params: dict[str, str] = dict(sample["params"])
        log_path = logs_dir / f"run_{idx}.log"
        row_base: dict[str, Any] = {"sample_index": idx, "log_path": str(log_path)}
        for k in param_keys:
            row_base[k] = params.get(k, "")

        def finalize(
            status: str,
            text: str,
            metrics: DbBenchMetrics | None = None,
            du_s: str | None = None,
        ) -> None:
            m = metrics
            row = {
                **row_base,
                "read_p50_us": "" if m is None or m.read_p50_us is None else m.read_p50_us,
                "read_p99_us": "" if m is None or m.read_p99_us is None else m.read_p99_us,
                "write_p50_us": "" if m is None or m.write_p50_us is None else m.write_p50_us,
                "write_p99_us": "" if m is None or m.write_p99_us is None else m.write_p99_us,
                "throughput_qps": "" if m is None or m.throughput_qps is None else m.throughput_qps,
                "db_disk_du_sh": du_s or "",
                "status": status,
            }
            _ensure_csv_header(results_csv, fieldnames)
            _append_csv(results_csv, fieldnames, row)
            log_path.write_text(text, encoding="utf-8")

        shutil.rmtree(db_path, ignore_errors=True)
        db_path.mkdir(parents=True, exist_ok=True)

        fd, options_path_str = tempfile.mkstemp(
            prefix=f"lhs_options_{idx}_",
            suffix=".ini",
            dir=tempfile.gettempdir(),
        )
        os.close(fd)
        options_path = Path(options_path_str)
        full_log: list[str] = []

        try:
            overrides = params_to_overrides(params, specs)
            write_options_file(base_options, options_path, overrides)

            common_flags = [
                f"--db={db_path}",
                f"--options_file={options_path}",
                f"--num={num}",
                f"--key_size={key_size}",
                f"--value_size={value_size}",
                f"--compression_type={compression_type}",
            ]
            if statistics.lower() == "true":
                common_flags.append("--statistics=true")

            load_inner = list(common_flags)
            if bench_vars.get("histogram", "false").lower() == "true":
                load_inner.append("--histogram=true")
            load_inner.extend(load_flags)
            load_argv = _db_bench_scoped_argv(db_bin, load_inner, memory_max_bytes)
            full_log.append(" ".join(load_argv) + "\n\n")
            try:
                cp_load = _run_subprocess(load_argv, timeout_sec=subprocess_timeout_sec)
            except subprocess.TimeoutExpired as e:
                full_log.append(str(e))
                finalize("load_timeout", "".join(full_log))
                shutil.rmtree(db_path, ignore_errors=True)
                continue
            full_log.append(cp_load.stdout or "")
            if cp_load.returncode != 0:
                finalize("load_failed", "".join(full_log))
                shutil.rmtree(db_path, ignore_errors=True)
                continue

            run_inner = list(common_flags) + [
                f"--threads={threads}",
                f"--duration={workload_duration_sec}",
                "--histogram=true",
            ] + wl_flags
            run_argv = _db_bench_scoped_argv(db_bin, run_inner, memory_max_bytes)
            full_log.append("\n" + " ".join(run_argv) + "\n\n")
            try:
                cp_run = _run_subprocess(run_argv, timeout_sec=subprocess_timeout_sec)
            except subprocess.TimeoutExpired as e:
                full_log.append(str(e))
                finalize("run_timeout", "".join(full_log))
                shutil.rmtree(db_path, ignore_errors=True)
                continue
            full_log.append(cp_run.stdout or "")
            combined = "".join(full_log)

            if cp_run.returncode != 0:
                finalize("run_failed", combined)
                shutil.rmtree(db_path, ignore_errors=True)
                continue

            du_s = _du_sh(db_path)
            metrics = parse_db_bench_output(combined)

            if metrics.throughput_qps is None:
                finalize("parse_incomplete", combined, metrics=metrics, du_s=du_s)
            else:
                finalize("ok", combined, metrics=metrics, du_s=du_s)
                _write_last_success(last_success_path, idx)

        finally:
            try:
                if options_path.is_file():
                    options_path.unlink()
            except OSError:
                pass

        shutil.rmtree(db_path, ignore_errors=True)
