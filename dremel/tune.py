#!/usr/bin/env python3
"""Command-line Dremel tuning controller for RocksDB db_bench."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from typing import Any, Callable

from .bandit import (
    Arm,
    EvaluationResult,
    build_arms,
    heuristic_config,
    sample_untried_config,
    select_ucb_arms,
    successive_halving,
)
from .db_bench import (
    DbBenchContext,
    DbBenchRunResult,
    find_db_bench,
    load_simple_ini,
    repo_root,
    result_row,
    run_db_bench_evaluation,
)
from .parameter_space import BenchmarkShape, DremelConfig, compute_fused_features, sample_candidates
from .parse_db_bench import DbBenchMetrics


def run_tuning(
    *,
    context: DbBenchContext,
    candidate_count: int,
    buckets_per_feature: int,
    memory_budget_bytes: int,
    seed: int,
    time_points_sec: list[int],
    ucb_rounds: int,
    ucb_top_k: int,
    resume: bool = False,
    benchmark_shape: BenchmarkShape | None = None,
    evaluate_db_bench: Callable[..., DbBenchRunResult] = run_db_bench_evaluation,
) -> dict[str, Any]:
    """Run Dremel and return the recommendation payload."""

    shape = benchmark_shape or _benchmark_shape_from_ini(context.bench_ini)
    rng = random.Random(seed)
    candidates = sample_candidates(
        memory_budget_bytes=memory_budget_bytes,
        count=candidate_count,
        seed=seed,
    )
    arms = build_arms(
        candidates,
        buckets_per_feature=buckets_per_feature,
        benchmark_shape=shape,
        memory_budget_bytes=memory_budget_bytes,
    )
    arm_by_config_index = {config.index: arm for arm in arms for config in arm.configs}
    config_by_index = {config.index: config for config in candidates}
    tried_indices: set[int] = set()
    db_results: list[DbBenchRunResult] = []
    prior_by_key: dict[tuple[int, int], DbBenchRunResult] = {}

    if resume:
        db_results, prior_by_key = _load_prior_results(context.out_dir / "results.csv", config_by_index)
        for result in db_results:
            if result.status == "ok":
                arm_by_config_index[result.config.index].rewards.append(result.reward)
                tried_indices.add(result.config.index)
    else:
        _clear_previous_outputs(context.out_dir)

    def evaluate(config: DremelConfig, duration: int) -> EvaluationResult:
        cached = prior_by_key.get((config.index, duration))
        if cached is not None:
            return EvaluationResult(
                config=config,
                reward=cached.reward,
                duration_sec=duration,
                status=cached.status,
            )
        db_result = evaluate_db_bench(context, config, duration_sec=duration)
        db_results.append(db_result)
        _append_result_csv(context.out_dir / "results.csv", db_result)
        if db_result.status == "ok":
            arm_by_config_index[config.index].rewards.append(db_result.reward)
            tried_indices.add(config.index)
        _write_state(context.out_dir, arms, db_results)
        return EvaluationResult(
            config=config,
            reward=db_result.reward,
            duration_sec=duration,
            status=db_result.status,
        )

    initial_configs = [
        heuristic_config(
            arm,
            benchmark_shape=shape,
            memory_budget_bytes=memory_budget_bytes,
        )
        for arm in arms
    ]
    successive_halving(initial_configs, time_points_sec, evaluate)

    for _round in range(ucb_rounds):
        sampled = []
        for arm in select_ucb_arms(arms, top_k=ucb_top_k):
            config = sample_untried_config(arm, tried_indices, rng)
            if config is not None:
                sampled.append(config)
        if not sampled:
            break
        successive_halving(sampled, time_points_sec, evaluate)

    successful = [result for result in db_results if result.status == "ok"]
    if not successful:
        raise RuntimeError("Dremel did not complete any successful db_bench evaluations")
    best = max(successful, key=lambda result: result.reward)
    payload = _recommendation_payload(
        best, candidates, arms, db_results, shape, memory_budget_bytes
    )
    context.out_dir.mkdir(parents=True, exist_ok=True)
    (context.out_dir / "recommendation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    repo = repo_root()
    bench_default = repo / "bench"

    parser = argparse.ArgumentParser(description="Dremel adaptive RocksDB db_bench tuner.")
    parser.add_argument("--base-options", type=Path, default=bench_default / "rocksdb_options.ini")
    parser.add_argument("--bench-ini", type=Path, default=bench_default / "bench.ini")
    parser.add_argument("--workload-dir", type=Path, default=bench_default / "workloads")
    parser.add_argument("--out", type=Path, default=repo / "dremel" / "out" / "run")
    parser.add_argument("--db-bench", type=Path, default=None)
    parser.add_argument("--objective", choices=("max_iops",), default="max_iops")
    parser.add_argument("--candidates", type=int, default=512)
    parser.add_argument("--buckets", type=int, default=2)
    parser.add_argument("--memory-budget", type=str, default="1G")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--time-points", type=str, default="60,80,100")
    parser.add_argument("--ucb-rounds", type=int, default=3)
    parser.add_argument("--ucb-top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--systemd-scope", action="store_true")
    parser.add_argument("--memory-limit", type=str, default=None)
    parser.add_argument("--resume", action="store_true", help="Reuse prior results.csv in --out")
    parser.add_argument("--num", type=float, default=None, help="Override benchmark num for Dremel feature estimates")
    parser.add_argument("--key-size", type=float, default=None, help="Override benchmark key_size for Dremel feature estimates")
    parser.add_argument("--value-size", type=float, default=None, help="Override benchmark value_size for Dremel feature estimates")
    parser.add_argument("--threads", type=int, default=None, help="Override db_bench workload threads")
    args = parser.parse_args()

    time_points = _parse_time_points(args.time_points)
    bench_vars = load_simple_ini(args.bench_ini.resolve())
    benchmark_shape = _benchmark_shape_from_args(args, args.bench_ini.resolve())
    context = DbBenchContext(
        db_bench=find_db_bench(repo, args.db_bench.resolve() if args.db_bench else None),
        bench_ini=args.bench_ini.resolve(),
        base_options=args.base_options.resolve(),
        workload_dir=args.workload_dir.resolve(),
        out_dir=args.out.resolve(),
        num=_db_bench_number(benchmark_shape.num_keys),
        key_size=_db_bench_number(benchmark_shape.key_size_bytes),
        value_size=_db_bench_number(benchmark_shape.value_size_bytes),
        threads=str(args.threads if args.threads is not None else bench_vars.get("threads", "16")),
        compression_type=bench_vars.get("compression_type", "none"),
        histogram=bench_vars.get("histogram", "true"),
        statistics=bench_vars.get("statistics", "false"),
        report_interval_seconds=bench_vars.get("report_interval_seconds", "0"),
        report_dir=bench_vars.get("report_dir", "bench/reports"),
        memory_limit=args.memory_limit,
        use_systemd_scope=args.systemd_scope,
        timeout_sec=args.timeout,
    )
    payload = run_tuning(
        context=context,
        candidate_count=args.candidates,
        buckets_per_feature=args.buckets,
        memory_budget_bytes=_parse_bytes(args.memory_budget),
        seed=args.seed,
        time_points_sec=time_points,
        ucb_rounds=args.ucb_rounds,
        ucb_top_k=args.ucb_top_k,
        resume=args.resume,
        benchmark_shape=benchmark_shape,
    )
    print(json.dumps(payload["best"], indent=2))
    return 0


def _append_result_csv(path: Path, result: DbBenchRunResult) -> None:
    row = result_row(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row.keys()), extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _write_state(out_dir: Path, arms: list[Arm], results: list[DbBenchRunResult]) -> None:
    payload = {
        "evaluations": len(results),
        "arms": [
            {
                "key": list(arm.key),
                "config_indices": [config.index for config in arm.configs],
                "rewards": arm.rewards,
            }
            for arm in arms
        ],
        "results": [result_row(result) for result in results],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "state.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_prior_results(
    path: Path,
    config_by_index: dict[int, DremelConfig],
) -> tuple[list[DbBenchRunResult], dict[tuple[int, int], DbBenchRunResult]]:
    if not path.is_file():
        return [], {}
    results: list[DbBenchRunResult] = []
    by_key: dict[tuple[int, int], DbBenchRunResult] = {}
    with path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            try:
                config_index = int(row["config_index"])
                duration_sec = int(row["duration_sec"])
            except (KeyError, TypeError, ValueError):
                continue
            config = config_by_index.get(config_index)
            if config is None:
                continue
            result = DbBenchRunResult(
                config=config,
                duration_sec=duration_sec,
                metrics=DbBenchMetrics(
                    throughput_qps=_optional_float(row.get("throughput_qps")),
                    read_p50_us=_optional_float(row.get("read_p50_us")),
                    read_p99_us=_optional_float(row.get("read_p99_us")),
                    write_p50_us=_optional_float(row.get("write_p50_us")),
                    write_p99_us=_optional_float(row.get("write_p99_us")),
                ),
                status=row.get("status") or "unknown",
                log_path=Path(row.get("log_path") or ""),
            )
            results.append(result)
            by_key[(config_index, duration_sec)] = result
    return results, by_key


def _clear_previous_outputs(out_dir: Path) -> None:
    for filename in ("results.csv", "state.json", "recommendation.json"):
        path = out_dir / filename
        if path.is_file():
            path.unlink()


def _recommendation_payload(
    best: DbBenchRunResult,
    candidates: list[DremelConfig],
    arms: list[Arm],
    results: list[DbBenchRunResult],
    benchmark_shape: BenchmarkShape,
    memory_budget_bytes: int,
) -> dict[str, Any]:
    features = compute_fused_features(best.config.params, benchmark_shape, memory_budget_bytes)
    return {
        "best": {
            "config_index": best.config.index,
            "reward_iops": best.reward,
            "duration_sec": best.duration_sec,
            "params": best.config.params,
            "fused_features": {
                "compaction_frequency": features.compaction_frequency,
                "write_buffer_full_frequency": features.write_buffer_full_frequency,
                "read_sstable_cost": features.read_sstable_cost,
                "block_cache_hit_rate_proxy": features.block_cache_hit_rate_proxy,
            },
            "log_path": str(best.log_path),
        },
        "summary": {
            "candidate_count": len(candidates),
            "arm_count": len(arms),
            "evaluation_count": len(results),
            "successful_evaluation_count": sum(1 for result in results if result.status == "ok"),
            "benchmark_shape": {
                "num": benchmark_shape.num_keys,
                "key_size": benchmark_shape.key_size_bytes,
                "value_size": benchmark_shape.value_size_bytes,
            },
            "memory_budget_bytes": memory_budget_bytes,
        },
    }


def _benchmark_shape_from_args(args: argparse.Namespace, bench_ini: Path) -> BenchmarkShape:
    from_ini = _benchmark_shape_from_ini(bench_ini)
    return BenchmarkShape(
        num_keys=from_ini.num_keys if args.num is None else args.num,
        key_size_bytes=from_ini.key_size_bytes if args.key_size is None else args.key_size,
        value_size_bytes=from_ini.value_size_bytes if args.value_size is None else args.value_size,
    )


def _benchmark_shape_from_ini(bench_ini: Path) -> BenchmarkShape:
    if not bench_ini.is_file():
        return BenchmarkShape()
    values = load_simple_ini(bench_ini)
    return BenchmarkShape(
        num_keys=_optional_float(values.get("num")) or BenchmarkShape.num_keys,
        key_size_bytes=_optional_float(values.get("key_size")) or BenchmarkShape.key_size_bytes,
        value_size_bytes=_optional_float(values.get("value_size")) or BenchmarkShape.value_size_bytes,
    )


def _db_bench_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def _parse_time_points(raw: str) -> list[int]:
    out = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not out or any(value <= 0 for value in out):
        raise ValueError("--time-points must contain positive seconds")
    return out


def _parse_bytes(raw: str) -> int:
    text = raw.strip().upper()
    if text.isdigit():
        return int(text)
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if len(text) >= 2 and text[:-1].isdigit() and text[-1] in multipliers:
        return int(text[:-1]) * multipliers[text[-1]]
    raise ValueError(f"invalid byte size: {raw}")


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())

