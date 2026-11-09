"""v0.3: dashboard data (read-only), atm_diff, and the safety checkpoint taken before a restore.

Run with: python -m unittest discover -s tests -p "test_v03_dashboard.py" -v
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import unittest
from pathlib import Path

from test_v02_flow import FlowBase, handlers, make_repo, write

dashboard_data = importlib.import_module("hermes_time_machine.dashboard_data")
ROOT = Path(__file__).resolve().parents[1]

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except Exception:  # pragma: no cover - depends on the environment
    HAS_FASTAPI = False


def j(raw: str) -> dict:
    return json.loads(raw)


class DashboardDataTest(FlowBase):
    def setUp(self):
        super().setUp()
        make_repo(self.ws)
        self.cp = j(handlers.atm_checkpoint({"label": "before work"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "v2\n")
        write(self.ws / "new.txt", "fresh\n")
        command = "git push origin main"
        self.pre("terminal", {"command": command}, turn_id="t1", tool_call_id="p1")
        self.post("terminal", {"command": command}, json.dumps({"output": "ok", "exit_code": 0}), tool_call_id="p1")

    def test_overview_has_checkpoints_and_permanent_effects(self):
        data = dashboard_data.overview()
        self.assertNotIn("error", data)
        self.assertEqual(data["workspace"], str(self.ws))
        self.assertIn(self.cp, [c["id"] for c in data["checkpoints"]])
        pushes = [e for e in data["effects"] if e["kind"] == "git_push"]
        self.assertEqual(len(pushes), 1)
        self.assertTrue(pushes[0]["permanent"])
        self.assertEqual(pushes[0]["via"], "command")
        self.assertEqual(data["summary"]["permanent"], 1)

    def test_reminded_flag_comes_from_reminded_json(self):
        record = [r for r in handlers.read_ledger() if r.get("kind") == "git_push"][0]
        storage = importlib.import_module("hermes_time_machine.storage")
        storage.write_json_atomic(storage.data_dir() / "reminded.json",
                                  {"s1": [f"{record['ts']}|{record['tool_call_id']}"]})
        data = dashboard_data.overview()
        self.assertTrue([e for e in data["effects"] if e["kind"] == "git_push"][0]["reminded"])
        self.assertEqual(data["summary"]["reminded"], 1)

    def test_checkpoint_shows_files_and_what_stays_true(self):
        data = dashboard_data.checkpoint(self.cp)
        self.assertIn("app.py", data["files"]["modified"])
        self.assertIn("new.txt", data["files"]["added"])
        self.assertEqual([e["kind"] for e in data["stays_true"]], ["git_push"])

    def test_file_diff(self):
        data = dashboard_data.file_diff(self.cp, "app.py")
        self.assertIn("-v1", data["diff"])
        self.assertIn("+v2", data["diff"])
        self.assertEqual(data["state"], "modified")
        added = dashboard_data.file_diff(self.cp, "new.txt")
        self.assertEqual(added["state"], "added")
        self.assertIn("+fresh", added["diff"])

    def test_bad_inputs_never_raise(self):
        self.assertIn("error", dashboard_data.checkpoint("../etc"))
        self.assertIn("error", dashboard_data.file_diff(self.cp, "../outside.txt"))
        self.assertIn("error", dashboard_data.file_diff(self.cp, "C:/Windows/win.ini"))
        self.assertIn("error", dashboard_data.file_diff("nope", "app.py"))

    def test_binary_files_are_not_diffed(self):
        (self.ws / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.assertEqual(dashboard_data.file_diff(self.cp, "blob.bin").get("note"), "Binary file.")

    def test_dashboard_never_writes_the_workspace(self):
        before = {p.name: p.read_bytes() for p in self.ws.iterdir() if p.is_file()}
        dashboard_data.overview()
        dashboard_data.checkpoint(self.cp)
        dashboard_data.file_diff(self.cp, "app.py")
        after = {p.name: p.read_bytes() for p in self.ws.iterdir() if p.is_file()}
        self.assertEqual(before, after)


class DiffToolAndSafetyCheckpointTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cp = j(handlers.atm_checkpoint({"label": "base"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "v2")

    def test_atm_diff_lists_and_diffs(self):
        listing = j(handlers.atm_diff({"checkpoint_id": self.cp}))
        self.assertIn("app.py", listing["changes"]["modified"])
        one = j(handlers.atm_diff({"checkpoint_id": self.cp, "path": "app.py"}))
        self.assertIn("+v2", one["diff"])

    def test_restore_takes_a_safety_checkpoint_that_undoes_it(self):
        out = j(handlers.atm_rewind({"checkpoint_id": self.cp, "confirm": True}))
        self.assertTrue(out["ok"])
        self.assertEqual((self.ws / "app.py").read_text(), "v1")
        safety = out["undo_checkpoint"]
        self.assertTrue(safety)
        self.assertIn(safety, out["undo_hint"])
        back = j(handlers.atm_rewind({"checkpoint_id": safety, "confirm": True}))
        self.assertTrue(back["ok"])
        self.assertEqual((self.ws / "app.py").read_text(), "v2")

    def test_slash_diff_with_path(self):
        commands = importlib.import_module("hermes_time_machine.commands")
        text = commands._diff([self.cp, "app.py"])
        self.assertIn("+v2", text)


@unittest.skipUnless(HAS_FASTAPI, "FastAPI is not installed")
class PluginApiRoutesTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cp = j(handlers.atm_checkpoint({"label": "base"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "v2")
        spec = importlib.util.spec_from_file_location("htm_plugin_api_test", ROOT / "dashboard" / "plugin_api.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = fastapi.FastAPI()
        app.include_router(module.router, prefix="/api/plugins/hermes-time-machine")
        self.client = TestClient(app)

    def test_routes(self):
        base = "/api/plugins/hermes-time-machine"
        ov = self.client.get(base + "/overview").json()
        self.assertIn(self.cp, [c["id"] for c in ov["checkpoints"]])
        cp = self.client.get(f"{base}/checkpoint/{self.cp}").json()
        self.assertIn("app.py", cp["files"]["modified"])
        diff = self.client.get(f"{base}/diff/{self.cp}", params={"path": "app.py"}).json()
        self.assertIn("+v2", diff["diff"])

    def test_no_write_routes(self):
        base = "/api/plugins/hermes-time-machine"
        for path in ("/overview", f"/checkpoint/{self.cp}", f"/diff/{self.cp}"):
            self.assertEqual(self.client.post(base + path).status_code, 405)


class ManifestTest(unittest.TestCase):
    def test_dashboard_manifest(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "hermes-time-machine")
        self.assertEqual(manifest["api"], "plugin_api.py")
        for key in ("entry", "css"):
            self.assertTrue((ROOT / "dashboard" / manifest[key]).is_file(), key)


if __name__ == "__main__":
    unittest.main()
