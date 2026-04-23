"""Latin Hypercube unit sampling and JSON persistence for LHS configs."""

from __future__ import annotations

import json
import random
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .parameter_space import OptionSpec, sample_vector_to_params


def latin_hypercube_unit(n: int, d: int, rng: random.Random) -> list[list[float]]:
    """
    Classic LHS on [0, 1)^d: one sample per stratum per dimension, then
    random permutation per dimension (McKay et al.).
    """
    if n <= 0 or d <= 0:
        raise ValueError("n and d must be positive")
    cols: list[list[float]] = []
    for _j in range(d):
        perm = list(range(n))
        rng.shuffle(perm)
        col = [(perm[i] + rng.random()) / n for i in range(n)]
        cols.append(col)
    # transpose: rows are samples
    return [[cols[j][i] for j in range(d)] for i in range(n)]


def draw_samples(
    specs: Sequence[OptionSpec],
    n: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    d = len(specs)
    unit_rows = latin_hypercube_unit(n, d, rng)
    samples: list[dict[str, Any]] = []
    for i, row in enumerate(unit_rows):
        params = sample_vector_to_params(specs, row)
        samples.append({"index": i, "params": params})
    return samples


def specs_to_json_list(specs: Sequence[OptionSpec]) -> list[dict[str, Any]]:
    return [s.to_json_dict() for s in specs]


def specs_from_json_list(rows: list[dict[str, Any]]) -> list[OptionSpec]:
    return [OptionSpec.from_json_dict(r) for r in rows]


def write_samples_file(
    path: Path,
    specs: Sequence[OptionSpec],
    samples: list[dict[str, Any]],
    seed: int,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "seed": seed,
        "n_samples": len(samples),
        "parameter_definitions": specs_to_json_list(specs),
        "samples": samples,
    }
    if extra_meta:
        payload["meta"] = extra_meta
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_samples_file(path: Path) -> tuple[list[OptionSpec], list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    specs = specs_from_json_list(data["parameter_definitions"])
    samples = data["samples"]
    meta = {k: v for k, v in data.items() if k not in ("parameter_definitions", "samples")}
    return specs, samples, meta
