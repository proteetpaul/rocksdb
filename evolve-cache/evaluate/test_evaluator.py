"""Tests for OpenEvolve cache tier db_bench evaluator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

EVOLVE_CACHE_EVAL = Path(__file__).resolve().parent
EVOLVE_CACHE_ROOT = EVOLVE_CACHE_EVAL.parent
WORKSPACE_ROOT = EVOLVE_CACHE_ROOT.parent

for path in (EVOLVE_CACHE_EVAL, WORKSPACE_ROOT, EVOLVE_CACHE_ROOT):
    s = str(path)
    if s not in sys.path:
        sys.path.insert(0, s)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "cache_tier_evaluator", EVOLVE_CACHE_EVAL / "evaluator.py"
)
assert _spec is not None and _spec.loader is not None
evaluator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluator)


class TestOpenEvolveCacheEvaluator(unittest.TestCase):
    def test_successful_run_installs_policy_and_parses_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "CMakeLists.txt").write_text("# stub\n", encoding="utf-8")
            (workspace / "cache").mkdir()
            (workspace / "cache" / "hit_rate_ghost_policy.h").write_text(
                "// original\n", encoding="utf-8"
            )

            bench_ini = root / "bench.ini"
            load_ini = root / "load.ini"
            workload_ini = root / "workload.ini"
            evolved_h = root / "evolved_policy.h"
            runtime_config = root / "runtime.json"
            db_dir = root / "db"
            db_bench = root / "db_bench"
            db_bench.write_text("#!/bin/sh\necho stub\n", encoding="utf-8")
            db_bench.chmod(0o755)

            bench_ini.write_text(
                "num=1000\nkey_size=16\nvalue_size=128\nthreads=4\nduration=10\nhistogram=true\n",
                encoding="utf-8",
            )
            load_ini.write_text("benchmarks=fillrandom\nthreads=1\n", encoding="utf-8")
            workload_ini.write_text(
                "benchmarks=readrandomwriterandom\nreadwritepercent=50\n",
                encoding="utf-8",
            )
            evolved_h.write_text("// evolved cache policy\n", encoding="utf-8")
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

            load_output = (
                "fillrandom : 4.0 micros/op 20000 ops/sec 1.0 seconds 10000 operations\n"
            )
            run_output = (
                "readrandomwriterandom : 10.000 micros/op 50000 ops/sec 1.000 seconds 1000000 operations\n"
                "Microseconds per read:\n"
                "Count: 1000 Average: 5.0000  StdDev: 1.00\n"
                "Percentiles: P50: 4.50 P75: 6.00 P99: 18.00\n"
                "STATISTICS:\n"
                "rocksdb.block.cache.hit COUNT : 9000\n"
                "rocksdb.block.cache.miss COUNT : 1000\n"
                "rocksdb.secondary.cache.hits COUNT : 300\n"
                "rocksdb.compressed.secondary.cache.hits COUNT : 200\n"
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
                    "WORKSPACE": str(workspace),
                    "DB_BENCH": str(db_bench),
                    "DB_DIR": str(db_dir),
                    "OPENEVOLVE_EVAL_CONFIG": str(runtime_config),
                },
                clear=False,
            ):
                with mock.patch.object(
                    evaluator,
                    "_build_rocksdb_with_cmake",
                    return_value=(True, {}),
                ):
                    with mock.patch.object(evaluator, "_run", side_effect=fake_run):
                        with mock.patch.object(evaluator, "_ensure_systemd_run"):
                            result = evaluator.evaluate(str(evolved_h))

            self.assertEqual(result.metrics["combined_score"], -5.0)
            self.assertEqual(result.metrics["throughput_qps"], 50000.0)
            self.assertEqual(result.metrics["mean_read_latency"], 5.0)
            self.assertEqual(result.metrics["read_p50_us"], 4.5)
            self.assertAlmostEqual(result.metrics["primary_cache_hit_rate"], 0.87)
            self.assertAlmostEqual(
                result.metrics["secondary_cache_hit_rate"], 300.0 / 1300.0
            )
            self.assertEqual(result.artifacts.get("status"), "ok")

            installed = (workspace / evaluator._EVOLVE_CACHE_POLICY_H_RELATIVE).read_text(
                encoding="utf-8"
            )
            self.assertEqual(installed, "// evolved cache policy\n")

            workload_argv = captured_commands[-1]
            joined = " ".join(workload_argv)
            self.assertIn("use_tiered_cache=true", joined)
            self.assertIn("cache_tier_controller_enabled=true", joined)
            self.assertIn("statistics=true", joined)
            self.assertIn("eval_warmup_sec=180", joined)
            self.assertIn("duration=240", joined)

    def test_apply_two_phase_eval_config_defaults(self) -> None:
        eval_config: dict = {"db_bench_flags": {}}
        warmup, measure, total = evaluator._apply_two_phase_eval_config(eval_config)
        self.assertEqual(warmup, 180)
        self.assertEqual(measure, 60)
        self.assertEqual(total, 240)
        self.assertEqual(eval_config["db_bench_flags"]["eval_warmup_sec"], "180")
        self.assertEqual(eval_config["bench_overrides"]["duration"], "240")

    def test_force_tiered_cache_eval_injects_flags_and_bloom_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options_ini = root / "options.ini"
            options_ini.write_text(
                '[TableOptions/BlockBasedTable "default"]\n'
                "  filter_policy=rocksdb.EvolveDummyFilter\n",
                encoding="utf-8",
            )
            eval_config: dict = {
                "base_options_file": str(options_ini),
                "db_bench_flags": {
                    "cache_size": "384M",
                    "compressed_secondary_cache_size": "128M",
                },
            }
            with mock.patch.object(
                evaluator,
                "_resolve_path",
                return_value=options_ini,
            ):
                evaluator._force_tiered_cache_eval(eval_config)

            self.assertEqual(eval_config["db_bench_flags"]["use_tiered_cache"], "true")
            self.assertEqual(
                eval_config["db_bench_flags"]["cache_tier_controller_enabled"], "true"
            )
            self.assertEqual(eval_config["db_bench_flags"]["cache_size"], "268435456")
            self.assertEqual(
                eval_config["db_bench_flags"]["compressed_secondary_cache_size"],
                "268435456",
            )
            self.assertEqual(eval_config["db_bench_flags"]["use_direct_reads"], "true")
            self.assertEqual(
                eval_config["db_bench_flags"]["cache_index_and_filter_blocks"], "false"
            )
            section = '[TableOptions/BlockBasedTable "default"]'
            self.assertEqual(
                eval_config["options_overrides"][section]["filter_policy"],
                "bloomfilter:10",
            )
            self.assertEqual(
                eval_config["options_overrides"][section][
                    "cache_index_and_filter_blocks"
                ],
                "false",
            )
            self.assertEqual(
                eval_config["options_overrides"]["[DBOptions]"]["use_direct_reads"],
                "true",
            )

    def test_force_tiered_cache_eval_user_cannot_override_enforced_assumptions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options_ini = root / "options.ini"
            options_ini.write_text(
                '[TableOptions/BlockBasedTable "default"]\n'
                "  filter_policy=rocksdb.EvolveDummyFilter\n",
                encoding="utf-8",
            )
            eval_config: dict = {
                "base_options_file": str(options_ini),
                "db_bench_flags": {
                    "use_direct_reads": "false",
                    "cache_index_and_filter_blocks": "true",
                },
                "options_overrides": {
                    "[DBOptions]": {"use_direct_reads": "false"},
                    '[TableOptions/BlockBasedTable "default"]': {
                        "cache_index_and_filter_blocks": "true",
                    },
                },
            }
            with mock.patch.object(
                evaluator,
                "_resolve_path",
                return_value=options_ini,
            ):
                evaluator._force_tiered_cache_eval(eval_config)

            self.assertEqual(eval_config["db_bench_flags"]["use_direct_reads"], "true")
            self.assertEqual(
                eval_config["db_bench_flags"]["cache_index_and_filter_blocks"], "false"
            )
            section = '[TableOptions/BlockBasedTable "default"]'
            self.assertEqual(
                eval_config["options_overrides"][section][
                    "cache_index_and_filter_blocks"
                ],
                "false",
            )
            self.assertEqual(
                eval_config["options_overrides"]["[DBOptions]"]["use_direct_reads"],
                "true",
            )

    def test_apply_initial_tiered_cache_split_preserves_total(self) -> None:
        cases = [
            (
                {"cache_size": "384M", "compressed_secondary_cache_size": "128M"},
                "268435456",
                "268435456",
            ),
            (
                {"cache_size": "512M"},
                "268435456",
                "268435456",
            ),
            (
                {},
                "268435456",
                "268435456",
            ),
        ]
        for db_flags, want_primary, want_secondary in cases:
            with self.subTest(db_flags=db_flags):
                flags = dict(db_flags)
                evaluator._apply_initial_tiered_cache_split(flags)
                self.assertEqual(flags["cache_size"], want_primary)
                self.assertEqual(
                    flags["compressed_secondary_cache_size"], want_secondary
                )

    def test_eval_db_bench_flags_parses_cache_size_suffixes(self) -> None:
        flags = evaluator._eval_db_bench_flags(
            {
                "db_bench_flags": {
                    "cache_size": "256M",
                    "compressed_secondary_cache_size": "256M",
                    "use_tiered_cache": "true",
                }
            }
        )
        self.assertIn("--cache_size=268435456", flags)
        self.assertIn("--compressed_secondary_cache_size=268435456", flags)
        self.assertIn("--use_tiered_cache=true", flags)


if __name__ == "__main__":
    unittest.main()
