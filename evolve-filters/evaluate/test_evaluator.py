"""Tests for OpenEvolve db_bench evaluator."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluate.evaluator import _force_evolve_dummy_filter_policy, evaluate


class TestOpenEvolveEvaluator(unittest.TestCase):
    def test_successful_run_uses_systemd_memory_limit_and_parses_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            evolved_cc = root / "evolved_filter_policy.cc"
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
            evolved_cc.write_text("// evolved filter policy stub\n", encoding="utf-8")
            runtime_config.write_text(
                json.dumps(
                    {
                        "bench_ini": str(bench_ini),
                        "load_ini": str(load_ini),
                        "workload_ini": str(workload_ini),
                        "memory_limit": "2G",
                        "cleanup_db_dir": False,
                        "db_bench_flags": {"max_background_jobs": 8},
                    }
                ),
                encoding="utf-8",
            )

            load_output = (
                "fillrandom : 4.0 micros/op 20000 ops/sec 1.0 seconds 10000 operations\n"
            )
            run_output = (
                "readrandomwriterandom : 10.000 micros/op 50000 ops/sec 1.000 seconds 1000000 operations\n"
                "Microseconds per read:\n"
                "Percentiles: P50: 4.50 P75: 6.00 P99: 18.00\n"
                "Microseconds per write:\n"
                "Percentiles: P50: 7.00 P75: 10.00 P99: 25.00\n"
                "STATISTICS:\n"
                "rocksdb.bloom.filter.useful COUNT : 100\n"
                "rocksdb.bloom.filter.full.positive COUNT : 40\n"
                "rocksdb.bloom.filter.full.true.positive COUNT : 30\n"
                "rocksdb.bloom.filter.prefix.checked COUNT : 12\n"
                "rocksdb.bloom.filter.prefix.useful COUNT : 9\n"
                "rocksdb.bloom.filter.prefix.true.positive COUNT : 2\n"
                "rocksdb.compute.bits.per.key.micros P50 : 3 P95 : 8 P99 : 12 COUNT : 250\n"
                "Level[0]: # entries=100 rocksdb.filter.size: 1024\n"
                "Level[1]: # entries=200 rocksdb.filter.size: 2048\n"
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
            ), mock.patch(
                "evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"
            ), mock.patch(
                "evaluate.evaluator._install_evolved_filter_policy_source",
                return_value=(True, {}),
            ), mock.patch(
                "evaluate.evaluator._build_rocksdb_with_cmake", return_value=(True, {})
            ), mock.patch(
                "evaluate.evaluator.subprocess.run",
                side_effect=fake_run,
            ):
                result = evaluate(str(evolved_cc))

            self.assertEqual(result.artifacts.get("status"), "ok")
            self.assertEqual(
                result.artifacts.get("evolved_source_path"), str(evolved_cc.resolve())
            )
            self.assertGreater(result.metrics["combined_score"], 0.0)
            self.assertEqual(result.metrics["throughput_qps"], 50000.0)
            self.assertEqual(result.metrics["read_p99_us"], 18.0)
            self.assertEqual(result.metrics["write_p99_us"], 25.0)
            self.assertEqual(result.metrics["filter_memory_usage"], 3072.0)
            self.assertIn("rocksdb.bloom.filter.useful.count", result.metrics)
            self.assertIn("rocksdb.bloom.filter.full.positive.count", result.metrics)
            self.assertIn("bloom.full.observed_fp_rate", result.metrics)
            self.assertEqual(
                result.metrics["rocksdb.compute.bits.per.key.micros.p95"], 8.0
            )
            self.assertNotIn("rocksdb.compute.bits.per.key.micros.p50", result.metrics)
            self.assertNotIn("rocksdb.compute.bits.per.key.micros.p99", result.metrics)
            self.assertNotIn("rocksdb.compute.bits.per.key.micros.count", result.metrics)
            self.assertNotIn("rocksdb.compaction.times.micros.p99", result.metrics)

            self.assertEqual(len(captured_commands), 2)
            load_argv, workload_argv = captured_commands
            for argv in (load_argv, workload_argv):
                self.assertEqual(argv[0:4], ["systemd-run", "--user", "--scope", "-p"])
                self.assertEqual(argv[4], f"MemoryMax={2 * 1024**3}")
            self.assertIn("--show_table_properties=false", load_argv)
            self.assertIn("--show_table_properties=true", workload_argv)
            self.assertIn("--stats_interval=0", load_argv)
            self.assertIn("--statistics=false", load_argv)
            self.assertIn("--statistics=true", workload_argv)
            self.assertIn("--stats_interval=0", workload_argv)

    def test_force_evolve_dummy_filter_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_options = root / "base.ini"
            base_options.write_text(
                '\n'.join(
                    [
                        '[Version]',
                        '  rocksdb_version=11.1.0',
                        '[TableOptions/BlockBasedTable "default"]',
                        "  filter_policy=rocksdb.BloomFilter:10:false",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            eval_config: dict[str, object] = {
                "base_options_file": str(base_options),
                "options_overrides": {
                    '[TableOptions/BlockBasedTable "default"]': {
                        "block_size": "4096"
                    }
                },
            }

            _force_evolve_dummy_filter_policy(eval_config)

            section = eval_config["options_overrides"][
                '[TableOptions/BlockBasedTable "default"]'
            ]
            self.assertEqual(section["filter_policy"], "rocksdb.EvolveDummyFilter")
            self.assertEqual(section["block_size"], "4096")

    def test_missing_required_memory_limit_returns_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            evolved_cc = root / "evolved_filter_policy.cc"
            runtime_config = root / "runtime.json"

            bench_ini.write_text("num=1000\n", encoding="utf-8")
            load_ini.write_text("benchmarks=fillrandom\n", encoding="utf-8")
            workload_ini.write_text("benchmarks=readrandomwriterandom\n", encoding="utf-8")
            evolved_cc.write_text("// stub\n", encoding="utf-8")
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
            ), mock.patch("evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"), mock.patch(
                "evaluate.evaluator._install_evolved_filter_policy_source",
                return_value=(True, {}),
            ), mock.patch(
                "evaluate.evaluator._build_rocksdb_with_cmake", return_value=(True, {})
            ):
                result = evaluate(str(evolved_cc))

            self.assertEqual(result.metrics["combined_score"], 0.0)
            self.assertEqual(result.artifacts.get("status"), "error")
            self.assertIn("memory_limit", result.artifacts.get("error", ""))

    def test_workload_failure_returns_zero_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            evolved_cc = root / "evolved_filter_policy.cc"
            runtime_config = root / "runtime.json"
            db_dir = root / "db"

            bench_ini.write_text("num=1000\n", encoding="utf-8")
            load_ini.write_text("benchmarks=fillrandom\n", encoding="utf-8")
            workload_ini.write_text("benchmarks=readrandomwriterandom\n", encoding="utf-8")
            evolved_cc.write_text("// stub\n", encoding="utf-8")
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
            ), mock.patch(
                "evaluate.evaluator.shutil.which", return_value="/usr/bin/systemd-run"
            ), mock.patch(
                "evaluate.evaluator._install_evolved_filter_policy_source",
                return_value=(True, {}),
            ), mock.patch(
                "evaluate.evaluator._build_rocksdb_with_cmake", return_value=(True, {})
            ), mock.patch(
                "evaluate.evaluator.subprocess.run",
                side_effect=fake_run,
            ):
                result = evaluate(str(evolved_cc))

            self.assertEqual(result.metrics["combined_score"], 0.0)
            self.assertEqual(result.artifacts.get("status"), "run_failed")


if __name__ == "__main__":
    unittest.main()

