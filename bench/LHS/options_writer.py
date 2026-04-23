"""Patch a baseline RocksDB options INI with per-sample overrides."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Sequence

from .parameter_space import OptionSpec


def _normalize_header(line: str) -> str | None:
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        return s
    return None


def write_options_file(
    baseline_path: Path,
    out_path: Path,
    overrides_by_section: dict[str, dict[str, str]],
) -> None:
    """
    Copy baseline_path to out_path, replacing key=value lines when
    (section, key) appears in overrides_by_section. Section headers must
    match exactly (e.g. '[DBOptions]').
    Keys present in overrides but missing from the baseline section are
    appended (with two-space indent) just before the next section header.
    """
    text = baseline_path.read_text(encoding="utf-8")
    current_section: str | None = None
    out_lines: list[str] = []
    applied_in_section: dict[str, set[str]] = defaultdict(set)

    def flush_pending_appends(section: str | None) -> None:
        if section is None:
            return
        sec_map = overrides_by_section.get(section)
        if not sec_map:
            return
        done = applied_in_section[section]
        for key, val in sec_map.items():
            if key not in done:
                out_lines.append(f"  {key}={val}\n")
                done.add(key)

    for raw in text.splitlines(keepends=True):
        line_no_nl = raw.rstrip("\r\n")
        hdr = _normalize_header(line_no_nl)
        if hdr is not None:
            if current_section is not None and hdr != current_section:
                flush_pending_appends(current_section)
            current_section = hdr
            out_lines.append(raw)
            continue

        stripped = line_no_nl.lstrip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw)
            continue

        key_part = stripped.split("=", 1)[0].strip()
        if current_section is None or "=" not in stripped or not key_part:
            out_lines.append(raw)
            continue

        sec_map = overrides_by_section.get(current_section)
        if sec_map is None or key_part not in sec_map:
            out_lines.append(raw)
            continue

        indent = line_no_nl[: len(line_no_nl) - len(stripped)]
        new_val = sec_map[key_part]
        comment = ""
        if "#" in stripped:
            idx = stripped.index("#")
            comment = " " + stripped[idx:].rstrip()
        new_line = f"{indent}{key_part}={new_val}{comment}\n"
        out_lines.append(new_line)
        applied_in_section[current_section].add(key_part)

    flush_pending_appends(current_section)

    for section, sec_map in overrides_by_section.items():
        if section == current_section:
            continue
        if not sec_map:
            continue
        done = applied_in_section[section]
        missing = [k for k in sec_map if k not in done]
        if not missing:
            continue
        out_lines.append(f"\n{section}\n")
        for k in missing:
            out_lines.append(f"  {k}={sec_map[k]}\n")
            done.add(k)

    out_path.write_text("".join(out_lines), encoding="utf-8")


def params_to_overrides(
    params: dict[str, str],
    specs: Sequence[OptionSpec],
) -> dict[str, dict[str, str]]:
    """Build nested override map from flat param name -> value."""
    by_section: dict[str, dict[str, str]] = {}
    name_to_spec = {s.name: s for s in specs}
    for name, val in params.items():
        spec = name_to_spec.get(name)
        if spec is None:
            continue
        by_section.setdefault(spec.section, {})[name] = val
    return by_section
