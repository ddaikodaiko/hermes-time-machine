"""Tests for the effect ledger v2 (effects.py) and the point-of-no-return gate (gate.py).

Run with: python -m unittest discover -s tests -p "test_effects.py" -v
Fake git directories are built by writing files; git is never executed.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
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
effects = importlib.import_module("hermes_time_machine.effects")
gate = importlib.import_module("hermes_time_machine.gate")

LOCAL = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
REMOTE = "3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d"
OTHER = "1111111111111111111111111111111111111111"

CONFIG = """[core]
\trepositoryformatversion = 0
[remote "origin"]
\turl = https://alice:ghp_secret@github.com/o/r.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
\tremote = origin
\tmerge = refs/heads/main
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path, *, branch="main", local=LOCAL, remote=REMOTE, config=CONFIG, packed=False) -> Path:
    git = root / ".git"
    write(git / "HEAD", f"ref: refs/heads/{branch}\n")
    write(git / "config", config)
    if packed:
        write(git / "packed-refs", f"# pack-refs\n{local} refs/heads/{branch}\n{remote} refs/remotes/origin/{branch}\n^{OTHER}\n")
    else:
        write(git / f"refs/heads/{branch}", local + "\n")
        if remote:
            write(git / f"refs/remotes/origin/{branch}", remote + "\n")
    return root


def push_effect(command: str, workspace: Path) -> dict:
    found = [e for e in effects.preflight("terminal", {"command": command}, workspace) if e["kind"] == "git_push"]
    assert len(found) == 1, found
    return found[0]


class GitPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upstream_config(self):
        make_repo(self.ws)
        e = push_effect("git push", self.ws)
        self.assertFalse(e["reversible"])
        self.assertEqual(e["target"], "origin/main")
        ps = e["pre_state"]
        self.assertEqual((ps["remote"], ps["branch"], ps["local_sha"], ps["remote_sha_before"]), ("origin", "main", LOCAL, REMOTE))
        self.assertFalse(ps["force"])
        self.assertFalse(ps["new_branch"])
        self.assertEqual(
            e["compensation"],
            f"git push --force-with-lease=refs/heads/main:{LOCAL} origin {REMOTE}:refs/heads/main",
        )
        self.assertIn("the lease makes it fail", e["compensation_note"])
        for key in ("kind", "reason", "evidence", "target", "compensation", "compensation_note", "pre_state"):
            self.assertIn(key, e)

    def test_explicit_remote_and_branch(self):
        make_repo(self.ws)
        e = push_effect("git push origin main", self.ws)
        self.assertEqual(e["target"], "origin/main")
        self.assertEqual(e["pre_state"]["remote_sha_before"], REMOTE)

    def test_new_branch_set_upstream(self):
        make_repo(self.ws)
        write(self.ws / ".git/refs/heads/feature/x", OTHER + "\n")
        e = push_effect("git push -u origin feature/x", self.ws)
        self.assertTrue(e["pre_state"]["new_branch"])
        self.assertEqual(e["target"], "origin/feature/x")
        # Leased delete: only removes the branch if it still points at what we pushed.
        self.assertEqual(
            e["compensation"],
            f"git push --force-with-lease=refs/heads/feature/x:{OTHER} origin :refs/heads/feature/x",
        )
        self.assertIn("Confirm it is new before deleting it", e["compensation_note"])

    def test_redirections_are_not_refspecs(self):
        # Real command from a Hermes 0.23 + Claude Sonnet session: the agent appended 2>&1.
        make_repo(self.ws)
        expected = (
            f"git push --force-with-lease=refs/heads/main:{LOCAL} origin {REMOTE}:refs/heads/main"
        )
        for cmd in (
            "git push origin main 2>&1",
            "git push origin main > push.log 2>&1",
            "git push origin main &>/dev/null",
            "git push 2>&1 | tail -5",
            "git push origin main 1>>out.txt",
        ):
            e = push_effect(cmd, self.ws)
            self.assertEqual(e["target"], "origin/main", cmd)
            self.assertEqual(e["compensation"], expected, cmd)

    def test_force_head_refspec(self):
        make_repo(self.ws)
        e = push_effect("git push --force origin HEAD:main", self.ws)
        self.assertTrue(e["pre_state"]["force"])
        self.assertEqual(e["pre_state"]["local_sha"], LOCAL)
        self.assertIn("force push", e["compensation_note"])
        self.assertTrue(e["compensation"].startswith("git push --force-with-lease=refs/heads/main:"))
        e2 = push_effect("git push -f origin main", self.ws)
        self.assertTrue(e2["pre_state"]["force"])
        e3 = push_effect("git push origin +main", self.ws)
        self.assertTrue(e3["pre_state"]["force"])
        e4 = push_effect("git push --force-with-lease origin main", self.ws)
        self.assertTrue(e4["pre_state"]["force"])

    def test_delete_forms(self):
        make_repo(self.ws)
        write(self.ws / ".git/refs/remotes/origin/old", OTHER + "\n")
        for cmd in ("git push origin --delete old", "git push origin :old", "git push -d origin old"):
            e = push_effect(cmd, self.ws)
            self.assertTrue(e["pre_state"]["delete"], cmd)
            self.assertEqual(e["target"], "origin/old", cmd)
            self.assertEqual(e["compensation"], f"git push origin {OTHER}:refs/heads/old", cmd)

    def test_tags_all_mirror_have_no_compensation(self):
        make_repo(self.ws)
        for cmd, suffix in (("git push --tags", "(+tags)"), ("git push --all origin", "(all branches)"), ("git push --mirror", "(mirror)")):
            e = push_effect(cmd, self.ws)
            self.assertIsNone(e["compensation"], cmd)
            self.assertIn(suffix, e["target"], cmd)
            self.assertIn("git ls-remote origin", e["compensation_note"], cmd)

    def test_dry_run_is_no_effect(self):
        make_repo(self.ws)
        for cmd in ("git push --dry-run", "git push -n origin main"):
            self.assertEqual(effects.preflight("terminal", {"command": cmd}, self.ws), [], cmd)
            self.assertEqual(effects.classify("terminal", {"command": cmd}, self.ws), [], cmd)

    def test_cd_and_dash_c_find_subrepo(self):
        sub = self.ws / "sub"
        sub.mkdir()
        make_repo(sub)
        for cmd in ("cd sub && git push", "git -C sub push", "cd sub; git push origin main"):
            e = push_effect(cmd, self.ws)
            self.assertEqual(e["pre_state"]["local_sha"], LOCAL, cmd)
            self.assertEqual(Path(e["pre_state"]["repo"]).resolve(), sub.resolve(), cmd)

    def test_packed_refs_only(self):
        make_repo(self.ws, packed=True)
        e = push_effect("git push", self.ws)
        self.assertEqual(e["pre_state"]["local_sha"], LOCAL)
        self.assertEqual(e["pre_state"]["remote_sha_before"], REMOTE)
        self.assertIn(REMOTE, e["compensation"])

    def test_worktree_gitdir_file_and_commondir(self):
        main = self.ws / "main-repo"
        make_repo(main)
        wt_git = main / ".git" / "worktrees" / "wt"
        write(wt_git / "HEAD", "ref: refs/heads/main\n")
        write(wt_git / "commondir", "../..\n")
        wt = self.ws / "wt"
        write(wt / ".git", f"gitdir: {wt_git}\n")
        e = push_effect("git push", wt)
        self.assertEqual(e["pre_state"]["local_sha"], LOCAL)
        self.assertEqual(e["pre_state"]["remote_sha_before"], REMOTE)
        self.assertEqual(e["pre_state"]["remote_url"], "https://github.com/o/r.git")

    def test_credentials_stripped(self):
        make_repo(self.ws)
        e = push_effect("git push", self.ws)
        self.assertEqual(e["pre_state"]["remote_url"], "https://github.com/o/r.git")
        self.assertNotIn("ghp_secret", json.dumps(e))

    def test_default_remote_origin_without_config(self):
        make_repo(self.ws, config="[core]\n")
        e = push_effect("git push", self.ws)
        self.assertEqual(e["pre_state"]["remote"], "origin")
        self.assertEqual(e["target"], "origin/main")

    def test_missing_git_dir_is_safe(self):
        e = push_effect("git push origin main", self.ws)
        self.assertFalse(e["reversible"])
        self.assertIsNone(e["compensation"])
        self.assertTrue(e["compensation_note"])

    def test_classify_backward_compatible(self):
        make_repo(self.ws)
        out = effects.classify("terminal", {"command": "git push origin main"}, self.ws)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["reversible"])
        self.assertEqual(out[0]["kind"], "git_push")
        out = effects.classify("terminal", {"command": "rm -rf build"}, self.ws)
        self.assertEqual([(e["kind"], e["reversible"]) for e in out], [("workspace_delete", True)])


class OtherKindsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_npm_publish(self):
        write(self.ws / "package.json", json.dumps({"name": "@me/pkg", "version": "1.2.3"}))
        [e] = effects.preflight("terminal", {"command": "npm publish --access public"}, self.ws)
        self.assertEqual(e["kind"], "npm_publish")
        self.assertEqual(e["target"], "@me/pkg@1.2.3")
        self.assertEqual(e["compensation"], "npm unpublish @me/pkg@1.2.3")
        self.assertIn("72 hours", e["compensation_note"])
        self.assertIn("npm deprecate @me/pkg@1.2.3", e["compensation_note"])

    def test_npm_publish_dir_argument(self):
        write(self.ws / "lib" / "package.json", json.dumps({"name": "lib", "version": "0.0.1"}))
        [e] = effects.preflight("terminal", {"command": "npm publish lib"}, self.ws)
        self.assertEqual(e["target"], "lib@0.0.1")

    def test_finalize_pr_url(self):
        [e] = effects.preflight("terminal", {"command": 'gh pr create --title "x" --body y'}, self.ws)
        self.assertEqual(e["kind"], "github_pr")
        self.assertIsNone(e["compensation"])
        result = json.dumps({"output": "Creating pull request\nhttps://github.com/octo/repo/pull/42\n", "exit_code": 0, "error": None})
        done = effects.finalize(e, result, self.ws)
        self.assertEqual(done["target"], "octo/repo#42")
        self.assertEqual(done["compensation"], "gh pr close https://github.com/octo/repo/pull/42")
        self.assertEqual(done["outcome"]["status"], "succeeded")
        self.assertIsNone(e["compensation"])  # original untouched

    def test_release_docker_kubectl_terraform(self):
        [r] = effects.preflight("terminal", {"command": 'gh release create v1.0.0 --title "One"'}, self.ws)
        self.assertEqual(r["compensation"], "gh release delete v1.0.0 --cleanup-tag --yes")
        [d] = effects.preflight("terminal", {"command": "docker push ghcr.io/o/app:1.0"}, self.ws)
        self.assertEqual(d["target"], "ghcr.io/o/app:1.0")
        self.assertIsNone(d["compensation"])
        self.assertIn("ghcr.io/o/app:1.0", d["compensation_note"])
        [k] = effects.preflight("terminal", {"command": "kubectl apply -f deploy.yaml"}, self.ws)
        self.assertEqual(k["compensation"], "kubectl delete -f deploy.yaml")
        self.assertIn("rollout undo", k["compensation_note"])
        [k2] = effects.preflight("terminal", {"command": "kubectl delete -f deploy.yaml"}, self.ws)
        self.assertEqual(k2["compensation"], "kubectl apply -f deploy.yaml")
        [t] = effects.preflight("terminal", {"command": "terraform apply -auto-approve"}, self.ws)
        self.assertIsNone(t["compensation"])
        self.assertIn("terraform.tfstate.backup", t["compensation_note"])
        [db] = effects.preflight("terminal", {"command": 'psql -c "drop table users"'}, self.ws)
        self.assertIsNone(db["compensation"])
        self.assertTrue(db["compensation_note"])

    def test_inside_write_with_unresolved_workspace_is_not_outside(self):
        # On Windows the temp dir is often an 8.3 short path (JOSEC~1.DES) while tools pass
        # the long form; both sides must be resolved before comparing.
        inside_long = str(self.ws.resolve() / "src" / "app.py")
        for workspace in (self.ws, self.ws.resolve()):
            out = effects.preflight("write_file", {"path": inside_long}, workspace)
            self.assertEqual(out, [], str(workspace))

    def test_outside_workspace_write(self):
        outside = str(Path(self.tmp.name).parent / "elsewhere" / "f.txt")
        out = effects.preflight("write_file", {"path": outside}, self.ws.resolve())
        self.assertEqual([e["kind"] for e in out], ["outside_workspace_write"])
        self.assertEqual(out[0]["target"], outside)
        self.assertTrue(out[0]["compensation_note"])

    def test_split_commands(self):
        self.assertEqual(effects.split_commands("cd x && git push || echo no; ls | wc\nfoo"),
                         ["cd x", "git push", "echo no", "ls", "wc", "foo"])
        self.assertEqual(effects.split_commands('echo "a && b" && git push'), ['echo "a && b"', "git push"])
        self.assertEqual(effects.split_commands(None), [])


class OutcomeTest(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(effects.outcome("terminal", json.dumps({"output": "ok", "exit_code": 0, "error": None}))["status"], "succeeded")
        res = effects.outcome("terminal", json.dumps({"output": "remote: denied", "exit_code": 1, "error": None}))
        self.assertEqual((res["status"], res["exit_code"]), ("failed", 1))
        self.assertIn("denied", res["detail"])
        res = effects.outcome("terminal", json.dumps({"returncode": 0}))
        self.assertEqual((res["status"], res["exit_code"]), ("succeeded", 0))
        self.assertEqual(effects.outcome("terminal", json.dumps({"exit_status": 2}))["status"], "failed")
        self.assertEqual(effects.outcome("terminal", {"code": 0})["status"], "succeeded")

    def test_error_field_and_status(self):
        res = effects.outcome("terminal", json.dumps({"error": "x"}))
        self.assertEqual(res["status"], "failed")
        self.assertEqual(effects.outcome("terminal", json.dumps({"status": "error"}))["status"], "failed")
        self.assertEqual(effects.outcome("terminal", json.dumps({"output": "hi", "exit_code": None, "error": None}))["status"], "unknown")

    def test_plain_strings_and_garbage(self):
        self.assertEqual(effects.outcome("terminal", "fatal: could not read Username")["status"], "failed")
        self.assertEqual(effects.outcome("terminal", "everything went fine")["status"], "unknown")
        for garbage in (None, b"\xff\xfe", {}, [], 42, "{not json", "", object()):
            res = effects.outcome("terminal", garbage)
            self.assertIn(res["status"], ("succeeded", "failed", "unknown"))
            self.assertEqual(set(res), {"status", "exit_code", "detail"})
        self.assertEqual(effects.outcome("terminal", None)["status"], "unknown")
        self.assertEqual(effects.outcome("terminal", "{not json")["status"], "unknown")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        make_repo(self.ws)
        self.effects = effects.preflight("terminal", {"command": "git push"}, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_default(self):
        d = gate.decide(self.effects, {}, checkpoint_id="ckpt_20261107T181500_1a2b3c4d", command="git push")
        self.assertEqual(d["action"], "approve")
        self.assertEqual(d["rule_key"], "hermes-time-machine:git_push:origin/main")
        msg = d["message"]
        self.assertLessEqual(len(msg), 700)
        self.assertIn("Point of no return (hermes-time-machine)", msg)
        self.assertIn("origin/main", msg)
        self.assertIn("3a4b5c6..9f8e7d6", msg)
        self.assertIn("git push --force-with-lease=refs/heads/main:" + LOCAL, msg)
        self.assertIn("ckpt_20261107T181500_1a2b3c4d", msg)

    def test_no_checkpoint_sentence(self):
        d = gate.decide(self.effects, {"irreversible_gate": "approve"}, checkpoint_id=None)
        self.assertIn("No checkpoint covers the local files for this call.", d["message"])

    def test_unknown_mode_is_approve_warn_off_none(self):
        self.assertEqual(gate.decide(self.effects, {"irreversible_gate": "banana"}, checkpoint_id=None)["action"], "approve")
        self.assertIsNone(gate.decide(self.effects, {"irreversible_gate": "warn"}, checkpoint_id=None))
        self.assertIsNone(gate.decide(self.effects, {"irreversible_gate": "off"}, checkpoint_id=None))

    def test_allow_patterns(self):
        cfg = {"gate_allow_patterns": ["([unclosed", r"^git push origin main$"]}
        self.assertIsNone(gate.decide(self.effects, cfg, checkpoint_id=None, command="GIT PUSH origin main"))
        self.assertIsNotNone(gate.decide(self.effects, cfg, checkpoint_id=None, command="git push --force"))

    def test_reversible_only_or_empty(self):
        rev = effects.classify("terminal", {"command": "rm -rf build"}, self.ws)
        self.assertIsNone(gate.decide(rev, {}, checkpoint_id=None))
        self.assertIsNone(gate.decide([], {}, checkpoint_id=None))
        self.assertIsNone(gate.decide(None, {}, checkpoint_id=None))

    def test_message_length_with_many_effects(self):
        many = [dict(self.effects[0], target="origin/" + "very-long-branch-name-" * 5 + str(i)) for i in range(6)]
        msg = gate.build_message(many, "ckpt_x")
        self.assertLessEqual(len(msg), 700)
        self.assertIn("ckpt_x", msg)

    def test_rule_key_sanitised(self):
        key = gate.rule_key({"kind": "Git Push", "target": "Origin/Main branch*" + "x" * 200})
        self.assertLessEqual(len(key), 120)
        self.assertRegex(key, r"^[a-z0-9:/._@#+-]+$")
        self.assertEqual(gate.rule_key({"kind": "npm_publish", "target": None}), "hermes-time-machine:npm_publish:any")


class RobustnessTest(unittest.TestCase):
    def test_preflight_never_raises(self):
        weird = [
            ("terminal", {}, None),
            ("terminal", {"command": None}, None),
            ("terminal", {"command": 123}, Path(".")),
            ("terminal", {"command": ["git", "push"]}, None),
            ("terminal", None, None),
            ("terminal", {"command": "git push"}, None),
            ("terminal", {"command": "git push 'unterminated"}, Path("/definitely/not/here")),
            ("terminal", {"command": "git -C"}, Path("/definitely/not/here")),
            (None, {"command": "npm publish"}, Path("/definitely/not/here")),
            ("write_file", {"path": 5}, None),
            ("terminal", {"command": "cd && git push && cd .. && git push -u"}, None),
        ]
        for tool, args, ws in weird:
            self.assertIsInstance(effects.preflight(tool, args, ws), list)
            self.assertIsInstance(effects.classify(tool, args, ws), list)

    def test_finalize_never_raises(self):
        for eff in ({}, {"kind": "github_pr"}, None, {"kind": "github_pr", "pre_state": None}):
            for res in (None, b"\xff", {"x": 1}, "garbage"):
                self.assertIsInstance(effects.finalize(eff, res, None), dict)

    def test_fast_on_normal_repo(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            ws = make_repo(Path(tmp))
            start = time.perf_counter()
            for _ in range(20):
                effects.preflight("terminal", {"command": "cd . && git push origin main"}, ws)
            self.assertLess((time.perf_counter() - start) / 20, 0.05)


if __name__ == "__main__":
    unittest.main()


class ChainedCommitPushTest(unittest.TestCase):
    """`git add -A && git commit -m x && git push origin main`: HEAD == origin/main at preflight time."""

    NEW = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    CMD = 'git add -A && git commit -m "feat: x" && git push origin main'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_repo(self.root, local=REMOTE, remote=REMOTE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preflight_does_not_claim_already_pointed(self):
        eff = push_effect(self.CMD, self.root)
        pre = eff["pre_state"]
        self.assertTrue(pre["pending_commits"])
        self.assertIsNone(pre["local_sha"])
        self.assertNotIn("already pointed", eff["compensation_note"])
        self.assertEqual(
            eff["compensation"],
            f"git push --force-with-lease=refs/heads/main origin {REMOTE}:refs/heads/main",
        )
        msg = gate.build_message([eff], "ckpt_20261107T181319_8193be50")
        self.assertIn("creates on top of", msg)
        self.assertIn("git push --force-with-lease", msg)
        self.assertNotIn("already pointed", msg)

    def test_plain_push_unchanged(self):
        eff = push_effect("git push origin main", self.root)
        self.assertFalse(eff["pre_state"].get("pending_commits"))
        self.assertIn("already pointed", eff["compensation_note"])

    def test_finalize_fills_exact_sha(self):
        eff = push_effect(self.CMD, self.root)
        write(self.root / ".git/refs/remotes/origin/main", self.NEW + "\n")  # git updates it on push
        final = effects.finalize(eff, {"exit_code": 0, "output": "ok"}, self.root)
        self.assertEqual(final["pre_state"]["pushed_sha"], self.NEW)
        self.assertEqual(
            final["compensation"],
            f"git push --force-with-lease=refs/heads/main:{self.NEW} origin {REMOTE}:refs/heads/main",
        )

    def test_finalize_failed_push_keeps_preflight(self):
        eff = push_effect(self.CMD, self.root)
        write(self.root / ".git/refs/remotes/origin/main", self.NEW + "\n")
        final = effects.finalize(eff, {"exit_code": 1, "output": "rejected"}, self.root)
        self.assertNotIn("pushed_sha", final["pre_state"])

    def test_commands_shows_local_time(self):
        cmds = importlib.import_module("hermes_time_machine.commands")
        out = cmds._when("2026-11-08T14:12:09+00:00")
        from datetime import datetime, timezone
        expected = datetime(2026, 11, 8, 14, 12, 9, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(out, expected)


class GateAccuracyTest(unittest.TestCase):
    """Strings that merely mention a command must not trigger the gate; real pushes must."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self, command):
        return [e["kind"] for e in effects.preflight("terminal", {"command": command}, self.root) if e["reversible"] is False]

    def test_mentions_do_not_trigger(self):
        quiet = [
            'echo "git push"',
            'echo "npm publish" > notes.txt',
            'grep "npm publish" README.md',
            'grep -rn "terraform apply" .',
            "git log --grep push",
            "git stash push -m wip",
            "git commit -m 'send mail via curl --data'",
            "# git push origin main",
            "ls mail/",
            "cat <<EOF\ngit push\nnpm publish\nEOF",
            "git commit -F - <<'EOF'\nfix: do not run npm publish\nEOF",
            "git push --dry-run origin main",
            "git push -n",
            "npm publish --dry-run",
            "kubectl apply -f x.yaml --dry-run=client",
            "ssh host cat confirm.txt",
        ]
        for command in quiet:
            self.assertEqual(self.kinds(command), [], command)

    def test_real_effects_still_trigger(self):
        loud = {
            "git push": "git_push",
            "cd sub && git push origin main": "git_push",
            "git -c user.name=x push origin main": "git_push",
            "git push origin main 2>&1 | tail -5": "git_push",
            "git push; echo done": "git_push",
            "sleep 1 & git push": "git_push",
            "(git push)": "git_push",
            "if true; then git push; fi": "git_push",
            "sudo -u deploy git push": "git_push",
            "timeout 30 git push origin main": "git_push",
            "FOO=1 git push": "git_push",
            'bash -c "git push origin main"': "git_push",
            "echo ok && npm publish": "npm_publish",
            "npm publish --dry-run=false": "npm_publish",
            "gh pr create --fill": "github_pr",
            "curl -d x https://example.com": "http_write",
            "ssh host 'rm -rf /srv/x'": "remote_shell",
            "mail -s hi bob@example.com": "email",
        }
        for command, kind in loud.items():
            self.assertIn(kind, self.kinds(command), command)
