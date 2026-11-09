"""v0.2.2: effects from MCP/plugin tools, package publishers, persistent effect-memory reminders.

Run with: python -m unittest discover -s tests -p "test_v022_coverage.py" -v
No network, no subprocesses: tool results are canned dicts, project files are written.
"""

from __future__ import annotations

import importlib
import json
import unittest

from test_v02_flow import FlowBase, guard, pkg, write

effects = importlib.import_module("hermes_time_machine.effects")
tool_effects = importlib.import_module("hermes_time_machine.tool_effects")
gate = importlib.import_module("hermes_time_machine.gate")

PR_ARGS = {"owner": "o", "repo": "r", "title": "Fix error handling", "head": "fix", "base": "main"}
PR_RESULT = json.dumps({"number": 42, "html_url": "https://github.com/o/r/pull/42", "state": "open"})


class ToolClassifierTest(unittest.TestCase):
    def kinds(self, name, args=None):
        return [e["kind"] for e in tool_effects.classify_tool(name, args or {})]

    def test_github_family(self):
        self.assertEqual(self.kinds("mcp__github__create_pull_request", PR_ARGS), ["github_pr"])
        self.assertEqual(self.kinds("mcp__github__merge_pull_request", {"owner": "o", "repo": "r", "pullNumber": 3}),
                         ["github_merge"])
        self.assertEqual(self.kinds("mcp__github__create_release", {"owner": "o", "repo": "r", "tag_name": "v1"}),
                         ["github_release"])
        self.assertEqual(self.kinds("mcp__github__push_files", {"owner": "o", "repo": "r", "branch": "main",
                                                                  "files": [{}, {}]}), ["github_commit"])
        self.assertEqual(self.kinds("mcp__github__delete_branch", {"owner": "o", "repo": "r", "branch": "x"}),
                         ["github_branch_delete"])

    def test_messaging_and_generic(self):
        self.assertEqual(self.kinds("mcp__slack__slack_post_message", {"channel": "#general", "text": "hi"}),
                         ["message_send"])
        self.assertEqual(self.kinds("mcp__vercel__deploy_project", {"project": "web"}), ["tool_call"])
        self.assertEqual(self.kinds("mcp__stripe__create_refund", {"id": "ch_1"}), ["tool_call"])

    def test_reads_and_builtins_never_trigger(self):
        for name in ("mcp__github__list_pull_requests", "mcp__github__get_pull_request",
                     "mcp__slack__slack_get_user_posts", "mcp__db__search_deleted_rows",
                     "terminal", "write_file", "atm_rewind", "browser_click", "web_search"):
            self.assertEqual(self.kinds(name, {"owner": "o", "repo": "r"}), [], name)

    def test_arguments_never_decide(self):
        # the word "delete" in a PR body is not a deletion
        self.assertEqual(self.kinds("mcp__notes__create_note", {"body": "delete and publish everything"}), [])

    def test_dry_run_is_ignored(self):
        self.assertEqual(self.kinds("mcp__vercel__deploy_project", {"dry_run": True}), [])

    def test_unsafe_values_never_reach_a_compensation(self):
        args = {"owner": "o", "repo": "r; rm -rf /", "tag_name": "v1"}
        fx = tool_effects.classify_tool("mcp__github__create_release", args)[0]
        self.assertIsNone(fx["compensation"])

    def test_hashed_long_names(self):
        name = ("mcp__" + "server_" * 5 + "__create_pull_request_with_a_very_long_tail")[:55] + "_deadbeef"
        name = name.ljust(64, "x")[:64]
        self.assertIsInstance(tool_effects.split_name(name), tuple)

    def test_outcome_reads_structure_not_words(self):
        self.assertEqual(tool_effects.tool_outcome(PR_RESULT)["status"], "unknown")
        self.assertEqual(tool_effects.tool_outcome({"isError": True, "error": "403"})["status"], "failed")
        self.assertEqual(tool_effects.tool_outcome("Error: bad credentials")["status"], "failed")
        self.assertTrue(tool_effects.tool_outcome(
            {"error": "the outcome is UNKNOWN: timed out"})["uncertain"])

    def test_never_raises(self):
        for name, args in ((None, None), (3, []), ("mcp__", {"owner": object()}), ("mcp____", "x")):
            self.assertIsInstance(tool_effects.classify_tool(name, args), list)


class ToolGateFlowTest(FlowBase):
    def test_mcp_pr_asks_and_records_with_compensation(self):
        directive = self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m1")
        self.assertEqual(directive["action"], "approve")
        self.assertIn("pull request", directive["message"])
        self.post("mcp__github__create_pull_request", PR_ARGS, PR_RESULT, tool_call_id="m1")
        rows = [r for r in self.ledger() if r.get("kind") == "github_pr"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(rows[0]["target"], "o/r#42")
        self.assertEqual(rows[0]["compensation"], "gh pr close 42 --repo o/r")

    def test_failed_tool_call_is_recorded_as_failed(self):
        self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m2")
        self.post("mcp__github__create_pull_request", PR_ARGS, {"isError": True, "error": "403"}, tool_call_id="m2")
        rows = [r for r in self.ledger() if r.get("kind") == "github_pr"]
        self.assertEqual(rows[0]["status"], "failed")

    def test_read_tool_passes_silently(self):
        self.assertIsNone(self.pre("mcp__github__list_pull_requests", {"owner": "o", "repo": "r"}, tool_call_id="m3"))

    def test_warn_mode_records_without_asking(self):
        self.configure(gate_tools="warn")
        self.assertIsNone(self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m4"))
        self.post("mcp__github__create_pull_request", PR_ARGS, PR_RESULT, tool_call_id="m4")
        self.assertEqual(len([r for r in self.ledger() if r.get("kind") == "github_pr"]), 1)

    def test_off_mode_ignores_tools(self):
        self.configure(gate_tools="off")
        self.assertIsNone(self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m5"))
        self.post("mcp__github__create_pull_request", PR_ARGS, PR_RESULT, tool_call_id="m5")
        self.assertEqual([r for r in self.ledger() if r.get("kind") == "github_pr"], [])

    def test_allow_pattern_on_tool_name(self):
        self.configure(gate_tool_allow_patterns=[r"^mcp__slack__"])
        args = {"channel": "#ci", "text": "green"}
        self.assertIsNone(self.pre("mcp__slack__slack_post_message", args, tool_call_id="m6"))
        self.assertEqual(self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m7")["action"], "approve")

    def test_off_mode_does_not_touch_shell_gate(self):
        self.configure(gate_tools="off")
        out = self.pre("terminal", {"command": "npm publish"}, tool_call_id="m8")
        self.assertEqual(out["action"], "approve")

    def test_bad_mode_falls_back_to_approve(self):
        self.assertEqual(pkg.plugin_config.normalize({"gate_tools": "maybe"})["gate_tools"], "approve")


class PublisherTest(FlowBase):
    def found(self, command, cwd=None):
        return effects.preflight("terminal", {"command": command}, cwd or self.ws)

    def test_cargo_publish_reads_cargo_toml(self):
        write(self.ws / "Cargo.toml", '[package]\nname = "foo"\nversion = "1.2.3"\n')
        fx = self.found("cargo publish")[0]
        self.assertEqual((fx["kind"], fx["target"]), ("cargo_publish", "foo@1.2.3"))
        self.assertEqual(fx["compensation"], "cargo yank --version 1.2.3 foo")

    def test_cargo_manifest_path(self):
        write(self.ws / "crates/bar/Cargo.toml", '[package]\nname = "bar"\nversion = "0.1.0"\n')
        fx = self.found("cargo publish --manifest-path crates/bar/Cargo.toml")[0]
        self.assertEqual(fx["target"], "bar@0.1.0")

    def test_cargo_without_manifest_still_gated(self):
        fx = self.found("cargo publish")[0]
        self.assertIsNone(fx["compensation"])
        self.assertIn("yank", fx["compensation_note"])

    def test_pypi_publishers(self):
        write(self.ws / "pyproject.toml", '[project]\nname = "baz"\nversion = "0.4.0"\n')
        for command in ("twine upload dist/*", "python -m twine upload dist/*", "uv publish",
                        "poetry publish --build", "hatch publish"):
            fx = self.found(command)
            self.assertEqual([f["kind"] for f in fx], ["pypi_publish"], command)
            self.assertEqual(fx[0]["target"], "baz==0.4.0", command)
            self.assertIn("pypi.org/manage/project/baz", fx[0]["compensation_note"])

    def test_pypi_name_from_wheel(self):
        fx = self.found("twine upload dist/qux-2.0.0-py3-none-any.whl")[0]
        self.assertEqual(fx["target"], "qux==2.0.0")

    def test_poetry_metadata(self):
        write(self.ws / "pyproject.toml", '[tool.poetry]\nname = "p"\nversion = "9.9"\n')
        self.assertEqual(self.found("poetry publish")[0]["target"], "p==9.9")

    def test_gem_push(self):
        fx = self.found("gem push pkg/mygem-0.3.1.gem")[0]
        self.assertEqual(fx["compensation"], "gem yank mygem -v 0.3.1")

    def test_mentions_and_dry_runs_pass(self):
        for command in ("echo cargo publish", "cargo publish --dry-run", "grep 'twine upload' Makefile",
                        "poetry publish --dry-run", "uv publish --dry-run"):
            self.assertEqual(self.found(command), [], command)

    def test_gate_message_mentions_yank(self):
        write(self.ws / "Cargo.toml", '[package]\nname = "foo"\nversion = "1.2.3"\n')
        out = self.pre("terminal", {"command": "cargo publish"}, tool_call_id="c1")
        self.assertEqual(out["action"], "approve")
        self.assertIn("cargo yank --version 1.2.3 foo", out["message"])
        self.assertLessEqual(len(out["message"]), gate.MAX_MESSAGE)


class PersistentReminderTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.pre("mcp__github__create_pull_request", PR_ARGS, tool_call_id="m1")
        self.post("mcp__github__create_pull_request", PR_ARGS, PR_RESULT, tool_call_id="m1")
        self.history = [{"role": "user", "content": "continue"}]

    def llm(self):
        return guard.pre_llm_call(session_id="s1", conversation_history=self.history)

    def test_tool_effect_is_remembered(self):
        out = self.llm()
        self.assertIsNotNone(out)
        self.assertIn("o/r#42", out["context"])

    def test_reminder_is_not_repeated_after_restart(self):
        self.assertIsNotNone(self.llm())
        guard._NOTIFIED.clear()  # gateway restart: only reminded.json remains
        self.assertIsNone(self.llm())

    def test_corrupt_reminded_file_is_tolerated(self):
        write(guard._reminded_path(), "{not json")
        self.assertIsNotNone(self.llm())


if __name__ == "__main__":
    unittest.main()
