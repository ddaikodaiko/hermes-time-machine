"""End-to-end integration test against a real Hermes installation.

This does not mock the plugin API. It copies the plugin into a throwaway
Hermes home, lets ``PluginManager`` discover and load it, fires the real
``pre_tool_call`` (including the approve directive), ``post_tool_call``,
``transform_tool_result`` and ``pre_llm_call`` hooks, and calls the tools through
``tools.registry`` exactly as the agent would.

Run with the Python that has Hermes installed:

    python -m unittest tests.test_hermes_integration -v

Skipped automatically when Hermes is not importable.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import hermes_cli.plugins as hermes_plugins

    HERMES_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    hermes_plugins = None
    HERMES_AVAILABLE = False


@unittest.skipUnless(HERMES_AVAILABLE, "Hermes Agent is not installed")
class HermesIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("v1", encoding="utf-8")

        self.home = root / "hermes-home"
        plugin_dir = self.home / "plugins" / "hermes-time-machine"
        plugin_dir.parent.mkdir(parents=True)
        shutil.copytree(
            ROOT,
            plugin_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests"),
        )

        (self.home / "config.yaml").write_text(
            "plugins:\n"
            "  enabled:\n"
            "    - hermes-time-machine\n"
            "  entries:\n"
            "    hermes-time-machine:\n"
            "      settings:\n"
            f"        workspace: {json.dumps(str(self.workspace))}\n"
            "        auto_checkpoint: true\n"
            "        risk_threshold: 0.6\n"
            "        max_snapshot_mb: 50\n"
            "        retention: 5\n",
            encoding="utf-8",
        )

        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ.pop("ATM_DATA_DIR", None)

        from hermes_cli.plugins import get_plugin_manager

        self.pm = get_plugin_manager()
        self.pm.discover_and_load(force=True)

        module = self._plugin().module
        if module is not None:
            module.guard._reset_for_tests()

        from tools.registry import registry

        self.registry = registry

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _plugin(self):
        for key, plugin in self.pm._plugins.items():
            if "time-machine" in key:
                return plugin
        self.fail("plugin not discovered")

    def _call(self, name: str, args: dict) -> dict:
        result = self.registry.dispatch(name, args)
        return json.loads(result) if isinstance(result, str) else result

    def _checkpoints(self) -> list:
        return self._call("atm_list", {})["checkpoints"]

    # -- tests -----------------------------------------------------------

    def test_plugin_loads_with_tools_hooks_and_command(self):
        plugin = self._plugin()
        self.assertTrue(plugin.enabled, plugin.error)
        self.assertIsNone(plugin.error)
        self.assertEqual(
            set(plugin.tools_registered),
            {"atm_checkpoint", "atm_rewind", "atm_fork", "atm_list", "atm_effects"},
        )
        self.assertEqual(
            set(plugin.hooks_registered),
            {"pre_tool_call", "post_tool_call", "transform_tool_result", "pre_llm_call", "post_compaction"},
        )
        self.assertEqual(plugin.commands_registered, ["timemachine"])

    def test_safe_command_does_not_checkpoint(self):
        self.pm.invoke_hook(
            "pre_tool_call", tool_name="terminal", args={"command": "ls -la"}, task_id="t1"
        )
        self.assertEqual(self._checkpoints(), [])

    def _directive(self, command: str, **ids):
        return hermes_plugins._get_pre_tool_call_directive_details(
            "terminal", {"command": command}, task_id="t1", session_id="s1", **ids
        )

    def test_push_surfaces_an_approve_directive_through_hermes(self):
        directive = self._directive("git push origin main", tool_call_id="p1", turn_id="turn-1")
        self.assertEqual(directive.action, "approve")
        self.assertIn("Point of no return", directive.message)
        self.assertTrue(directive.rule_key.startswith("hermes-time-machine:git_push"))
        # A safe command is not escalated.
        self.assertIsNone(self._directive("ls -la", tool_call_id="p2").action)

    def test_model_restore_goes_through_hermes_approval(self):
        ckpt = self._call("atm_checkpoint", {"label": "good"})["checkpoint"]["id"]
        (self.workspace / "app.py").write_text("v2-broken", encoding="utf-8")
        args = {"checkpoint_id": ckpt, "confirm": True}
        directive = hermes_plugins._get_pre_tool_call_directive_details(
            "atm_rewind", args, task_id="t1", session_id="s1", tool_call_id="r1"
        )
        self.assertEqual(directive.action, "approve")
        self.assertIn("Restore workspace", directive.message)
        self.assertIn("1 modified", directive.message)
        # Previews and forks are not escalated.
        preview = hermes_plugins._get_pre_tool_call_directive_details(
            "atm_rewind", {"checkpoint_id": ckpt}, task_id="t1", session_id="s1"
        )
        self.assertIsNone(preview.action)

    def test_risky_command_checkpoints_then_rewind_restores(self):
        command = "git push origin main --force"
        directive = self._directive(command, tool_call_id="p1", turn_id="turn-1")
        self.assertEqual(directive.action, "approve")
        checkpoints = self._checkpoints()
        self.assertEqual(len(checkpoints), 1)
        self.assertTrue(checkpoints[0]["auto"])

        # The agent breaks the workspace.
        (self.workspace / "app.py").write_text("v2-broken", encoding="utf-8")
        (self.workspace / "junk.txt").write_text("junk", encoding="utf-8")

        # The push succeeded.
        self.pm.invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": command},
            result=json.dumps({"output": "ok", "exit_code": 0, "error": None}),
            status="ok",
            task_id="t1",
            session_id="s1",
            tool_call_id="p1",
        )

        effects = self._call("atm_effects", {})
        self.assertFalse(effects["safe_to_rewind"])
        self.assertEqual(effects["irreversible"][0]["kind"], "git_push")
        self.assertEqual(effects["irreversible"][0]["status"], "succeeded")

        preview = self._call("atm_rewind", {"confirm": False})
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["safe_to_rewind"])
        self.assertIn("app.py", preview["changes"]["modified"])
        self.assertIn("junk.txt", preview["changes"]["added"])

        blocked = self._call("atm_rewind", {"confirm": True})
        self.assertFalse(blocked["ok"])
        self.assertEqual(
            (self.workspace / "app.py").read_text(encoding="utf-8"), "v2-broken"
        )

        restored = self._call(
            "atm_rewind", {"confirm": True, "allow_irreversible": True}
        )
        self.assertTrue(restored["ok"], restored)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertFalse((self.workspace / "junk.txt").exists())
        self.assertEqual(restored["still_true_outside"][0]["kind"], "git_push")

    def test_effect_memory_and_result_annotation_hooks(self):
        self._directive("git push origin main", tool_call_id="p1", turn_id="turn-1")
        self.pm.invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": "git push origin main"},
            result=json.dumps({"output": "ok", "exit_code": 0, "error": None}),
            status="ok",
            task_id="t1",
            session_id="s1",
            tool_call_id="p1",
        )
        annotated = self.pm.invoke_hook(
            "transform_tool_result",
            tool_name="terminal",
            args={"command": "git push origin main"},
            result=json.dumps({"output": "ok", "exit_code": 0}),
            session_id="s1",
            tool_call_id="p1",
        )
        self.assertTrue(any("time_machine" in str(item) for item in annotated), annotated)

        forgotten = self.pm.invoke_hook(
            "pre_llm_call",
            session_id="s1",
            conversation_history=[{"role": "user", "content": "continue"}],
            user_message="continue",
            is_first_turn=False,
        )
        contexts = [r["context"] for r in forgotten if isinstance(r, dict)]
        self.assertEqual(len(contexts), 1)
        self.assertIn("Effect memory", contexts[0])

    def test_slash_command_is_registered_and_answers(self):
        handler = hermes_plugins.get_plugin_command_handler("timemachine")
        self.assertIsNotNone(handler)
        self._call("atm_checkpoint", {"label": "base"})
        text = handler("status", session_id="s1", platform="cli")
        self.assertIn("Checkpoints: 1", text)
        self.assertIn("base", handler("list", session_id="s1", platform="cli"))

    def test_write_file_triggers_one_automatic_checkpoint_per_turn(self):
        self.pm.invoke_hook(
            "pre_tool_call",
            tool_name="write_file",
            args={"path": str(self.workspace / "app.py")},
            task_id="t1",
            turn_id="turn-1",
        )
        self.pm.invoke_hook(
            "pre_tool_call",
            tool_name="patch",
            args={"path": str(self.workspace / "app.py")},
            task_id="t1",
            turn_id="turn-1",
        )
        checkpoints = self._checkpoints()
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["trigger"]["type"], "mutation")

    def test_manual_checkpoint_and_fork(self):
        created = self._call("atm_checkpoint", {"label": "base"})
        self.assertTrue(created["ok"])

        forked = self._call("atm_fork", {"name": "alt"})
        self.assertTrue(forked["ok"])
        path = Path(forked["path"])
        self.assertTrue(path.is_dir())
        self.assertEqual((path / "app.py").read_text(encoding="utf-8"), "v1")


if __name__ == "__main__":
    unittest.main()
