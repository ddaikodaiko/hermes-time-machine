"""Tool handlers for hermes-time-machine."""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

from . import snapshot, storage

_CHECKPOINT_ID_RE = re.compile(r"^ckpt_[0-9]{8}T[0-9]{6}_[0-9a-f]{8}$")

_CONFIG: dict = {}
_WORKSPACE: Path | None = None
_WORKSPACE_ERROR: str | None = None
_LAST: str | None = None
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Configuration and workspace
# ---------------------------------------------------------------------------


def configure(config: dict | None) -> None:
    global _CONFIG, _WORKSPACE, _WORKSPACE_ERROR, _LAST
    with _LOCK:
        _CONFIG = dict(config or {})
        _WORKSPACE = None
        _WORKSPACE_ERROR = None
        try:
            _WORKSPACE = snapshot.resolve_workspace(_CONFIG.get("workspace"))
        except Exception as exc:  # keep register() alive; fail on first use
            _WORKSPACE_ERROR = str(exc)
        latest = _latest()
        _LAST = latest["id"] if latest else None


def config() -> dict:
    return _CONFIG


def workspace() -> Path:
    global _WORKSPACE, _WORKSPACE_ERROR
    with _LOCK:
        if _WORKSPACE is not None:
            return _WORKSPACE
        if _WORKSPACE_ERROR:
            raise snapshot.SnapshotError(_WORKSPACE_ERROR)
        _WORKSPACE = snapshot.resolve_workspace(_CONFIG.get("workspace"))
        return _WORKSPACE


def last_checkpoint() -> str | None:
    return _LAST


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _manifest_path(checkpoint_id: str) -> Path:
    return snapshot.checkpoint_dir(checkpoint_id) / "manifest.json"


def _load(checkpoint_id: str | None) -> dict | None:
    if not checkpoint_id or not _CHECKPOINT_ID_RE.fullmatch(str(checkpoint_id)):
        return None
    path = _manifest_path(str(checkpoint_id))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _all_manifests() -> list[dict]:
    manifests: list[dict] = []
    for child in storage.checkpoints_dir().iterdir():
        if not child.is_dir():
            continue
        manifest = _load(child.name)
        if manifest:
            manifests.append(manifest)
    manifests.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return manifests


def _latest() -> dict | None:
    manifests = _all_manifests()
    return manifests[0] if manifests else None


def _public(manifest: dict) -> dict:
    return {
        "id": manifest.get("id"),
        "label": manifest.get("label"),
        "created_at": manifest.get("created_at"),
        "auto": manifest.get("auto", False),
        "trigger": manifest.get("trigger"),
        "bytes": manifest.get("bytes"),
        "files": manifest.get("files"),
        "workspace": manifest.get("workspace"),
    }


def _write_manifest(manifest: dict) -> None:
    path = _manifest_path(manifest["id"])
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _prune() -> None:
    retention = int(_CONFIG.get("retention", 20) or 20)
    if retention >= 1:
        automatic = [item for item in _all_manifests() if item.get("auto")]
        for stale in automatic[retention:]:
            try:
                shutil.rmtree(snapshot.checkpoint_dir(stale["id"]))
            except OSError:
                continue
    _prune_trash()


def _prune_trash(keep: int = 10) -> None:
    """Keep only the most recent rewind backups.

    A rewind moves the previous workspace into ``trash/``; without a cap that
    directory grows forever.
    """
    try:
        dirs = [child for child in storage.trash_dir().iterdir() if child.is_dir()]
        dirs.sort(key=lambda child: child.stat().st_mtime, reverse=True)
        for stale in dirs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        return


def _effects_since(created_at: str | None) -> list[dict]:
    records = storage.read_jsonl(storage.effects_path())
    if not created_at:
        return records
    return [record for record in records if record.get("ts", "") >= created_at]


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def create_checkpoint(label: str, trigger: dict | None = None, auto: bool = False) -> dict | None:
    global _LAST
    with _LOCK:
        target = workspace()
        excludes = snapshot.excludes_for(_CONFIG)
        limit = int(_CONFIG.get("max_snapshot_mb", 200) or 200) * 1024 * 1024
        size, _ = snapshot.measure(target, excludes, limit if auto else None)
        if auto and size > limit:
            storage.append_jsonl(
                storage.journal_path(),
                {
                    "type": "checkpoint_skipped",
                    "ts": storage.now_iso(),
                    "reason": "max_snapshot_mb",
                    "bytes": size,
                    "limit": limit,
                },
            )
            return None

        checkpoint_id = storage.new_id()
        try:
            info = snapshot.create(target, checkpoint_id, excludes)
        except Exception:
            shutil.rmtree(snapshot.checkpoint_dir(checkpoint_id), ignore_errors=True)
            raise

        manifest = {
            "id": checkpoint_id,
            "label": label,
            "created_at": storage.now_iso(),
            "auto": auto,
            "trigger": trigger or {},
            "workspace": str(target),
            "excludes": sorted(excludes),
            "archive": info["archive"],
            "bytes": info["bytes"],
            "files": info["files"],
        }
        _write_manifest(manifest)
        storage.append_jsonl(
            storage.journal_path(),
            {"type": "checkpoint", "ts": manifest["created_at"], **manifest},
        )
        _LAST = checkpoint_id
        _prune()
        return manifest


def _resolve_id(value: str | None) -> dict | None:
    return _load(value) if value else (_load(_LAST) or _latest())


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def atm_checkpoint(args: dict, **kwargs) -> str:
    try:
        label = str(args.get("label") or "manual").strip()[:120]
        manifest = create_checkpoint(
            label,
            {"type": "manual", "tool": kwargs.get("tool_name")},
            auto=False,
        )
        return json.dumps({"ok": True, "checkpoint": _public(manifest or {})})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def atm_list(args: dict, **kwargs) -> str:
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        items = [_public(item) for item in _all_manifests()[:limit]]
        return json.dumps({"ok": True, "count": len(items), "checkpoints": items})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def atm_effects(args: dict, **kwargs) -> str:
    try:
        manifest = _resolve_id(args.get("since"))
        since = manifest.get("created_at") if manifest else None
        records = _effects_since(since)
        blocking = [record for record in records if not record.get("reversible", True)]
        return json.dumps(
            {
                "ok": True,
                "since": manifest.get("id") if manifest else None,
                "effects": records,
                "irreversible": blocking,
                "safe_to_rewind": not blocking,
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def atm_rewind(args: dict, **kwargs) -> str:
    try:
        manifest = _resolve_id(args.get("checkpoint_id"))
        if not manifest:
            return json.dumps({"ok": False, "error": "No checkpoint found."})

        blocking = [item for item in _effects_since(manifest.get("created_at")) if not item.get("reversible", True)]

        if not args.get("confirm"):
            return json.dumps(
                {
                    "ok": True,
                    "preview": True,
                    "checkpoint": _public(manifest),
                    "irreversible": blocking,
                    "safe_to_rewind": not blocking,
                    "hint": (
                        "Call again with confirm=true to restore the workspace."
                        if not blocking
                        else "Call again with confirm=true and allow_irreversible=true to restore files anyway."
                    ),
                }
            )

        if blocking and not args.get("allow_irreversible"):
            return json.dumps(
                {
                    "ok": False,
                    "error": "Irreversible effects happened after this checkpoint. They cannot be undone.",
                    "checkpoint": _public(manifest),
                    "irreversible": blocking,
                    "hint": "Call again with allow_irreversible=true to restore files anyway.",
                }
            )

        result = snapshot.restore(workspace(), manifest["id"], snapshot.excludes_for(_CONFIG))
        storage.append_jsonl(
            storage.journal_path(),
            {
                "type": "rewind",
                "ts": storage.now_iso(),
                "checkpoint_id": manifest["id"],
                "previous_contents": result["previous_contents"],
            },
        )
        return json.dumps(
            {
                "ok": True,
                "restored": _public(manifest),
                "previous_contents": result["previous_contents"],
                "moved_entries": result["moved_entries"],
            }
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def atm_fork(args: dict, **kwargs) -> str:
    try:
        manifest = _resolve_id(args.get("checkpoint_id"))
        if not manifest:
            return json.dumps({"ok": False, "error": "No checkpoint found."})

        base = workspace()
        suffix = str(args.get("name") or "").strip()
        suffix = "-" + "".join(ch for ch in suffix if ch.isalnum() or ch in "-_") if suffix else ""
        destination = base.parent / f"{base.name}-fork{suffix}"
        if destination.exists():
            destination = base.parent / f"{base.name}-fork{suffix}-{storage.new_id()[-6:]}"

        snapshot.extract_to(manifest["id"], destination)
        storage.append_jsonl(
            storage.journal_path(),
            {
                "type": "fork",
                "ts": storage.now_iso(),
                "checkpoint_id": manifest["id"],
                "destination": str(destination),
            },
        )
        return json.dumps({"ok": True, "checkpoint": _public(manifest), "path": str(destination)})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})
