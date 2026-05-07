"""Patch RocksDB OPTIONS files for Dremel candidate runs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path


def write_options_file(
    baseline_path: Path,
    out_path: Path,
    overrides_by_section: dict[str, dict[str, str]],
) -> None:
    """Copy baseline options while replacing or appending selected keys."""

    text = baseline_path.read_text(encoding="utf-8")
    current_section: str | None = None
    out_lines: list[str] = []
    applied: dict[str, set[str]] = defaultdict(set)

    def flush_pending(section: str | None) -> None:
        if section is None:
            return
        section_overrides = overrides_by_section.get(section)
        if not section_overrides:
            return
        for key, value in section_overrides.items():
            if key not in applied[section]:
                out_lines.append(f"  {key}={value}\n")
                applied[section].add(key)

    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        stripped_line = line.strip()
        is_header = stripped_line.startswith("[") and stripped_line.endswith("]")
        if is_header:
            if current_section is not None and stripped_line != current_section:
                flush_pending(current_section)
            current_section = stripped_line
            out_lines.append(raw)
            continue

        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(raw)
            continue

        key = stripped.split("=", 1)[0].strip()
        section_overrides = overrides_by_section.get(current_section or "")
        if not section_overrides or key not in section_overrides:
            out_lines.append(raw)
            continue

        indent = line[: len(line) - len(stripped)]
        comment = ""
        value_part = stripped.split("=", 1)[1]
        if "#" in value_part:
            comment = " #" + value_part.split("#", 1)[1].rstrip()
        out_lines.append(f"{indent}{key}={section_overrides[key]}{comment}\n")
        applied[current_section or ""].add(key)

    flush_pending(current_section)
    for section, section_overrides in overrides_by_section.items():
        missing = [key for key in section_overrides if key not in applied[section]]
        if not missing:
            continue
        out_lines.append(f"\n{section}\n")
        for key in missing:
            out_lines.append(f"  {key}={section_overrides[key]}\n")

    out_path.write_text("".join(out_lines), encoding="utf-8")

