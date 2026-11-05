"""Recordable demo for hermes-time-machine.

Creates a throwaway workspace, checkpoints it, simulates an agent breaking it,
then previews and performs a rewind. No Hermes install required.

    python demo/run_demo.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def line(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 58 - len(title)))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="atm-demo-"))
    workspace = root / "demo-app"
    workspace.mkdir()
    (workspace / "app.py").write_text(
        "def handler(request):\n    return {'ok': True}\n", encoding="utf-8"
    )
    (workspace / "schema.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )

    os.environ["ATM_DATA_DIR"] = str(root / "data")
    pkg = load_package()
    pkg.handlers.configure(
        {
            "workspace": str(workspace),
            "auto_checkpoint": True,
            "risk_threshold": 0.6,
            "max_snapshot_mb": 50,
            "retention": 5,
        }
    )

    line("1. Baseline")
    print(f"workspace: {workspace}")
    print("app.py:", (workspace / "app.py").read_text(encoding="utf-8").splitlines()[0])

    line("2. The agent starts editing: automatic checkpoint")
    pkg.guard.pre_tool_call(
        tool_name="write_file",
        args={"path": str(workspace / "app.py")},
        session_id="demo",
        turn_id="turn-1",
    )
    checkpoints = json.loads(pkg.handlers.atm_list({}))
    print(json.dumps(checkpoints, indent=2))

    line("3. The agent breaks the workspace")
    (workspace / "app.py").write_text("def handler(request):\n    raise SystemExit(1)\n", encoding="utf-8")
    (workspace / "schema.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
    (workspace / "debug.log").write_text("boom\n", encoding="utf-8")
    print("app.py:", (workspace / "app.py").read_text(encoding="utf-8").splitlines()[1])
    print("extra file: debug.log")

    line("4. The agent also pushed to a remote")
    pkg.guard.post_tool_call(
        tool_name="terminal",
        args={"command": "git push origin main --force"},
        status="ok",
        session_id="demo",
    )
    effects = json.loads(pkg.handlers.atm_effects({}))
    print(json.dumps(effects, indent=2))

    line("5. Rewind preview (refuses to hide the push)")
    preview = json.loads(pkg.handlers.atm_rewind({"confirm": False}))
    print(json.dumps(preview, indent=2))

    line("6. Acknowledge and restore")
    started = time.perf_counter()
    restored = json.loads(
        pkg.handlers.atm_rewind({"confirm": True, "allow_irreversible": True})
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(json.dumps(restored, indent=2))
    print(f"restore took {elapsed:.0f} ms")

    line("7. Verify")
    print("app.py:", (workspace / "app.py").read_text(encoding="utf-8").splitlines()[1])
    print("schema.sql:", (workspace / "schema.sql").read_text(encoding="utf-8").strip())
    print("debug.log exists:", (workspace / "debug.log").exists())

    line("8. Fork instead of rewind")
    forked = json.loads(pkg.handlers.atm_fork({"name": "alternative"}))
    print(json.dumps(forked, indent=2))
    print("fork app.py:", (Path(forked["path"]) / "app.py").read_text(encoding="utf-8").splitlines()[1])

    print("\nDemo complete. Data:", pkg.storage.data_dir())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
