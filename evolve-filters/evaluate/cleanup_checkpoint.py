"""Remove a checkpoint (or scratch) DB directory without touching protected roots."""

from __future__ import annotations

import shutil
from pathlib import Path


def _is_strict_ancestor(ancestor: Path, descendant: Path) -> bool:
    """True if ``descendant`` is strictly under ``ancestor`` (not equal)."""
    a = ancestor.resolve()
    d = descendant.resolve()
    if a == d:
        return False
    try:
        d.relative_to(a)
        return True
    except ValueError:
        return False


def remove_checkpoint_tree(
    path: Path,
    *,
    protected_roots: frozenset[Path],
    ignore_errors: bool = True,
) -> None:
    """
    Delete ``path`` recursively if it exists.

    ``ignore_errors`` defaults to ``True`` so ``finally`` blocks match
    ``shutil.rmtree(..., ignore_errors=True)`` used elsewhere for scratch DB dirs.

    Raises:
        ValueError: ``path`` is equal to, inside, or a strict ancestor of any path in
            ``protected_roots`` (would delete or mutate a golden DB).
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return

    for golden_raw in protected_roots:
        golden = golden_raw.expanduser().resolve()
        if resolved == golden:
            raise ValueError(
                f"Refusing to remove {resolved}: path equals protected checkpoint source"
            )
        try:
            resolved.relative_to(golden)
        except ValueError:
            pass
        else:
            raise ValueError(f"Refusing to remove {resolved}: under protected root {golden}")

        if _is_strict_ancestor(resolved, golden):
            raise ValueError(
                f"Refusing to remove {resolved}: ancestor of protected root {golden}"
            )

    shutil.rmtree(resolved, ignore_errors=ignore_errors)
