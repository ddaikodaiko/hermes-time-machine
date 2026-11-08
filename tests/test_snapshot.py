"""Tests for the content-addressed snapshot engine."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
    existing = sys.modules.get("hermes_time_machine")
    if existing is not None and hasattr(existing, "snapshot"):
        return existing
    spec = importlib.util.spec_from_file_location(
        "hermes_time_machine",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_time_machine"] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()
snapshot = pkg.snapshot
EXCL = snapshot.excludes_for({})


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot_files(ws: Path) -> dict:
    return {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in ws.rglob("*")
        if p.is_file()
    }


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.ws = root / "workspace"
        self.ws.mkdir()
        os.environ["ATM_DATA_DIR"] = str(root / "data")
        write(self.ws / "src" / "a.py", "a1")
        write(self.ws / "src" / "b.py", "b1")
        write(self.ws / "keep.txt", "keep")

    def tearDown(self):
        os.environ.pop("ATM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_capture_diff_restore(self):
        tree = snapshot.capture(self.ws, EXCL)
        self.assertEqual(tree["files_count"], 3)
        write(self.ws / "src" / "a.py", "a2 changed")
        (self.ws / "keep.txt").unlink()
        write(self.ws / "new.txt", "new")
        (self.ws / "emptydir").mkdir()
        d = snapshot.diff(self.ws, tree, EXCL)
        self.assertEqual(d["modified"], ["src/a.py"])
        self.assertEqual(d["deleted"], ["keep.txt"])
        self.assertEqual(d["added"], ["new.txt"])
        self.assertEqual(d["unchanged"], 1)

        res = snapshot.restore(self.ws, tree, EXCL)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["restored"], 2)
        self.assertEqual(res["removed"], 1)
        self.assertEqual(
            snapshot_files(self.ws),
            {"src/a.py": b"a1", "src/b.py": b"b1", "keep.txt": b"keep"},
        )
        self.assertFalse((self.ws / "emptydir").exists())
        clean = snapshot.diff(self.ws, tree, EXCL)
        self.assertEqual((clean["modified"], clean["deleted"], clean["added"]), ([], [], []))
        trash = Path(res["previous_contents"])
        self.assertEqual((trash / "src" / "a.py").read_text(encoding="utf-8"), "a2 changed")
        self.assertEqual((trash / "new.txt").read_text(encoding="utf-8"), "new")

    def test_empty_dirs_recreated(self):
        (self.ws / "empty").mkdir()
        tree = snapshot.capture(self.ws, EXCL)
        self.assertIn("empty", tree["dirs"])
        (self.ws / "empty").rmdir()
        snapshot.restore(self.ws, tree, EXCL)
        self.assertTrue((self.ws / "empty").is_dir())

    def test_incremental_reuse(self):
        first = snapshot.capture(self.ws, EXCL)
        second = snapshot.capture(self.ws, EXCL, previous_tree=first)
        self.assertEqual(second["reused"], second["files_count"])
        self.assertEqual(second["new_objects"], 0)
        self.assertEqual(second["files"], first["files"])

    def test_dedup(self):
        write(self.ws / "x1.txt", "same content")
        write(self.ws / "x2.txt", "same content")
        tree = snapshot.capture(self.ws, EXCL)
        self.assertEqual(tree["files"]["x1.txt"]["h"], tree["files"]["x2.txt"]["h"])
        objs = [p for p in snapshot.objects_dir().rglob("*") if p.is_file() and p.parent.name != "tmp"]
        self.assertEqual(len(objs), 4)  # a1, b1, keep, same content

    def test_excludes(self):
        write(self.ws / "node_modules" / "x.js", "x")
        write(self.ws / "pkg" / "__pycache__" / "a.pyc", "x")
        tree = snapshot.capture(self.ws, EXCL)
        self.assertNotIn("node_modules/x.js", tree["files"])
        self.assertNotIn("pkg/__pycache__/a.pyc", tree["files"])
        self.assertNotIn("node_modules", tree["dirs"])
        d = snapshot.diff(self.ws, tree, EXCL)
        self.assertEqual(d["added"], [])
        snapshot.restore(self.ws, tree, EXCL)
        self.assertTrue((self.ws / "node_modules" / "x.js").exists())

    def test_max_file_bytes(self):
        write(self.ws / "big.bin", "x" * 5000)
        tree = snapshot.capture(self.ws, EXCL, max_file_bytes=1000)
        self.assertNotIn("big.bin", tree["files"])
        self.assertEqual(tree["skipped"][0]["reason"], "too_large")
        self.assertEqual(tree["skipped"][0]["path"], "big.bin")
        d = snapshot.diff(self.ws, tree, EXCL)
        self.assertEqual(d["added"], [])
        res = snapshot.restore(self.ws, tree, EXCL)
        self.assertEqual(res["removed"], 0)
        self.assertTrue((self.ws / "big.bin").exists())

    def test_deadline(self):
        with self.assertRaises(snapshot.SnapshotTimeout):
            snapshot.capture(self.ws, EXCL, deadline=time.monotonic() - 1)
        self.assertTrue(issubclass(snapshot.SnapshotTimeout, snapshot.SnapshotError))

    def test_restore_paths_only(self):
        tree = snapshot.capture(self.ws, EXCL)
        write(self.ws / "src" / "a.py", "a changed")
        write(self.ws / "src" / "b.py", "b changed")
        res = snapshot.restore(self.ws, tree, EXCL, paths=["src\\a.py"])
        self.assertEqual(res["restored"], 1)
        self.assertEqual((self.ws / "src" / "a.py").read_text(encoding="utf-8"), "a1")
        self.assertEqual((self.ws / "src" / "b.py").read_text(encoding="utf-8"), "b changed")
        d = snapshot.diff(self.ws, tree, EXCL, paths=["./src"])
        self.assertEqual(d["modified"], ["src/b.py"])
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.diff(self.ws, tree, EXCL, paths=["../x"])
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.diff(self.ws, tree, EXCL, paths=["/abs"])

    def test_missing_object(self):
        tree = snapshot.capture(self.ws, EXCL)
        write(self.ws / "src" / "a.py", "changed")
        write(self.ws / "extra.txt", "extra")
        snapshot.object_path(tree["files"]["src/a.py"]["h"]).unlink()
        before = snapshot_files(self.ws)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.restore(self.ws, tree, EXCL)
        self.assertEqual(snapshot_files(self.ws), before)

    def test_malicious_entries(self):
        tree = snapshot.capture(self.ws, EXCL)
        h = tree["files"]["keep.txt"]["h"]
        evil = {"h": h, "s": 4, "m": 1, "x": False}
        for bad in ("../evil.txt", "C:/x", "/etc/passwd", "a/../../b", "..\\evil2.txt"):
            tree["files"][bad] = dict(evil)
        tree["dirs"].append("../evil_dir")
        res = snapshot.restore(self.ws, tree, EXCL)
        self.assertGreaterEqual(len(res["errors"]), 5)
        for name in ("evil.txt", "evil2.txt", "b", "evil_dir", "x"):
            self.assertFalse((self.ws.parent / name).exists())
        self.assertNotIn("C:", os.listdir(self.ws))
        dest = self.root / "fork"
        info = snapshot.materialize(tree, dest)
        self.assertEqual(info["files"], 3)
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.root / "b").exists())

    def test_materialize(self):
        (self.ws / "empty").mkdir()
        tree = snapshot.capture(self.ws, EXCL)
        dest = self.root / "fork"
        info = snapshot.materialize(tree, dest)
        self.assertEqual(info["files"], 3)
        self.assertEqual(snapshot_files(dest), snapshot_files(self.ws))
        self.assertTrue((dest / "empty").is_dir())
        # empty existing dir is fine
        empty = self.root / "fork2"
        empty.mkdir()
        snapshot.materialize(tree, empty)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.materialize(tree, dest)

    def test_gc(self):
        tree = snapshot.capture(self.ws, EXCL)
        write(self.ws / "orphan.txt", "orphan data")
        tree2 = snapshot.capture(self.ws, EXCL)
        orphan = tree2["files"]["orphan.txt"]["h"]
        keep = snapshot.referenced_hashes([tree])
        self.assertNotIn(orphan, keep)
        res = snapshot.gc(keep)
        self.assertEqual(res["removed_objects"], 1)
        self.assertGreater(res["freed_bytes"], 0)
        self.assertFalse(snapshot.object_path(orphan).exists())
        for h in keep:
            self.assertTrue(snapshot.object_path(h).exists())

    def test_object_path_validation(self):
        for bad in ("zz", "../" + "a" * 61, "A" * 64, ""):
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.object_path(bad)

    def test_symlink_skipped(self):
        link = self.ws / "link.txt"
        try:
            os.symlink(self.ws / "keep.txt", link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unavailable")
        tree = snapshot.capture(self.ws, EXCL)
        self.assertNotIn("link.txt", tree["files"])
        self.assertIn({"path": "link.txt", "reason": "symlink", "bytes": 0}, tree["skipped"])
        self.assertEqual(snapshot.diff(self.ws, tree, EXCL)["added"], [])


if __name__ == "__main__":
    unittest.main()
