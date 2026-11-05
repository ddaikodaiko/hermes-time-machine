"""Smoke tests for hermes-time-machine.

Run with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
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


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("v1", encoding="utf-8")
        (self.workspace / "notes.txt").write_text("hello", encoding="utf-8")
        os.environ["ATM_DATA_DIR"] = str(root / "data")
        pkg.guard._reset_for_tests()
        pkg.handlers.configure(
            {
                "workspace": str(self.workspace),
                "auto_checkpoint": True,
                "risk_threshold": 0.6,
                "max_snapshot_mb": 50,
                "retention": 5,
            }
        )

    def tearDown(self):
        os.environ.pop("ATM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_checkpoint_and_rewind(self):
        created = json.loads(pkg.handlers.atm_checkpoint({"label": "before"}))
        self.assertTrue(created["ok"])

        (self.workspace / "app.py").write_text("v2", encoding="utf-8")
        (self.workspace / "new.txt").write_text("new", encoding="utf-8")

        preview = json.loads(pkg.handlers.atm_rewind({"confirm": False}))
        self.assertTrue(preview["preview"])

        restored = json.loads(pkg.handlers.atm_rewind({"confirm": True}))
        self.assertTrue(restored["ok"])
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_irreversible_effect_blocks_rewind(self):
        created = json.loads(pkg.handlers.atm_checkpoint({"label": "before"}))
        checkpoint_id = created["checkpoint"]["id"]
        pkg.storage.append_jsonl(
            pkg.storage.effects_path(),
            {
                "ts": pkg.storage.now_iso(),
                "kind": "git_push",
                "reversible": False,
                "reason": "test",
                "checkpoint_id": checkpoint_id,
            },
        )

        blocked = json.loads(
            pkg.handlers.atm_rewind({"checkpoint_id": checkpoint_id, "confirm": True})
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("Irreversible", blocked["error"])

        allowed = json.loads(
            pkg.handlers.atm_rewind(
                {
                    "checkpoint_id": checkpoint_id,
                    "confirm": True,
                    "allow_irreversible": True,
                }
            )
        )
        self.assertTrue(allowed["ok"])

    def test_fork(self):
        json.loads(pkg.handlers.atm_checkpoint({"label": "base"}))
        forked = json.loads(pkg.handlers.atm_fork({"name": "alt"}))
        self.assertTrue(forked["ok"])
        path = Path(forked["path"])
        self.assertTrue(path.is_dir())
        self.assertEqual((path / "app.py").read_text(encoding="utf-8"), "v1")

    def test_policy(self):
        risky, reasons = pkg.policy.evaluate(
            "terminal", {"command": "git push --force"}, self.workspace, []
        )
        self.assertGreaterEqual(risky, 0.9)
        self.assertTrue(reasons)

        safe, _ = pkg.policy.evaluate(
            "terminal", {"command": "ls -la"}, self.workspace, []
        )
        self.assertEqual(safe, 0.0)

    def test_invalid_checkpoint_id_is_rejected(self):
        self.assertIsNone(pkg.handlers._load("../../etc"))
        self.assertIsNone(pkg.handlers._load("ckpt_20260101T000000_ZZZZZZZZ"))
        result = json.loads(
            pkg.handlers.atm_rewind({"checkpoint_id": "../../etc", "confirm": True})
        )
        self.assertFalse(result["ok"])

    def test_extract_refuses_path_traversal(self):
        root = Path(self.tmp.name)
        archive = root / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../escaped.txt")
            data = b"owned"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        destination = root / "dest"
        with self.assertRaises((pkg.snapshot.SnapshotError, tarfile.TarError)):
            pkg.snapshot._extract(archive, destination)
        self.assertFalse((root / "escaped.txt").exists())

    def test_hook_checkpoints_only_risky_calls(self):
        before = len(pkg.handlers._all_manifests())
        pkg.guard.pre_tool_call(
            tool_name="terminal", args={"command": "ls"}, session_id="s1"
        )
        self.assertEqual(len(pkg.handlers._all_manifests()), before)

        pkg.guard.pre_tool_call(
            tool_name="terminal", args={"command": "git push origin main"}, session_id="s1"
        )
        self.assertEqual(len(pkg.handlers._all_manifests()), before + 1)


    def test_mutation_checkpoint_once_per_turn(self):
        before = len(pkg.handlers._all_manifests())

        pkg.guard.pre_tool_call(
            tool_name="write_file",
            args={"path": str(self.workspace / "app.py")},
            turn_id="turn-1",
        )
        self.assertEqual(len(pkg.handlers._all_manifests()), before + 1)

        pkg.guard.pre_tool_call(
            tool_name="patch",
            args={"path": str(self.workspace / "app.py")},
            turn_id="turn-1",
        )
        self.assertEqual(len(pkg.handlers._all_manifests()), before + 1)

        pkg.guard.pre_tool_call(
            tool_name="write_file",
            args={"path": str(self.workspace / "app.py")},
            turn_id="turn-2",
        )
        self.assertEqual(len(pkg.handlers._all_manifests()), before + 2)


if __name__ == "__main__":
    unittest.main()
