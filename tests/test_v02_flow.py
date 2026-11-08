"""End-to-end flow tests for v0.2 (no Hermes needed).

Hooks are called directly with the payloads verified against Hermes 0.23.
Fake git repositories are built by writing files; git is never executed.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
    existing = sys.modules.get("hermes_time_machine")
    if existing is not None and hasattr(existing, "commands"):
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
handlers, guard, memory, commands = pkg.handlers, pkg.guard, pkg.memory, pkg.commands
snapshot, storage = pkg.snapshot, pkg.storage

LOCAL = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
REMOTE = "3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d"
GIT_CONFIG = """[core]
\trepositoryformatversion = 0
[remote "origin"]
\turl = https://github.com/o/r.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
\tremote = origin
\tmerge = refs/heads/main
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path) -> None:
    git = root / ".git"
    write(git / "HEAD", "ref: refs/heads/main\n")
    write(git / "config", GIT_CONFIG)
    write(git / "refs/heads/main", LOCAL + "\n")
    write(git / "refs/remotes/origin/main", REMOTE + "\n")


def ok(raw: str) -> dict:
    data = json.loads(raw)
    return data


class FlowBase(unittest.TestCase):
    CONFIG: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "project"
        self.ws.mkdir()
        write(self.ws / "app.py", "v1")
        write(self.ws / "notes.txt", "hello")
        os.environ["ATM_DATA_DIR"] = str(self.root / "data")
        guard._reset_for_tests()
        self.configure()
        # Hermes provides the session folder; in tests it is the workspace.
        patcher = mock.patch.object(handlers, "session_dir", return_value=self.ws)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, **overrides):
        config = {"workspace": str(self.ws), "retention": 5, "max_snapshot_mb": 50}
        config.update(self.CONFIG)
        config.update(overrides)
        handlers.configure(pkg.plugin_config.normalize(config))

    def tearDown(self):
        os.environ.pop("ATM_DATA_DIR", None)
        self.tmp.cleanup()

    def checkpoints(self, all_ws=True):
        return handlers.list_manifests(all_ws)

    def pre(self, tool, args, **kw):
        kw.setdefault("session_id", "s1")
        return guard.pre_tool_call(tool_name=tool, args=args, **kw)

    def post(self, tool, args, result, **kw):
        kw.setdefault("session_id", "s1")
        kw.setdefault("status", "ok")
        return guard.post_tool_call(tool_name=tool, args=args, result=result, **kw)

    def ledger(self):
        return storage.read_jsonl(storage.effects_path())


class CheckpointAnnotationTest(FlowBase):
    def test_first_write_checkpoints_and_only_first_is_annotated(self):
        args = {"path": str(self.ws / "app.py"), "content": "v2"}
        self.assertIsNone(self.pre("write_file", args, turn_id="t1", tool_call_id="c1"))
        self.assertEqual(len(self.checkpoints()), 1)
        self.assertIsNone(self.pre("write_file", args, turn_id="t1", tool_call_id="c2"))
        self.assertEqual(len(self.checkpoints()), 1)

        result = json.dumps({"bytes_written": 2})
        first = guard.transform_tool_result(
            tool_name="write_file", args=args, result=result, session_id="s1", tool_call_id="c1"
        )
        second = guard.transform_tool_result(
            tool_name="write_file", args=args, result=result, session_id="s1", tool_call_id="c2"
        )
        self.assertIsNotNone(first)
        note = json.loads(first)["time_machine"]
        self.assertEqual(note["checkpoint"], self.checkpoints()[0]["id"])
        self.assertIn("atm_rewind", note["note"])
        self.assertIsNone(second)

    def test_plain_text_result_gets_appended_note(self):
        args = {"path": str(self.ws / "app.py")}
        self.pre("write_file", args, turn_id="t1", tool_call_id="c1")
        out = guard.transform_tool_result(
            tool_name="write_file", args=args, result="done", session_id="s1", tool_call_id="c1"
        )
        self.assertTrue(out.startswith("done\n\n[time-machine] "))

    def test_annotation_can_be_disabled(self):
        self.configure(annotate_results=False)
        args = {"path": str(self.ws / "app.py")}
        self.pre("write_file", args, turn_id="t1", tool_call_id="c1")
        self.assertIsNone(
            guard.transform_tool_result(
                tool_name="write_file", args=args, result="{}", session_id="s1", tool_call_id="c1"
            )
        )

    def test_listing_is_filtered_by_workspace(self):
        json.loads(handlers.atm_checkpoint({"label": "here"}))
        other = self.root / "other"
        other.mkdir()
        write(other / "x.txt", "x")
        self.configure(workspace=str(other))
        json.loads(handlers.atm_checkpoint({"label": "there"}))
        mine = ok(handlers.atm_list({}))
        self.assertEqual([c["label"] for c in mine["checkpoints"]], ["there"])
        everything = ok(handlers.atm_list({"all_workspaces": True}))
        self.assertEqual(len(everything["checkpoints"]), 2)


class UnsafeWorkspaceTest(FlowBase):
    def test_gate_and_ledger_work_without_a_workspace(self):
        fake_home = self.root / "home"
        fake_home.mkdir()
        make_repo(fake_home)
        self.configure(workspace=str(fake_home))
        handlers_session = mock.patch.object(handlers, "session_dir", return_value=fake_home)
        handlers_session.start()
        self.addCleanup(handlers_session.stop)
        with mock.patch.object(Path, "home", return_value=fake_home):
            directive = self.pre(
                "terminal", {"command": "git push origin main"}, turn_id="t1", tool_call_id="p1"
            )
            self.assertEqual(directive["action"], "approve")
            self.assertEqual(self.checkpoints(), [])
            self.pre("write_file", {"path": str(fake_home / "a.txt")}, turn_id="t1", tool_call_id="w1")
            self.pre("write_file", {"path": str(fake_home / "b.txt")}, turn_id="t2", tool_call_id="w2")
            self.post(
                "terminal",
                {"command": "git push origin main"},
                json.dumps({"exit_code": 0, "output": "ok"}),
                tool_call_id="p1",
                turn_id="t1",
            )
        self.assertEqual(self.checkpoints(), [])
        records = self.ledger()
        self.assertEqual([r["kind"] for r in records], ["git_push"])
        self.assertEqual(records[0]["status"], "succeeded")
        self.assertEqual(records[0]["workspace"], str(fake_home))
        journal = storage.read_jsonl(storage.journal_path())
        skipped = [j for j in journal if j.get("reason") == "unsafe_workspace"]
        self.assertEqual(len(skipped), 1, "logged once per path")


class GateAndLedgerTest(FlowBase):
    def setUp(self):
        super().setUp()
        make_repo(self.ws)

    def test_push_is_gated_with_compensation_and_recorded(self):
        command = "git push origin main"
        directive = self.pre(
            "terminal", {"command": command}, turn_id="t1", tool_call_id="p1"
        )
        self.assertEqual(directive["action"], "approve")
        self.assertIn("--force-with-lease", directive["message"])
        self.assertIn("origin/main", directive["rule_key"])
        self.assertEqual(len(self.checkpoints()), 1)
        self.assertIn(self.checkpoints()[0]["id"], directive["message"])

        self.post(
            "terminal",
            {"command": command},
            json.dumps({"output": "pushed", "exit_code": 0, "error": None}),
            tool_call_id="p1",
            turn_id="t1",
        )
        [record] = self.ledger()
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["tool_call_id"], "p1")
        self.assertEqual(record["session_id"], "s1")
        self.assertEqual(record["workspace"], str(self.ws))
        self.assertTrue(record["compensation"].startswith("git push --force-with-lease"))

        preview = ok(handlers.atm_rewind({}, session_id="s1"))
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["safe_to_rewind"])
        self.assertEqual(preview["irreversible"][0]["kind"], "git_push")
        self.assertIn("compensation", preview["irreversible"][0])

        refused = ok(handlers.atm_rewind({"confirm": True}, session_id="s1"))
        self.assertFalse(refused["ok"])

        write(self.ws / "app.py", "broken")
        done = ok(handlers.atm_rewind({"confirm": True, "allow_irreversible": True}, session_id="s1"))
        self.assertTrue(done["ok"], done)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertEqual(done["still_true_outside"][0]["kind"], "git_push")
        self.assertIn("NOT undone", done["note"])
        journal = storage.read_jsonl(storage.journal_path())
        self.assertTrue(any(j.get("type") == "rewind" for j in journal))

    def test_failed_push_is_recorded_as_failed_and_does_not_block(self):
        command = "git push origin main"
        self.pre("terminal", {"command": command}, turn_id="t1", tool_call_id="p1")
        self.post(
            "terminal",
            {"command": command},
            json.dumps({"output": "rejected", "exit_code": 1, "error": None}),
            tool_call_id="p1",
        )
        [record] = self.ledger()
        self.assertEqual(record["status"], "failed")
        preview = ok(handlers.atm_rewind({}, session_id="s1"))
        self.assertTrue(preview["safe_to_rewind"])
        effects = ok(handlers.atm_effects({}))
        self.assertTrue(effects["safe_to_rewind"])
        self.assertEqual(effects["effects"][0]["status"], "failed")

    def test_blocked_calls_are_not_recorded(self):
        command = "git push origin main"
        self.pre("terminal", {"command": command}, tool_call_id="p1")
        self.post("terminal", {"command": command}, "blocked", tool_call_id="p1", status="blocked")
        self.assertEqual(self.ledger(), [])

    def test_gate_does_not_cite_checkpoint_of_another_workspace(self):
        other = self.root / "other"
        other.mkdir()
        write(other / "f.txt", "x")
        self.configure(workspace=str(other))
        foreign = handlers.create_checkpoint("foreign", {}, auto=False)
        self.assertIsNotNone(foreign)
        self.configure(workspace=str(self.ws), auto_checkpoint=False)
        directive = self.pre("terminal", {"command": "git push origin main"}, turn_id="t9")
        self.assertEqual(directive["action"], "approve")
        self.assertNotIn(foreign["id"], directive["message"])
        self.assertNotIn("covered by checkpoint", directive["message"])

    def test_gate_modes_and_allow_patterns(self):
        command = "git push origin main"
        self.configure(irreversible_gate="warn")
        self.assertIsNone(self.pre("terminal", {"command": command}, tool_call_id="a"))
        journal = storage.read_jsonl(storage.journal_path())
        self.assertTrue(any(j.get("type") == "gate_warning" for j in journal))
        self.configure(irreversible_gate="off")
        self.assertIsNone(self.pre("terminal", {"command": command}, tool_call_id="b"))
        self.configure(irreversible_gate="nonsense")
        self.assertEqual(handlers.config()["irreversible_gate"], "approve")
        self.assertEqual(self.pre("terminal", {"command": command}, tool_call_id="c")["action"], "approve")
        self.configure(gate_allow_patterns=[r"git push origin main"])
        self.assertIsNone(self.pre("terminal", {"command": command}, tool_call_id="d"))
        self.assertEqual(
            self.pre("terminal", {"command": "git push origin dev"}, tool_call_id="e")["action"],
            "approve",
        )

    def test_gate_works_with_auto_checkpoint_disabled(self):
        self.configure(auto_checkpoint=False)
        directive = self.pre("terminal", {"command": "git push origin main"}, tool_call_id="p1")
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(self.checkpoints(), [])


class EffectMemoryTest(FlowBase):
    def setUp(self):
        super().setUp()
        make_repo(self.ws)
        command = "git push origin main"
        self.pre("terminal", {"command": command}, turn_id="t1", tool_call_id="p1")
        self.post(
            "terminal",
            {"command": command},
            json.dumps({"output": "ok", "exit_code": 0, "error": None}),
            tool_call_id="p1",
        )

    def history(self, with_push: bool):
        history = [{"role": "user", "content": "ship it"}]
        if with_push:
            history += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "p1",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "git push origin main"}),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "p1", "content": "{}"},
            ]
        history.append({"role": "user", "content": "continue"})
        return history

    def llm(self, history, session_id="s1"):
        return guard.pre_llm_call(
            session_id=session_id, conversation_history=history, user_message="continue",
            is_first_turn=False,
        )

    def test_visible_effect_is_not_reminded(self):
        self.assertIsNone(self.llm(self.history(True)))

    def test_forgotten_effect_is_reminded_once(self):
        first = self.llm(self.history(False))
        self.assertIsNotNone(first)
        text = first["context"]
        self.assertIn("Effect memory", text)
        self.assertIn("git push", text)
        self.assertIn("--force-with-lease", text)
        self.assertLessEqual(len(text), 1500)
        self.assertIsNone(self.llm(self.history(False)))

    def test_compaction_rotation_keeps_effect_memory(self):
        guard.post_compaction(session_id="B", old_session_id="s1", kind="compress", reason="auto")
        first = self.llm(self.history(False), session_id="B")
        self.assertIsNotNone(first)
        self.assertIn("git push", first["context"])
        self.assertIsNone(self.llm(self.history(False), session_id="B"))

    def test_lineage_survives_restart(self):
        guard.post_compaction(session_id="B", old_session_id="s1")
        guard._LINEAGE.clear()  # simulate a restart: only lineage.json remains
        self.assertEqual(guard.lineage("B"), ["B", "s1"])
        self.assertIsNotNone(self.llm(self.history(False), session_id="B"))

    def test_in_place_compaction_changes_nothing(self):
        guard.post_compaction(session_id="B", old_session_id="")
        guard.post_compaction(session_id="B", old_session_id="B")
        self.assertEqual(guard.lineage("B"), ["B"])
        self.assertIsNone(self.llm(self.history(False), session_id="B"))

    def test_lineage_is_cycle_safe(self):
        guard.post_compaction(session_id="B", old_session_id="s1")
        guard.post_compaction(session_id="s1", old_session_id="B")
        self.assertEqual(guard.lineage("B"), ["B", "s1"])

    def test_subagent_parent_session_id_is_not_followed(self):
        out = guard.pre_llm_call(
            session_id="C", parent_session_id="s1", conversation_history=self.history(False)
        )
        self.assertIsNone(out)

    def test_compaction_rotation_with_parent_kwarg_still_reminds(self):
        guard.post_compaction(session_id="B", old_session_id="s1")
        out = guard.pre_llm_call(
            session_id="B", parent_session_id="s1", conversation_history=self.history(False)
        )
        self.assertIsNotNone(out)

    def test_other_sessions_are_not_reminded(self):
        self.assertIsNone(self.llm(self.history(False), session_id="other"))

    def test_command_text_counts_as_visible_when_no_id_was_recorded(self):
        records = [dict(self.ledger()[0], tool_call_id=None)]
        self.assertEqual(memory.forgotten("s1", self.history(True), records, set()), [])
        self.assertEqual(len(memory.forgotten("s1", self.history(False), records, set())), 1)

    def test_failed_effects_are_never_reminded(self):
        records = [dict(self.ledger()[0], status="failed")]
        self.assertEqual(memory.forgotten("s1", self.history(False), records, set()), [])

    def test_disabled(self):
        self.configure(effect_memory=False)
        self.assertIsNone(self.llm(self.history(False)))

    def test_render_is_bounded(self):
        base = self.ledger()[0]
        many = [dict(base, ts=f"2026-01-01T00:00:{i:02d}+00:00", compensation="x" * 900) for i in range(9)]
        text = memory.render(many)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("Treat them as done", text)


class StruggleTest(FlowBase):
    def fail(self, call_id):
        args = {"command": "pytest -x"}
        result = json.dumps({"output": "FAILED", "exit_code": 1, "error": None})
        self.post("terminal", args, result, tool_call_id=call_id)
        return guard.transform_tool_result(
            tool_name="terminal", args=args, result=result, session_id="s1", tool_call_id=call_id
        )

    def test_hint_after_threshold_and_not_repeated(self):
        self.pre("write_file", {"path": str(self.ws / "app.py")}, turn_id="t1", tool_call_id="w1")
        checkpoint_id = self.checkpoints()[0]["id"]
        outputs = [self.fail(f"f{i}") for i in range(1, 7)]
        self.assertEqual(outputs[:3], [None, None, None])
        hint = json.loads(outputs[3])["time_machine"]["hint"]
        self.assertIn("4 failing calls", hint)
        self.assertIn(checkpoint_id, hint)
        self.assertIn("atm_rewind", hint)
        self.assertEqual(outputs[4:], [None, None])

    def test_success_resets_the_streak(self):
        self.pre("write_file", {"path": str(self.ws / "app.py")}, turn_id="t1", tool_call_id="w1")
        for i in range(3):
            self.assertIsNone(self.fail(f"f{i}"))
        good = json.dumps({"output": "ok", "exit_code": 0, "error": None})
        self.post("terminal", {"command": "pytest"}, good, tool_call_id="g")
        for i in range(3):
            self.assertIsNone(self.fail(f"h{i}"))

    def test_threshold_zero_disables(self):
        self.configure(struggle_threshold=0)
        self.pre("write_file", {"path": str(self.ws / "app.py")}, turn_id="t1", tool_call_id="w1")
        self.assertEqual([self.fail(f"f{i}") for i in range(8)], [None] * 8)


class GarbageInputTest(FlowBase):
    def test_hooks_never_raise(self):
        self.assertIsNone(guard.pre_tool_call(tool_name="terminal", args=None))
        self.assertIsNone(guard.pre_tool_call(tool_name=None, args=None))
        self.assertIsNone(guard.pre_tool_call())
        self.assertIsNone(guard.pre_tool_call(tool_name="terminal", args="not a dict", session_id=5))
        self.assertIsNone(guard.post_tool_call(tool_name="terminal", args=None, result=None))
        self.assertIsNone(guard.post_tool_call(tool_name="terminal", args={"command": "git push"}, result=object()))
        self.assertIsNone(guard.post_tool_call())
        self.assertIsNone(guard.transform_tool_result(tool_name="terminal", args=None, result=None))
        self.assertIsNone(guard.transform_tool_result())
        self.assertIsNone(guard.pre_llm_call(session_id="s1", conversation_history=None))
        self.assertIsNone(guard.pre_llm_call(session_id=None, conversation_history=[{"x": 1}]))
        self.assertIsNone(guard.pre_llm_call(session_id="s1", conversation_history="oops"))
        self.assertIsNone(guard.pre_llm_call(session_id="s1", conversation_history=[None, 3, {"tool_calls": 7}]))
        self.assertEqual(memory.render(None), "")
        self.assertEqual(memory.forgotten("s1", None, None, None), [])

    def test_a_broken_checkpoint_engine_never_blocks_the_tool(self):
        with mock.patch.object(handlers, "create_checkpoint", side_effect=RuntimeError("disk on fire")):
            result = self.pre("write_file", {"path": str(self.ws / "app.py")}, turn_id="t9")
        self.assertIsNone(result)


class ResumableCaptureTest(FlowBase):
    def test_timeout_keeps_progress_and_later_attempts_finish(self):
        for i in range(5):
            write(self.ws / f"f{i}.txt", f"content {i}")
        self.configure(auto_budget_seconds=0.5)

        base = __import__("time").monotonic()
        state = {"advance": 0.0}

        class FakeTime:
            @staticmethod
            def monotonic():
                return base + state["advance"]

            @staticmethod
            def time():
                return __import__("time").time()

        real_put = snapshot._Store.put

        def slow_put(self_, source):
            state["advance"] += 0.3
            return real_put(self_, source)

        results = []
        with mock.patch.object(snapshot, "time", FakeTime), mock.patch.object(
            snapshot._Store, "put", slow_put
        ):
            for _ in range(6):
                state["advance"] = 0.0
                manifest = handlers.create_checkpoint("warm", {}, auto=True)
                results.append(manifest)
                if manifest:
                    break
        self.assertIsNone(results[0])
        self.assertIsNotNone(results[-1], "a checkpoint exists after enough attempts")
        self.assertGreater(len(results), 1)
        self.assertGreater(results[-1]["reused"], 0)
        journal = storage.read_jsonl(storage.journal_path())
        warming = [j for j in journal if j.get("reason") == "timeout_warming"]
        self.assertTrue(warming)
        self.assertGreater(warming[0]["files_done"], 0)
        self.assertFalse(handlers._seed_path(self.ws).exists(), "seed removed after success")
        self.assertEqual(results[-1]["files"], 7)

        # the checkpoint is complete and restorable
        write(self.ws / "f3.txt", "broken")
        out = ok(handlers.atm_rewind({"confirm": True}))
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.ws / "f3.txt").read_text(encoding="utf-8"), "content 3")


class SlashCommandTest(FlowBase):
    def setUp(self):
        super().setUp()
        make_repo(self.ws)

    def cmd(self, text, **kw):
        out = commands.handle(text, session_id="s1", platform="cli", **kw)
        self.assertIsInstance(out, str)
        self.assertLessEqual(len(out), 3500)
        return out

    def test_full_cycle(self):
        self.assertIn("Checkpoints: 0", self.cmd(""))
        created = ok(handlers.atm_checkpoint({"label": "base"}))["checkpoint"]["id"]
        status = self.cmd("status")
        self.assertIn(str(self.ws), status)
        self.assertIn("Checkpoints: 1", status)
        self.assertIn("clean", status)
        self.assertIn("gate: approve", status)

        listing = self.cmd("list")
        self.assertIn(created, listing)
        self.assertIn("base", listing)

        command = "git push origin main"
        self.pre("terminal", {"command": command}, tool_call_id="p1")
        self.post("terminal", {"command": command}, json.dumps({"exit_code": 0, "output": ""}), tool_call_id="p1")
        effects_text = self.cmd("effects")
        self.assertIn("git push -> origin/main", effects_text)
        self.assertIn("--force-with-lease", effects_text)
        self.assertIn("NOT undo", self.cmd("status"))

        write(self.ws / "app.py", "v2")
        write(self.ws / "extra.txt", "x")
        diff = self.cmd(f"diff {created}")
        self.assertIn("app.py", diff)
        self.assertIn("extra.txt", diff)

        preview = self.cmd(f"rewind {created}")
        self.assertIn("preview", preview.lower())
        self.assertIn("will NOT undo", preview)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v2")

        refused = self.cmd(f"rewind {created} --yes")
        self.assertIn("Error", refused)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v2")

        done = self.cmd(f"rewind {created} --yes --allow-irreversible")
        self.assertIn("Restored", done)
        self.assertIn("Still true outside", done)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertFalse((self.ws / "extra.txt").exists())

    def test_path_rewind_and_errors(self):
        created = ok(handlers.atm_checkpoint({"label": "base"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "v2")
        write(self.ws / "notes.txt", "n2")
        self.cmd(f"rewind {created} --yes app.py")
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertEqual((self.ws / "notes.txt").read_text(encoding="utf-8"), "n2")
        self.assertIn("Unknown subcommand", self.cmd("frobnicate"))
        self.assertIn("/timemachine", self.cmd("help"))
        self.assertIn("Error", self.cmd("diff ckpt_20200101T000000_deadbeef"))
        self.assertIn("Usage", self.cmd("diff"))

    def test_windows_path_filter_is_case_insensitive(self):
        with mock.patch.object(snapshot.os, "name", "nt"):
            match = snapshot._matcher(snapshot._norm_paths(["SRC/App.py", "Docs"]))
            self.assertTrue(match("src/app.py"))
            self.assertTrue(match("docs/readme.md"))
            self.assertFalse(match("docs2/x"))
        with mock.patch.object(snapshot.os, "name", "posix"):
            match = snapshot._matcher(["SRC/App.py"])
            self.assertFalse(match("src/app.py"))
            self.assertTrue(match("SRC/App.py"))

    def test_registration(self):
        ctx = mock.Mock()
        commands.register(ctx)
        name, handler = ctx.register_command.call_args.args[:2]
        self.assertEqual(name, "timemachine")
        self.assertIs(handler, commands.handle)


class RegistrationTest(unittest.TestCase):
    def test_register_wires_everything(self):
        class Ctx:
            def __init__(self):
                self.tools, self.hooks, self.commands = [], [], []

            def get_config(self, key, default=None):
                return {"irreversible_gate": "warn", "auto_budget_seconds": 99}.get(key, default)

            def register_tool(self, **kw):
                self.tools.append(kw["name"])
                assert kw["toolset"] == "time_machine"

            def register_hook(self, name, cb):
                self.hooks.append(name)

            def register_command(self, name, handler, description="", args_hint=""):
                self.commands.append(name)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ATM_DATA_DIR"] = tmp
            try:
                ctx = Ctx()
                pkg.register(ctx)
            finally:
                os.environ.pop("ATM_DATA_DIR", None)
        self.assertEqual(
            sorted(ctx.tools),
            ["atm_checkpoint", "atm_effects", "atm_fork", "atm_list", "atm_rewind"],
        )
        self.assertEqual(
            ctx.hooks,
            ["pre_tool_call", "post_tool_call", "transform_tool_result", "pre_llm_call", "post_compaction"],
        )
        self.assertEqual(ctx.commands, ["timemachine"])
        self.assertEqual(handlers.config()["irreversible_gate"], "warn")
        self.assertEqual(handlers.config()["auto_budget_seconds"], 20.0)


class ConfigTest(unittest.TestCase):
    def test_normalize_new_keys(self):
        norm = pkg.plugin_config.normalize(
            {
                "irreversible_gate": "WARN",
                "gate_allow_patterns": "git push origin dev",
                "auto_budget_seconds": "0.1",
                "max_file_mb": 0,
                "effect_memory": "false",
                "struggle_threshold": -3,
                "annotate_results": "no",
            }
        )
        self.assertEqual(norm["irreversible_gate"], "warn")
        self.assertEqual(norm["gate_allow_patterns"], ["git push origin dev"])
        self.assertEqual(norm["auto_budget_seconds"], 0.5)
        self.assertEqual(norm["max_file_mb"], 1)
        self.assertFalse(norm["effect_memory"])
        self.assertEqual(norm["struggle_threshold"], 0)
        self.assertFalse(norm["annotate_results"])
        defaults = pkg.plugin_config.normalize({})
        self.assertEqual(defaults["irreversible_gate"], "approve")
        self.assertEqual(defaults["auto_budget_seconds"], 5.0)
        self.assertEqual(defaults["struggle_threshold"], 4)

    def test_load_falls_back_when_get_config_takes_no_key(self):
        class Old:
            def get_config(self):
                return {}

        with mock.patch.object(pkg.plugin_config, "_from_config_file", return_value={"retention": 7}):
            self.assertEqual(pkg.plugin_config.load(Old())["retention"], 7)


if __name__ == "__main__":
    unittest.main()


class AdversarialFixesTest(FlowBase):
    def test_string_false_confirm_is_only_a_preview(self):
        ckpt = ok(handlers.atm_checkpoint({"label": "a"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "changed")
        out = ok(handlers.atm_rewind({"checkpoint_id": ckpt, "confirm": "false"}))
        self.assertTrue(out.get("preview"))
        self.assertEqual((self.ws / "app.py").read_text(), "changed")

    def test_ledger_with_invalid_utf8_still_reads_and_appends(self):
        handlers.append_effect({"kind": "x", "ts": "t"})
        with open(storage.effects_path(), "ab") as fh:
            fh.write(b'\xff\xfe garbage\n')
        self.assertEqual(len(handlers.read_ledger()), 1)
        storage._rotate(storage.effects_path(), 5)
        handlers.append_effect({"kind": "y", "ts": "t2"})
        self.assertEqual(len(handlers.read_ledger()), 2)

    def test_gc_skipped_when_a_manifest_is_unreadable(self):
        ckpt = ok(handlers.atm_checkpoint({"label": "a"}))["checkpoint"]["id"]
        objects = [p for p in (self.root / "data" / "objects").rglob("*") if p.is_file()]
        self.assertTrue(objects)
        manifest = snapshot.checkpoint_dir(ckpt) / "manifest.json"
        manifest.write_text("{ not json", encoding="utf-8")  # e.g. locked or half-written
        handlers._prune()
        self.assertTrue(all(p.exists() for p in objects))

    def test_auto_checkpoint_measure_is_time_bounded(self):
        self.configure(auto_budget_seconds=0.5)
        with mock.patch.object(snapshot, "measure", return_value=(-1, 0)):
            self.assertIsNone(handlers.create_checkpoint("auto", {}, auto=True))
        self.assertEqual(self.checkpoints(), [])

    def test_restore_over_read_only_file(self):
        ckpt = ok(handlers.atm_checkpoint({"label": "a"}))["checkpoint"]["id"]
        target = self.ws / "app.py"
        write(target, "changed")
        os.chmod(target, 0o444)
        out = ok(handlers.atm_rewind({"checkpoint_id": ckpt, "confirm": True}))
        self.assertEqual(out.get("errors"), [])
        self.assertEqual(target.read_text(), "v1")


class ModelRestoreApprovalTest(FlowBase):
    """A restore requested by the model must always reach the human."""

    def _broken(self):
        ckpt = ok(handlers.atm_checkpoint({"label": "good"}))["checkpoint"]["id"]
        write(self.ws / "app.py", "broken")
        write(self.ws / "notes.txt", "broken")
        write(self.ws / "junk.txt", "junk")
        return ckpt

    def test_confirm_true_asks_the_human_with_counts(self):
        ckpt = self._broken()
        out = self.pre("atm_rewind", {"checkpoint_id": ckpt, "confirm": True})
        self.assertEqual(out["action"], "approve")
        self.assertNotIn("rule_key", out)
        self.assertLessEqual(len(out["message"]), 700)
        self.assertIn(ckpt, out["message"])
        self.assertIn("2 modified", out["message"])
        self.assertIn("1 added", out["message"])
        self.assertIn("app.py", out["message"])
        # Nothing was restored by the hook itself.
        self.assertEqual((self.ws / "app.py").read_text(), "broken")

    def test_message_lists_effects_that_stay_true(self):
        ckpt = self._broken()
        handlers.append_effect({
            "kind": "git_push", "target": "origin/main", "reversible": False,
            "status": "succeeded", "ts": storage.now_iso(), "workspace": str(self.ws),
            "session_id": "s1", "compensation": "git push --force-with-lease origin x:main",
        })
        out = self.pre("atm_rewind", {"checkpoint_id": ckpt, "confirm": "yes"})
        self.assertIn("Still true outside", out["message"])
        self.assertIn("git push", out["message"])

    def test_preview_and_string_false_are_not_gated(self):
        ckpt = self._broken()
        self.assertIsNone(self.pre("atm_rewind", {"checkpoint_id": ckpt}))
        self.assertIsNone(self.pre("atm_rewind", {"checkpoint_id": ckpt, "confirm": "false"}))
        self.assertIsNone(self.pre("atm_rewind", {"checkpoint_id": ckpt, "confirm": False}))

    def test_other_atm_tools_stay_ungated(self):
        self.assertIsNone(self.pre("atm_fork", {"confirm": True}))
        self.assertIsNone(self.pre("atm_checkpoint", {}))

    def test_preview_failure_still_asks_the_human(self):
        ckpt = self._broken()
        with mock.patch.object(handlers, "preview_changes", side_effect=RuntimeError("boom")):
            out = self.pre("atm_rewind", {"checkpoint_id": ckpt, "confirm": True})
        self.assertEqual(out["action"], "approve")
        self.assertIn("Restore workspace", out["message"])

    def test_unknown_checkpoint_and_garbage_args_ask_generically(self):
        out = self.pre("atm_rewind", {"checkpoint_id": "nope", "confirm": True})
        self.assertEqual(out["action"], "approve")
        self.assertIsNone(self.pre("atm_rewind", "garbage"))
