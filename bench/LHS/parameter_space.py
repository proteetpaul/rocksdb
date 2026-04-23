"""RocksDB option knobs for LHS: name, INI section, type, and bounds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

OptionKind = Literal["continuous", "integer", "discrete"]

# Section headers exactly as in rocksdb_options.ini
SECTION_DB = "[DBOptions]"
SECTION_CF = '[CFOptions "default"]'
SECTION_TABLE = '[TableOptions/BlockBasedTable "default"]'


@dataclass(frozen=True)
class OptionSpec:
    """One tunable option written into a RocksDB options INI file."""

    name: str
    section: str
    kind: OptionKind
    low: float | int | None = None
    high: float | int | None = None
    choices: tuple[str, ...] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "section": self.section,
            "kind": self.kind,
        }
        if self.low is not None:
            d["low"] = self.low
        if self.high is not None:
            d["high"] = self.high
        if self.choices is not None:
            d["choices"] = list(self.choices)
        return d

    @staticmethod
    def from_json_dict(d: dict[str, Any]) -> OptionSpec:
        ch = d.get("choices")
        choices_t: tuple[str, ...] | None = None
        if ch is not None:
            choices_t = tuple(str(x) for x in ch)
        return OptionSpec(
            name=str(d["name"]),
            section=str(d["section"]),
            kind=d["kind"],  # type: ignore[arg-type]
            low=d.get("low"),
            high=d.get("high"),
            choices=choices_t,
        )


def map_unit_to_value(spec: OptionSpec, u: float) -> str:
    """Map one LHS coordinate u in [0, 1) to an INI value string."""
    u = min(max(u, 0.0), 1.0 - 1e-15)
    if spec.kind == "discrete":
        assert spec.choices is not None
        n = len(spec.choices)
        idx = min(int(u * n), n - 1)
        return spec.choices[idx]
    if spec.kind == "integer":
        assert spec.low is not None and spec.high is not None
        lo = int(spec.low)
        hi = int(spec.high)
        span = hi - lo + 1
        return str(lo + min(int(u * span), span - 1))
    # continuous (stored as string for INI; use int when whole numbers)
    assert spec.low is not None and spec.high is not None
    v = float(spec.low) + u * (float(spec.high) - float(spec.low))
    if abs(v - round(v)) < 1e-9 * max(1.0, abs(v)):
        return str(int(round(v)))
    return repr(v)


def sample_vector_to_params(specs: Sequence[OptionSpec], u_row: Sequence[float]) -> dict[str, str]:
    """One LHS sample row (len == len(specs)) -> option name -> INI value."""
    if len(u_row) != len(specs):
        raise ValueError(f"Expected {len(specs)} coordinates, got {len(u_row)}")
    return {spec.name: map_unit_to_value(spec, u) for spec, u in zip(specs, u_row)}


# Default knobs aligned with bench/rocksdb_options.ini
SEARCH_SPACE: tuple[OptionSpec, ...] = (
    OptionSpec("max_background_jobs", SECTION_DB, "integer", 1, 8),         # Limits used in Dremel
    OptionSpec("write_buffer_size", SECTION_CF, "continuous", 32 << 20, 80 << 20),
    OptionSpec("max_write_buffer_number", SECTION_CF, "integer", 2, 8),     # Limits used in Dremel
    # OptionSpec("min_write_buffer_number_to_merge", SECTION_CF, "integer", 1, 2),        # Limits used in Dremel
    OptionSpec(
        "compaction_style",
        SECTION_CF,
        "discrete",
        choices=(
            "kCompactionStyleLevel",
            "kCompactionStyleUniversal",
        ),
    ),
    OptionSpec("block_size", SECTION_TABLE, "integer", 4096, 32768),
    OptionSpec("level0_file_num_compaction_trigger", SECTION_CF, "integer", 2, 12),
    # OptionSpec("level0_slowdown_writes_trigger", SECTION_CF, "integer", 8, 40),
    # OptionSpec("level0_stop_writes_trigger", SECTION_CF, "integer", 20, 80),
    OptionSpec("target_file_size_base", SECTION_CF, "continuous", 32 << 20, 256 << 20),
    # OptionSpec("target_file_size_multiplier", SECTION_CF, "integer", 1, 8),
    OptionSpec("max_bytes_for_level_base", SECTION_CF, "continuous", 200 << 20, 512 << 20),
    OptionSpec(
        "max_bytes_for_level_multiplier",
        SECTION_CF,
        "continuous",
        4.0,        # Limits used in Dremel
        16.0,
    ),
    OptionSpec(
        "filter_policy",
        SECTION_TABLE,
        "discrete",
        choices=(
            "rocksdb.BloomFilter:8:false",
            "rocksdb.BloomFilter:10:false",
            "rocksdb.BloomFilter:14:false",
            "rocksdb.BloomFilter:16:false",
            "rocksdb.BloomFilter:20:false",
        ),
    ),
)
