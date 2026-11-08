"""Recordable demo for hermes-time-machine v0.2. No Hermes install and no model needed.

It replays the story of a real session by calling the plugin's own hook
callbacks and tools, in the order Hermes calls them:

  1. write_file         -> automatic checkpoint, annotation on the tool result
  2. git push           -> "Point of no return" approval directive (nothing is approved here)
  3. push succeeded     -> the effect ledger records it with its pre-state
  4. /rollback          -> simulated: the chat history no longer contains the push
  5. next model call    -> effect memory reminds the model that the push happened
  6. atm_rewind         -> preview lists what is "still true outside", then restore
  7. atm_fork           -> try an alternative without touching the current work

The "git repository" and the "remote" are plain files in a temp folder: no git
binary, no subprocess, no network. Nothing leaves the machine.

    python demo/run_demo.py
    python demo/run_demo.py --pause   # wait for Enter between steps, for recording
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SESSION = "demo-session"
OLD_SHA = "1f4e9a2b" * 5  # what origin/main points at before the push
NEW_SHA = "7c3d5e8f" * 5  # the commit the agent pushes


def load_package():
    spec = importlib.util.spec_from_file_location(
        "hermes_time_machine",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_time_machine"] = module
    spec.loader.exec_module(module)
    return module


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 64 - len(title)))
    if "--pause" in sys.argv:
        try:
            input("  [Enter] ")
        except EOFError:
            pass


def show(label: str, value) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False)
    print(f"{label}:")
    for row in value.splitlines():
        print(f"  {row}")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(tempfile.mkdtemp(prefix="atm-demo-")).resolve()
    workspace = root / "demo-app"
    remote = root / "remote.git"  # stands in for origin; lives OUTSIDE the workspace

    # A fake git repo and a fake bare remote, written as files (this is all the
    # plugin ever reads: .git/HEAD, .git/config and refs).
    write(workspace / "app.py", "def handler(request):\n    return {'ok': True}\n")
    write(workspace / ".git" / "HEAD", "ref: refs/heads/main\n")
    write(
        workspace / ".git" / "config",
        f'[remote "origin"]\n\turl = {remote}\n[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
    )
    write(workspace / ".git" / "refs" / "heads" / "main", OLD_SHA + "\n")
    write(workspace / ".git" / "refs" / "remotes" / "origin" / "main", OLD_SHA + "\n")
    write(remote / "refs" / "heads" / "main", OLD_SHA + "\n")

    os.environ["ATM_DATA_DIR"] = str(root / "data")
    pkg = load_package()
    pkg.handlers.configure(pkg.plugin_config.normalize({"workspace": str(workspace)}))
    pkg.guard._reset_for_tests()

    cwd = os.getcwd()
    os.chdir(workspace)  # Hermes runs the agent's commands in the workspace
    try:
        return run(pkg, root, workspace, remote)
    finally:
        os.chdir(cwd)


def run(pkg, root: Path, workspace: Path, remote: Path) -> int:
    guard, handlers, memory = pkg.guard, pkg.handlers, pkg.memory

    step("1. Healthy project, local bare remote as origin")
    print(f"workspace : {workspace}")
    print(f"remote    : {remote}   (a folder; nothing leaves this machine)")
    print(f"origin/main is at {OLD_SHA[:7]}")
    print("app.py    :", read(workspace / "app.py").splitlines()[1].strip())

    step("2. write_file: pre_tool_call takes the restore point, transform_tool_result tells the model")
    call_1 = dict(session_id=SESSION, turn_id="turn-1", tool_call_id="call_1")
    args_1 = {"path": str(workspace / "app.py"), "content": "def handler(request):\n    return {'ok': True, 'v': 2}\n"}
    verdict = guard.pre_tool_call(tool_name="write_file", args=args_1, **call_1)
    print("pre_tool_call returned:", verdict, "(None = let the call through)")
    write(workspace / "app.py", args_1["content"])
    guard.post_tool_call(tool_name="write_file", args=args_1, result='{"ok": true}', status="ok", **call_1)
    annotated = guard.transform_tool_result(tool_name="write_file", args=args_1, result='{"ok": true}', **call_1)
    show("tool result the model sees", json.loads(annotated))
    first = json.loads(handlers.atm_list({}))["checkpoints"][-1]
    print(f"checkpoint {first['id']}: {first['files']} files, {first['duration_ms']} ms, trigger={first['trigger'].get('type')}")

    step("3. The agent commits and runs `git push origin main`: point of no return")
    write(workspace / ".git" / "refs" / "heads" / "main", NEW_SHA + "\n")  # the commit
    push = dict(session_id=SESSION, turn_id="turn-1", tool_call_id="call_2")
    push_args = {"command": "git push origin main"}
    directive = guard.pre_tool_call(tool_name="terminal", args=push_args, **push)
    show("pre_tool_call returned", {k: directive[k] for k in ("action", "rule_key")} if directive else directive)
    print()
    print((directive or {}).get("message", "(no directive)"))
    print("\nHermes now shows its own approval prompt with that text. The plugin never approves anything.")

    step("4. The human approves; the push runs; post_tool_call records it in the ledger")
    write(remote / "refs" / "heads" / "main", NEW_SHA + "\n")  # what the real push would do
    write(workspace / ".git" / "refs" / "remotes" / "origin" / "main", NEW_SHA + "\n")
    result = json.dumps({"output": f"To {remote}\n   {OLD_SHA[:7]}..{NEW_SHA[:7]}  main -> main", "exit_code": 0})
    guard.post_tool_call(tool_name="terminal", args=push_args, result=result, status="ok", **push)
    print(f"origin/main is now at {read(remote / 'refs' / 'heads' / 'main')[:7]}")
    show("/timemachine effects", pkg.commands.handle("effects", session_id=SESSION))

    step("5. Before /rollback the model still sees its push: no reminder needed")
    full_history = [
        {"role": "user", "content": "Refactor app.py and push to origin/main."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps(args_1)}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2", "type": "function", "function": {"name": "terminal", "arguments": json.dumps(push_args)}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": result},
    ]
    print("pre_llm_call returned:", guard.pre_llm_call(session_id=SESSION, conversation_history=full_history))

    step("6. Simulated /rollback: files and the last chat turn are gone. The remote is not.")
    rolled_back = [
        {"role": "user", "content": "Refactor app.py and push to origin/main."},
        {"role": "assistant", "content": "On it."},
        {"role": "user", "content": "Did you push anything?"},
    ]
    print(f"history now has {len(rolled_back)} messages and no trace of the push;")
    print(f"origin/main is still at {read(remote / 'refs' / 'heads' / 'main')[:7]}")

    step("7. Next model call: pre_llm_call injects the effect memory")
    injected = guard.pre_llm_call(session_id=SESSION, conversation_history=rolled_back)
    print((injected or {}).get("context", "(nothing injected)"))
    again = guard.pre_llm_call(session_id=SESSION, conversation_history=rolled_back)
    print("\nasked again on the next call:", again, "(reminded once, not on every turn)")

    step("8. atm_rewind preview: what will change, and what it can NOT undo")
    preview = json.loads(handlers.atm_rewind({"checkpoint_id": first["id"]}, session_id=SESSION))
    show("changes", preview["changes"]["counts"])
    show("still true outside after a restore", [
        {"kind": e["kind"], "target": e["target"], "undo": e["compensation"]} for e in preview["irreversible"]])
    print("hint:", preview["hint"])
    refused = json.loads(handlers.atm_rewind({"checkpoint_id": first["id"], "confirm": True}, session_id=SESSION))
    print("confirm=true alone  ->", refused["error"])

    step("9. Restore, acknowledging the push")
    done = json.loads(handlers.atm_rewind(
        {"checkpoint_id": first["id"], "confirm": True, "allow_irreversible": True}, session_id=SESSION))
    print(f"restored {done['restored']} file(s), removed {done['removed']}; previous contents kept in:\n  {done['previous_contents']}")
    print("app.py   :", read(workspace / "app.py").splitlines()[1].strip())
    print(f"local main is back at {read(workspace / '.git' / 'refs' / 'heads' / 'main')[:7]}")
    print(f"origin/main (outside) is still at {read(remote / 'refs' / 'heads' / 'main')[:7]}")
    print("note:", done.get("note"))

    step("10. Fork instead of overwrite")
    forked = json.loads(handlers.atm_fork({"checkpoint_id": first["id"], "name": "alternative"}))
    print("fork path :", forked["path"])
    print("fork app.py:", read(Path(forked["path"]) / "app.py").splitlines()[1].strip())

    # Self-check, so a regression makes the demo fail loudly.
    assert directive and directive["action"] == "approve", "the push should have produced an approve directive"
    assert injected and "git push" in injected["context"], "effect memory should mention the push"
    assert again is None, "effect memory must not repeat"
    assert read(remote / "refs" / "heads" / "main") == NEW_SHA, "the remote must be untouched by a rewind"
    assert "v" not in read(workspace / "app.py"), "app.py should be restored"

    print("\nDemo complete. Data:", pkg.storage.data_dir())
    shutil.rmtree(forked["path"], ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
