"""Tests for checkpoint_db and cleanup_checkpoint libraries."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluate.checkpoint_db import CheckpointError, create_checkpoint
from evaluate.cleanup_checkpoint import remove_checkpoint_tree


def _dir_fingerprint(root: Path) -> dict[str, bytes]:
    fp: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            fp[str(path.relative_to(root))] = path.read_bytes()
    return fp


class TestCreateCheckpointValidation(unittest.TestCase):
    def test_rejects_same_source_and_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "db"
            src.mkdir()
            ldb = root / "ldb"
            ldb.write_text("#!/bin/sh\necho mock\n", encoding="utf-8")
            ldb.chmod(0o755)
            with self.assertRaisesRegex(CheckpointError, "must differ"):
                create_checkpoint(ldb, src, src)

    def test_rejects_dest_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "db"
            src.mkdir()
            dest = src / "snap"
            ldb = root / "ldb"
            ldb.write_text("#!/bin/sh\necho mock\n", encoding="utf-8")
            ldb.chmod(0o755)
            with self.assertRaisesRegex(CheckpointError, "must not be inside"):
                create_checkpoint(ldb, src, dest)

    def test_rejects_nonexecutable_ldb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "db"
            src.mkdir()
            dest = root / "cp"
            ldb = root / "ldb"
            ldb.write_text("not executable", encoding="utf-8")
            ldb.chmod(0o644)
            with self.assertRaisesRegex(CheckpointError, "not an executable"):
                create_checkpoint(ldb, src, dest)

    def test_rejects_nonexistent_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "missing"
            dest = root / "cp"
            ldb = root / "ldb"
            ldb.write_text("#!/bin/sh\n", encoding="utf-8")
            ldb.chmod(0o755)
            with self.assertRaisesRegex(CheckpointError, "not a directory"):
                create_checkpoint(ldb, src, dest)


class TestCreateCheckpointSubprocess(unittest.TestCase):
    def test_golden_files_unchanged_after_mocked_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "golden"
            golden.mkdir()
            (golden / "stub").write_bytes(b"marker")
            dest = root / "work"
            ldb = root / "ldb"
            ldb.write_text("#!/bin/sh\n", encoding="utf-8")
            ldb.chmod(0o755)

            before = _dir_fingerprint(golden)
            with mock.patch(
                "evaluate.checkpoint_db.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["ldb"], 0, stdout="OK\n", stderr=""
                ),
            ):
                create_checkpoint(ldb, golden, dest)
            after = _dir_fingerprint(golden)
            self.assertEqual(before, after)
            self.assertFalse(dest.exists())


class TestRemoveCheckpointTree(unittest.TestCase):
    def test_removes_work_dir_leaves_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "golden"
            golden.mkdir()
            (golden / "data").write_text("keep", encoding="utf-8")
            work = root / "work"
            work.mkdir()
            (work / "x").write_text("drop", encoding="utf-8")

            before = (golden / "data").read_bytes()
            remove_checkpoint_tree(
                work,
                protected_roots=frozenset({golden}),
                ignore_errors=False,
            )
            self.assertFalse(work.exists())
            self.assertEqual((golden / "data").read_bytes(), before)

    def test_rejects_equal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "g"
            golden.mkdir()
            with self.assertRaisesRegex(ValueError, "equals protected"):
                remove_checkpoint_tree(
                    golden,
                    protected_roots=frozenset({golden}),
                )

    def test_rejects_strict_ancestor_of_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "g"
            golden.mkdir()
            with self.assertRaisesRegex(ValueError, "ancestor of protected"):
                remove_checkpoint_tree(root, protected_roots=frozenset({golden}))

    def test_rejects_path_under_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "g"
            golden.mkdir()
            sub = golden / "scratch"
            sub.mkdir()
            with self.assertRaisesRegex(ValueError, "under protected root"):
                remove_checkpoint_tree(sub, protected_roots=frozenset({golden}))

    def test_raises_on_ldb_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "golden"
            golden.mkdir()
            dest = root / "dest"
            ldb = root / "ldb"
            ldb.write_text("#!/bin/sh\n", encoding="utf-8")
            ldb.chmod(0o755)

            with mock.patch(
                "evaluate.checkpoint_db.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["ldb"],
                    7,
                    stdout="",
                    stderr="boom",
                ),
            ):
                with self.assertRaises(CheckpointError) as ctx:
                    create_checkpoint(ldb, golden, dest)
            self.assertEqual(ctx.exception.returncode, 7)
            self.assertEqual(ctx.exception.stderr, "boom")


@unittest.skipUnless(
    os.environ.get("EVOLVE_FILTERS_LDB", "").strip()
    and os.environ.get("EVOLVE_FILTERS_DB_BENCH", "").strip()
    and Path(os.environ["EVOLVE_FILTERS_LDB"].strip()).is_file()
    and Path(os.environ["EVOLVE_FILTERS_DB_BENCH"].strip()).is_file(),
    "Set EVOLVE_FILTERS_LDB and EVOLVE_FILTERS_DB_BENCH for integration test",
)
class TestCheckpointIntegration(unittest.TestCase):
    def test_golden_stable_after_checkpoint_and_read(self) -> None:
        ldb = Path(os.environ["EVOLVE_FILTERS_LDB"].strip())
        bench = Path(os.environ["EVOLVE_FILTERS_DB_BENCH"].strip())
        timeout = float(os.environ.get("EVOLVE_CHECKPOINT_TEST_TIMEOUT_SEC", "60"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / "golden_db"
            work = root / "work_db"
            golden.mkdir()
            fill_argv = [
                str(bench),
                f"--db={golden}",
                "--benchmarks=fillseq",
                "--num=200",
                "--key_size=8",
                "--value_size=32",
                "--threads=1",
                "--disable_wal=true",
                "--statistics=false",
                "--histogram=false",
            ]
            subprocess.run(fill_argv, check=True, capture_output=True, timeout=timeout)
            fp_before = _dir_fingerprint(golden)

            create_checkpoint(ldb, golden, work)

            read_argv = [
                str(bench),
                f"--db={work}",
                "--benchmarks=readseq",
                "--duration=1",
                "--threads=1",
                "--statistics=false",
                "--histogram=false",
            ]
            subprocess.run(read_argv, check=True, capture_output=True, timeout=timeout)

            remove_checkpoint_tree(
                work, protected_roots=frozenset({golden}), ignore_errors=False
            )

            fp_after = _dir_fingerprint(golden)
            self.assertEqual(fp_before, fp_after)


if __name__ == "__main__":
    unittest.main()
