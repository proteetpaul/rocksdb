"""Tests for OpenEvolve db_bench evaluator."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluate.evaluator import evaluate


class TestOpenEvolveEvaluator(unittest.TestCase):
    def test_successful_run_uses_systemd_memory_limit_and_parses_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            candidate = root / "candidate.json"
            runtime_config = root / "runtime.json"
            db_dir = root / "db"

            bench_ini.write_text(
                "num=1000\nkey_size=16\nvalue_size=128\nthreads=4\nduration=10\nhistogram=true\n",
                encoding="utf-8",
            )
            load_ini.write_text("benchmarks=fillrandom\nthreads=1\n", encoding="utf-8")
            workload_ini.write_text(
                "benchmarks=readrandomwriterandom\nreadwritepercent=50\n",
                encoding="utf-8",
            )
            candidate.write_text(
                json.dumps({"db_bench_flags": {"max_background_jobs": 8}}),
                encoding="utf-8",
            )
            runtime_config.write_text(
                json.dumps(
                    {
                        "bench_ini": str(bench_ini),
                        "load_ini": str(load_ini),
                        "workload_ini": str(workload_ini),
                        "memory_limit": "2G",
                        "cleanup_db_dir": False,
                    }
                ),
                encoding="utf-8",
            )

            load_output = "fillrandom : 4.0 micros/op 20000 ops/sec 1.0 seconds 10000 operations\n"
            run_output = (
                "readrandomwriterandom : 10.000 micros/op 50000 ops/sec 1.000 seconds 1000000 operations\n"
                "Microseconds per read:\n"
                "Percentiles: P50: 4.50 P75: 6.00 P99: 18.00\n"
                "Microseconds per write:\n"
                "Percentiles: P50: 7.00 P75: 10.00 P99: 25.00\n"
                "STATISTICS:\n"
                "rocksdb.compact.read.bytes COUNT : 111\n"
                "rocksdb.compaction.times.micros P50 : 1 P99 : 9\n"
                "rocksdb.block.cache.hit COUNT : 12345\n"
            )

            captured_commands: list[list[str]] = []

            def fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
                captured_commands.append(list(argv))
                if len(captured_commands) == 1:
                    return subprocess.CompletedProcess(argv, 0, stdout=load_output)
                return subprocess.CompletedProcess(argv, 0, stdout=run_output)

            with mock.patch.dict(
                os.environ,
                {
                    "OPENEVOLVE_EVAL_CONFIG": str(runtime_config),
                    "DB_BENCH": "/bin/true",
                    "DB_DIR": str(db_dir),
                },
                clear=False,
            ), mock.patch("evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"), mock.patch(
                "evaluate.evaluator.subprocess.run",
                side_effect=fake_run,
            ):
                result = evaluate(str(candidate))

            self.assertEqual(result.artifacts.get("status"), "ok")
            self.assertEqual(result.metrics["combined_score"], 50000.0)
            self.assertEqual(result.metrics["throughput_qps"], 50000.0)
            self.assertEqual(result.metrics["read_p99_us"], 18.0)
            self.assertEqual(result.metrics["write_p99_us"], 25.0)
            self.assertIn("rocksdb.compact.read.bytes.count", result.metrics)
            self.assertIn("rocksdb.compaction.times.micros.p99", result.metrics)
            self.assertNotIn("rocksdb.block.cache.hit.count", result.metrics)

            self.assertEqual(len(captured_commands), 2)
            for argv in captured_commands:
                self.assertEqual(argv[0:4], ["systemd-run", "--user", "--scope", "-p"])
                self.assertEqual(argv[4], f"MemoryMax={2 * 1024**3}")
                self.assertIn("--statistics=true", argv)

    def test_missing_required_memory_limit_returns_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            candidate = root / "candidate.json"
            runtime_config = root / "runtime.json"

            bench_ini.write_text("num=1000\n", encoding="utf-8")
            load_ini.write_text("benchmarks=fillrandom\n", encoding="utf-8")
            workload_ini.write_text("benchmarks=readrandomwriterandom\n", encoding="utf-8")
            candidate.write_text("{}", encoding="utf-8")
            runtime_config.write_text(
                json.dumps(
                    {
                        "bench_ini": str(bench_ini),
                        "load_ini": str(load_ini),
                        "workload_ini": str(workload_ini),
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "OPENEVOLVE_EVAL_CONFIG": str(runtime_config),
                    "DB_BENCH": "/bin/true",
                    "DB_DIR": str(root / "db"),
                },
                clear=False,
            ), mock.patch("evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"):
                result = evaluate(str(candidate))

            self.assertEqual(result.metrics["combined_score"], 0.0)
            self.assertEqual(result.artifacts.get("status"), "error")
            self.assertIn("memory_limit", result.artifacts.get("error", ""))

    def test_workload_failure_returns_zero_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            candidate = root / "candidate.json"
            runtime_config = root / "runtime.json"
            db_dir = root / "db"

            bench_ini.write_text("num=1000\n", encoding="utf-8")
            load_ini.write_text("benchmarks=fillrandom\n", encoding="utf-8")
            workload_ini.write_text("benchmarks=readrandomwriterandom\n", encoding="utf-8")
            candidate.write_text("{}", encoding="utf-8")
            runtime_config.write_text(
                json.dumps(
                    {
                        "bench_ini": str(bench_ini),
                        "load_ini": str(load_ini),
                        "workload_ini": str(workload_ini),
                        "memory_limit": "1G",
                    }
                ),
                encoding="utf-8",
            )

            def fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
                if "--benchmarks=fillrandom" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="ok\n")
                return subprocess.CompletedProcess(argv, 1, stdout="run failed\n")

            with mock.patch.dict(
                os.environ,
                {
                    "OPENEVOLVE_EVAL_CONFIG": str(runtime_config),
                    "DB_BENCH": "/bin/true",
                    "DB_DIR": str(db_dir),
                },
                clear=False,
            ), mock.patch("evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"), mock.patch(
                "evaluate.evaluator.subprocess.run",
                side_effect=fake_run,
            ):
                result = evaluate(str(candidate))

            self.assertEqual(result.metrics["combined_score"], 0.0)
            self.assertEqual(result.artifacts.get("status"), "run_failed")


if __name__ == "__main__":
    unittest.main()
