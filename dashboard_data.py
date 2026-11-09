"""Read-only data for the web dashboard tab (``dashboard/plugin_api.py``).

Everything here reads files under ``plugin-data/hermes-time-machine`` and the protected
workspaces. Nothing writes, restores or runs a process: the dashboard shows the point of
no return, the human still restores with ``/timemachine``.

Never raises: every public function returns a dict with an ``error`` key on failure.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from . import config as plugin_config
from . import handlers, snapshot, storage

MAX_CHECKPOINTS = 80
MAX_EFFECTS = 300
MAX_DIFF_BYTES = 400_000
MAX_DIFF_LINES = 600

_EFFECT_KEYS = (
    "ts", "tool", "kind", "reversible", "status", "target", "reason", "evidence",
    "compensation", "compensation_note", "checkpoint_id", "session_id", "tool_call_id",
    "workspace", "source", "exit_code",
)


def _reminded_keys() -> set[str]:
    try:
        data = json.loads((storage.data_dir() / "reminded.json").read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    if isinstance(data, dict):
        for keys in data.values():
            if isinstance(keys, list):
                out.update(str(k) for k in keys)
    return out


def _effect(record: dict, reminded: set[str]) -> dict:
    view = {key: record.get(key) for key in _EFFECT_KEYS if key in record}
    view["permanent"] = handlers.is_irreversible(record)
    view["via"] = "tool" if record.get("source") == "tool" else "command"
    view["reminded"] = f"{record.get('ts')}|{record.get('tool_call_id')}" in reminded
    return view


def _checkpoint(manifest: dict) -> dict:
    view = handlers.public(manifest)
    trigger = manifest.get("trigger") if isinstance(manifest.get("trigger"), dict) else {}
    view["trigger_type"] = trigger.get("type")
    view["tool"] = trigger.get("tool")
    view["session_id"] = trigger.get("session_id")
    return view


def overview(workspace: str | None = None) -> dict:
    """Checkpoints and effects of one workspace (the most recently used one by default)."""
    try:
        manifests = handlers.list_manifests(True)
        records = [r for r in handlers.read_ledger() if isinstance(r, dict)]
        workspaces: list[str] = []
        for item in [*(m.get("workspace") for m in manifests), *(r.get("workspace") for r in reversed(records))]:
            if isinstance(item, str) and item and item not in workspaces:
                workspaces.append(item)
        latest = {}
        for m in manifests:
            ws = m.get("workspace")
            latest[ws] = max(latest.get(ws, ""), str(m.get("created_at") or ""))
        for r in records:
            ws = r.get("workspace")
            latest[ws] = max(latest.get(ws, ""), str(r.get("ts") or ""))
        workspaces.sort(key=lambda ws: latest.get(ws, ""), reverse=True)
        current = workspace if workspace in workspaces else (workspaces[0] if workspaces else None)

        reminded = _reminded_keys()
        cps = [_checkpoint(m) for m in manifests if m.get("workspace") == current][:MAX_CHECKPOINTS]
        effects = [
            _effect(r, reminded)
            for r in records
            if r.get("workspace") in (current, None)
        ][-MAX_EFFECTS:]
        cps.sort(key=lambda c: str(c.get("created_at") or ""))
        permanent = [e for e in effects if e["permanent"]]
        return {
            "workspace": current,
            "workspaces": workspaces,
            "checkpoints": cps,
            "effects": effects,
            "summary": {
                "checkpoints": len(cps),
                "permanent": len(permanent),
                "failed": sum(1 for e in effects if e.get("status") == "failed"),
                "reminded": sum(1 for e in permanent if e["reminded"]),
                "via_tools": sum(1 for e in permanent if e["via"] == "tool"),
            },
        }
    except Exception as exc:
        return {"error": f"Could not read the time machine data: {exc}"[:300]}


def _workspace_of(manifest: dict) -> Path:
    target = Path(str(manifest.get("workspace") or "")).resolve()
    snapshot.assert_safe(target)
    if not target.is_dir():
        raise snapshot.SnapshotError(f"The workspace {target} no longer exists.")
    return target


def _excludes(manifest: dict) -> set[str]:
    return set(manifest.get("excludes") or snapshot.excludes_for(plugin_config.normalize({})))


def checkpoint(checkpoint_id: str) -> dict:
    """What restoring *checkpoint_id* would change in files, and what it cannot change."""
    try:
        if not storage.is_valid_id(checkpoint_id):
            return {"error": "Unknown checkpoint."}
        manifest = handlers.get_checkpoint(checkpoint_id)
        if not manifest:
            return {"error": "Unknown checkpoint."}
        out = {"checkpoint": _checkpoint(manifest), "stays_true": handlers.irreversible_since(manifest)}
        try:
            target = _workspace_of(manifest)
            diff = snapshot.diff(target, manifest["tree"], _excludes(manifest))
            out["files"] = {
                key: list(diff.get(key) or [])[:200] for key in ("modified", "deleted", "added")
            }
            out["counts"] = {key: len(diff.get(key) or []) for key in ("modified", "deleted", "added")}
            out["counts"]["unchanged"] = diff.get("unchanged", 0)
        except Exception as exc:
            out["files_error"] = str(exc)[:300]
        return out
    except Exception as exc:
        return {"error": f"Could not read the checkpoint: {exc}"[:300]}


def _read_text(path: Path | None) -> tuple[list[str] | None, str | None]:
    if path is None or not path.is_file():
        return [], None
    if path.stat().st_size > MAX_DIFF_BYTES:
        return None, "File too large to diff here."
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None, "Binary file."
    return data.decode("utf-8", errors="replace").splitlines(keepends=True), None


def file_diff(checkpoint_id: str, rel: str) -> dict:
    """Unified diff of one file: checkpoint version -> current workspace version."""
    try:
        if not storage.is_valid_id(checkpoint_id):
            return {"error": "Unknown checkpoint."}
        manifest = handlers.get_checkpoint(checkpoint_id)
        if not manifest:
            return {"error": "Unknown checkpoint."}
        target = _workspace_of(manifest)
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel or not snapshot._rel_is_valid(target, rel):
            return {"error": "Invalid path."}
        meta = (manifest.get("tree") or {}).get("files", {}).get(rel)
        old_path = snapshot.object_path(meta["h"]) if isinstance(meta, dict) and meta.get("h") else None
        current = target / rel
        if current.is_symlink():
            return {"error": "Symlinks are not diffed."}
        old, err_old = _read_text(old_path)
        new, err_new = _read_text(current)
        if err_old or err_new:
            return {"path": rel, "note": err_old or err_new, "diff": ""}
        lines = list(
            difflib.unified_diff(
                old, new, fromfile=f"{rel} @ {checkpoint_id}", tofile=f"{rel} (now)", n=3
            )
        )
        truncated = len(lines) > MAX_DIFF_LINES
        return {
            "path": rel,
            "diff": "".join(lines[:MAX_DIFF_LINES]),
            "truncated": truncated,
            "state": "added" if old_path is None else ("deleted" if not current.exists() else "modified"),
        }
    except Exception as exc:
        return {"error": f"Could not diff the file: {exc}"[:300]}
