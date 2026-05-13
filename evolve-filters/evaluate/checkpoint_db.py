"""Create a RocksDB checkpoint by invoking ``ldb checkpoint``."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class CheckpointError(Exception):
    """Raised when path validation fails or ``ldb`` exits non-zero."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _is_inside_or_equal(path: Path, root: Path) -> bool:
    """True if resolved ``path`` is ``root`` or a descendant of ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def create_checkpoint(ldb: Path, source_db: Path, checkpoint_dir: Path) -> None:
    """
    Run ``ldb checkpoint`` so RocksDB creates ``checkpoint_dir`` (hard-linking SSTs).

    ``checkpoint_dir`` must not exist when ``ldb`` runs; any existing ``checkpoint_dir``
    is removed first. Parent directories are created as needed.

    Raises:
        CheckpointError: invalid paths or ``ldb`` failure.
    """
    ldb_p = ldb.expanduser().resolve()
    source_p = source_db.expanduser().resolve()
    dest_p = checkpoint_dir.expanduser().resolve()

    if not ldb_p.is_file() or not os.access(ldb_p, os.X_OK):
        raise CheckpointError(f"ldb is not an executable file: {ldb_p}")
    if not source_p.is_dir():
        raise CheckpointError(f"source_db is not a directory: {source_p}")
    if source_p == dest_p:
        raise CheckpointError("checkpoint_dir must differ from source_db")
    if _is_inside_or_equal(dest_p, source_p):
        raise CheckpointError(
            f"checkpoint_dir {dest_p} must not be inside source_db {source_p}"
        )

    dest_parent = dest_p.parent
    dest_parent.mkdir(parents=True, exist_ok=True)

    if dest_p.exists():
        shutil.rmtree(dest_p, ignore_errors=False)

    argv = [
        str(ldb_p),
        f"--db={source_p}",
        "checkpoint",
        f"--checkpoint_dir={dest_p}",
    ]
    proc = subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise CheckpointError(
            f"ldb checkpoint failed with exit code {proc.returncode}",
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
