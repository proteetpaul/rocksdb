"""Unit tests for the standalone Dremel tuner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dremel.bandit import EvaluationResult, Arm, build_arms, select_ucb_arms, successive_halving
from dremel.db_bench import DbBenchContext, DbBenchRunResult, _report_flags
from dremel.options_file import write_options_file
from dremel.parameter_space import (
    BenchmarkShape,
    DremelConfig,
    MiB,
    background_job_limits,
    compute_fused_features,
    inverse_sstable_size_sum,
    option_overrides,
    passes_rules,
    sample_candidates,
)
from dremel.parse_db_bench import DbBenchMetrics, parse_db_bench_output
from dremel.tune import run_tuning


def valid_params() -> dict[str, object]:
    return {
        "max_background_jobs": 4,
        "level0_file_num_compaction_trigger": 4,
        "level0_slowdown_writes_trigger": 8,
        "level0_stop_writes_trigger": 16,
        "max_bytes_for_level_multiplier": 10,
        "max_bytes_for_level_base": 256 * MiB,
        "target_file_size_multiplier": 1,
        "target_file_size_base": 64 * MiB,
        "num_levels": 6,
        "max_write_buffer_number": 4,
        "write_buffer_size": 64 * MiB,
        "min_write_buffer_number_to_merge": 1,
        "filter_policy": "rocksdb.BloomFilter:10:false",
        "block_size": 4096,
        "cache_size": 512 * MiB,
    }


class TestDremelParameterSpace(unittest.TestCase):
    def test_rules_accept_and_reject_configs(self) -> None:
        params = valid_params()
        self.assertTrue(passes_rules(params, memory_budget_bytes=1024 * MiB))

        bad = dict(params)
        bad["level0_slowdown_writes_trigger"] = 4
        self.assertFalse(passes_rules(bad, memory_budget_bytes=1024 * MiB))

        bad = dict(params)
        bad["cache_size"] = 1024 * MiB
        self.assertFalse(passes_rules(bad, memory_budget_bytes=1024 * MiB))

    def test_feature_computation_and_overrides(self) -> None:
        params = valid_params()
        features = compute_fused_features(params, memory_budget_bytes=1024 * MiB)
        self.assertGreater(features.compaction_frequency, 0.0)
        self.assertGreater(features.write_buffer_full_frequency, 0.0)
        self.assertGreater(features.read_sstable_cost, 0.0)
        self.assertEqual(features.block_cache_hit_rate_proxy, 768 * MiB)

        overrides = option_overrides(params)
        self.assertEqual(overrides["[DBOptions]"]["max_background_jobs"], "4")
        self.assertEqual(overrides['[CFOptions "default"]']["write_buffer_size"], str(64 * MiB))
        self.assertNotIn("cache_size", str(overrides))

    def test_feature_computation_uses_benchmark_shape(self) -> None:
        params = valid_params()
        small = compute_fused_features(
            params,
            BenchmarkShape(num_keys=1_000, key_size_bytes=16, value_size_bytes=128),
            memory_budget_bytes=1024 * MiB,
        )
        large = compute_fused_features(
            params,
            BenchmarkShape(num_keys=100_000_000, key_size_bytes=16, value_size_bytes=1024),
            memory_budget_bytes=1024 * MiB,
        )
        self.assertLess(small.read_sstable_cost, large.read_sstable_cost)

    def test_background_job_limits_match_rocksdb_split(self) -> None:
        self.assertEqual(background_job_limits(1), (1.0, 1.0))
        self.assertEqual(background_job_limits(2), (1.0, 1.0))
        self.assertEqual(background_job_limits(4), (1.0, 3.0))
        self.assertEqual(background_job_limits(8), (2.0, 6.0))

    def test_inverse_sstable_size_sum_is_geometric_sum(self) -> None:
        # 1/F + 1/(2F) + 1/(4F) = 1.75/F.
        self.assertAlmostEqual(inverse_sstable_size_sum(100.0, 2.0, 3.0), 0.0175)
        self.assertAlmostEqual(inverse_sstable_size_sum(100.0, 1.0, 3.0), 0.03)

    def test_sample_candidates_are_feasible(self) -> None:
        candidates = sample_candidates(count=8, seed=1, memory_budget_bytes=1024 * MiB)
        self.assertGreaterEqual(len(candidates), 1)
        self.assertTrue(all(passes_rules(c.params, memory_budget_bytes=1024 * MiB) for c in candidates))


class TestDremelBandit(unittest.TestCase):
    def test_build_arms_and_ucb(self) -> None:
        configs = sample_candidates(count=10, seed=2, memory_budget_bytes=1024 * MiB)
        arms = build_arms(configs, buckets_per_feature=2)
        self.assertGreaterEqual(len(arms), 1)
        arms[0].rewards.extend([10.0, 12.0])
        chosen = select_ucb_arms(arms, top_k=1)
        self.assertEqual(len(chosen), 1)
        self.assertIs(chosen[0], arms[0])

    def test_successive_halving_keeps_higher_rewards(self) -> None:
        configs = sample_candidates(count=4, seed=3, memory_budget_bytes=1024 * MiB)
        seen: list[tuple[int, int]] = []

        def evaluate(config, duration):
            seen.append((config.index, duration))
            return EvaluationResult(config=config, reward=float(config.index), duration_sec=duration)

        results = successive_halving(configs, [1, 2], evaluate)
        self.assertGreaterEqual(len(results), len(configs))
        self.assertTrue(any(duration == 2 for _idx, duration in seen))


class TestDremelIO(unittest.TestCase):
    def test_options_file_patch_appends_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.ini"
            out = Path(tmp) / "out.ini"
            base.write_text("[DBOptions]\n  max_background_jobs=2 # keep\n\n", encoding="utf-8")
            write_options_file(
                base,
                out,
                {
                    "[DBOptions]": {"max_background_jobs": "4"},
                    '[CFOptions "default"]': {"write_buffer_size": "1024"},
                },
            )
            text = out.read_text(encoding="utf-8")
            self.assertIn("max_background_jobs=4 # keep", text)
            self.assertIn('[CFOptions "default"]', text)
            self.assertIn("write_buffer_size=1024", text)

    def test_parse_db_bench_output(self) -> None:
        text = """
readrandomwriterandom : 10.000 micros/op 50000 ops/sec 1.000 seconds 1000000 operations
Microseconds per read:
Percentiles: P50: 4.50 P75: 6.00 P99: 18.00
Microseconds per write:
Percentiles: P50: 7.00 P75: 10.00 P99: 25.00
"""
        metrics = parse_db_bench_output(text)
        self.assertEqual(metrics.throughput_qps, 50000.0)
        self.assertEqual(metrics.read_p99_us, 18.0)
        self.assertEqual(metrics.write_p50_us, 7.0)

    def test_report_flags_use_report_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = DbBenchContext(
                db_bench=Path("/bin/true"),
                bench_ini=root / "bench.ini",
                base_options=root / "rocksdb_options.ini",
                workload_dir=root,
                out_dir=root / "out",
                report_interval_seconds="1",
                report_dir=str(root / "reports"),
            )
            flags = _report_flags(context, DremelConfig(index=7, params={}), 60)
            self.assertEqual(flags[0], "--report_interval_seconds=1")
            self.assertIn("dremel_config_7_duration_60.csv", flags[1])
            self.assertTrue((root / "reports").is_dir())

    def test_run_tuning_with_fake_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = DbBenchContext(
                db_bench=Path("/bin/true"),
                bench_ini=root / "bench.ini",
                base_options=root / "rocksdb_options.ini",
                workload_dir=root,
                out_dir=root / "out",
            )

            def fake_eval(_context, config, *, duration_sec):
                reward = float(config.index * 100 + duration_sec)
                return DbBenchRunResult(
                    config=config,
                    duration_sec=duration_sec,
                    metrics=DbBenchMetrics(reward, None, None, None, None),
                    status="ok",
                    log_path=root / f"{config.index}.log",
                )

            payload = run_tuning(
                context=context,
                candidate_count=8,
                buckets_per_feature=2,
                memory_budget_bytes=1024 * MiB,
                seed=4,
                time_points_sec=[1, 2],
                ucb_rounds=1,
                ucb_top_k=2,
                benchmark_shape=BenchmarkShape(num_keys=1000, key_size_bytes=16, value_size_bytes=128),
                evaluate_db_bench=fake_eval,
            )
            self.assertIn("best", payload)
            self.assertEqual(payload["summary"]["benchmark_shape"]["num"], 1000)
            self.assertTrue((context.out_dir / "results.csv").is_file())
            self.assertTrue((context.out_dir / "recommendation.json").is_file())


if __name__ == "__main__":
    unittest.main()

