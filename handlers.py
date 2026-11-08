"""Tool handlers and checkpoint operations for hermes-time-machine.

Every rollback restores files. hermes-time-machine is the one that knows what a
rollback can't undo: checkpoints are incremental (content-addressed) and
time-bounded, and every rewind preview lists the external effects (pushes,
publishes, applies) that a restore will NOT undo.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

from . import snapshot, storage

FORMAT = "atm-checkpoint-v2"
_LIST_CAP = 50
_MANUAL_BUDGET_SECONDS = 120.0
_JOURNAL_MAX_BYTES = 2 * 1024 * 1024
_EFFECTS_MAX_BYTES = 5 * 1024 * 1024

_CONFIG: dict = {}
_LAST: str | None = None
_LOCK = threading.RLock()
_WS_CACHE: dict[str, tuple[Path | None, str | None]] = {}
_SKIP_LOGGED: set[str] = set()


# ---------------------------------------------------------------------------
# Configuration and workspace
# ---------------------------------------------------------------------------


def configure(config: dict | None) -> None:
    global _CONFIG, _LAST
    with _LOCK:
        _CONFIG = dict(config or {})
        _WS_CACHE.clear()
        _SKIP_LOGGED.clear()
        try:
            latest = _latest()
        except Exception:
            latest = None
        _LAST = latest["id"] if latest else None


def config() -> dict:
    return _CONFIG


def session_dir() -> Path:
    """The folder the agent's commands run in, fully resolved. Never raises."""
    candidate = None
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # type: ignore

        candidate = Path(resolve_agent_cwd())
    except Exception:
        pass
    if candidate is None:
        try:
            candidate = Path.cwd()
        except Exception:
            return Path(".")
    try:
        # Long, canonical form: keeps path comparisons honest with Windows 8.3 short names.
        return candidate.resolve()
    except Exception:
        return candidate


def _raw_workspace() -> str:
    configured = _CONFIG.get("workspace")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return str(session_dir())


def workspace() -> Path:
    """The protected folder. Raises SnapshotError when it is missing or unsafe."""
    raw = _raw_workspace()
    with _LOCK:
        cached = _WS_CACHE.get(raw)
        if cached is None:
            try:
                resolved = Path(raw).expanduser().resolve()
                snapshot.assert_safe(resolved)
                cached = (resolved, None)
            except Exception as exc:
                cached = (None, str(exc))
            _WS_CACHE[raw] = cached
    if cached[0] is None:
        raise snapshot.SnapshotError(cached[1] or "Workspace is not available.")
    return cached[0]


def safe_workspace(journal: bool = True) -> Path | None:
    """Like workspace() but returns None when unsafe (journals that once per path)."""
    try:
        return workspace()
    except Exception as exc:
        if journal:
            _journal_skip_once(_raw_workspace(), "unsafe_workspace", error=str(exc))
        return None


def workspace_status() -> tuple[Path | None, str | None]:
    try:
        return workspace(), None
    except Exception as exc:
        return None, str(exc)


def workspace_key() -> str:
    """Identity recorded in the ledger: the protected folder, else the session folder."""
    target = safe_workspace(journal=False)
    return str(target if target is not None else session_dir())


def last_checkpoint() -> str | None:
    return _LAST


def workspace_checkpoint() -> str | None:
    """Newest checkpoint whose manifest workspace is the current safe workspace, else None."""
    try:
        manifests = list_manifests(False)
        return manifests[0]["id"] if manifests else None
    except Exception:
        return None


def _journal(record: dict) -> None:
    try:
        storage.append_jsonl(
            storage.journal_path(),
            {"ts": storage.now_iso(), **record},
            max_bytes=_JOURNAL_MAX_BYTES,
            keep=2000,
        )
    except Exception:
        pass


def _journal_skip_once(key: str, reason: str, **extra) -> None:
    with _LOCK:
        token = f"{reason}|{key}"
        if token in _SKIP_LOGGED:
            return
        _SKIP_LOGGED.add(token)
    _journal({"type": "checkpoint_skipped", "reason": reason, "workspace": key, **extra})


def append_effect(record: dict) -> None:
    storage.append_jsonl(storage.effects_path(), record, max_bytes=_EFFECTS_MAX_BYTES, keep=5000)


def record_journal(record: dict) -> None:
    _journal(record)


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _manifest_path(checkpoint_id: str) -> Path:
    return snapshot.checkpoint_dir(checkpoint_id) / "manifest.json"


def _load(checkpoint_id: str | None) -> dict | None:
    if not storage.is_valid_id(checkpoint_id):
        return None
    path = _manifest_path(str(checkpoint_id))
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tree"), dict):
        return None  # legacy (pre-0.2) archive checkpoints cannot be restored by this engine
    return manifest


def get_checkpoint(checkpoint_id: str | None) -> dict | None:
    return _load(checkpoint_id)


def _all_manifests() -> list[dict]:
    manifests: list[dict] = []
    try:
        children = list(storage.checkpoints_dir().iterdir())
    except OSError:
        return manifests
    for child in children:
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


def list_manifests(all_workspaces: bool = False) -> list[dict]:
    manifests = _all_manifests()
    if all_workspaces:
        return manifests
    target = safe_workspace(journal=False)
    if target is None:
        return []
    key = str(target)
    return [item for item in manifests if item.get("workspace") == key]


def _public(manifest: dict) -> dict:
    return {
        "id": manifest.get("id"),
        "label": manifest.get("label"),
        "created_at": manifest.get("created_at"),
        "auto": manifest.get("auto", False),
        "trigger": manifest.get("trigger"),
        "bytes": manifest.get("bytes"),
        "files": manifest.get("files"),
        "new_bytes": manifest.get("new_bytes"),
        "reused": manifest.get("reused"),
        "skipped_count": manifest.get("skipped_count"),
        "duration_ms": manifest.get("duration_ms"),
        "workspace": manifest.get("workspace"),
    }


def public(manifest: dict) -> dict:
    return _public(manifest)


def _seed_path(target: Path) -> Path:
    digest = hashlib.sha256(str(target).encode("utf-8", "replace")).hexdigest()[:16]
    seeds = storage.data_dir() / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    return seeds / f"{digest}.json"


def _read_seed(target: Path) -> dict:
    try:
        data = json.loads(_seed_path(target).read_text(encoding="utf-8"))
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}
    except (OSError, ValueError):
        return {}


def _previous_tree(target: Path) -> dict | None:
    """Newest checkpoint tree for *target* with the warm-up seed merged over it."""
    key = str(target)
    base: dict = {}
    for manifest in _all_manifests():
        if manifest.get("workspace") == key:
            base = dict(manifest["tree"].get("files") or {})
            break
    seed = _read_seed(target)
    if seed:
        base.update(seed)
    return {"files": base} if base else None


def _seed_hashes() -> set[str]:
    out: set[str] = set()
    try:
        seeds = storage.data_dir() / "seeds"
        if not seeds.is_dir():
            return out
        for path in seeds.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out |= snapshot.referenced_hashes([data] if isinstance(data, dict) else [])
    except OSError:
        pass
    return out


def _manifests_complete(manifests: list[dict]) -> bool:
    """False when a checkpoint folder exists whose manifest could not be read (locked, corrupt,
    listing failed): garbage collecting then could delete objects a live checkpoint needs."""
    try:
        loaded = {m.get("id") for m in manifests}
        for child in storage.checkpoints_dir().iterdir():
            if not child.is_dir() or child.name in loaded:
                continue
            path = child / "manifest.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if isinstance(data, dict) and isinstance(data.get("tree"), dict):
                return False
        return True
    except OSError:
        return False


def _prune() -> None:
    retention = int(_CONFIG.get("retention", 20) or 20)
    manifests = _all_manifests()
    gc_safe = _manifests_complete(manifests)
    if retention >= 1:
        automatic = [item for item in manifests if item.get("auto")]
        for stale in automatic[retention:]:
            try:
                shutil.rmtree(snapshot.checkpoint_dir(stale["id"]))
            except OSError:
                continue
        manifests = _all_manifests()
    try:
        if gc_safe:
            referenced = snapshot.referenced_hashes([m.get("tree") or {} for m in manifests])
            snapshot.gc(referenced | _seed_hashes())
    except Exception:
        pass
    _prune_trash()


def _prune_trash(keep: int = 10) -> None:
    """Keep only the most recent rewind backups (a rewind moves replaced files to trash/)."""
    try:
        dirs = [child for child in storage.trash_dir().iterdir() if child.is_dir()]
        dirs.sort(key=lambda child: child.stat().st_mtime, reverse=True)
        for stale in dirs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        return


# ---------------------------------------------------------------------------
# Effect ledger queries
# ---------------------------------------------------------------------------


def read_ledger() -> list[dict]:
    return storage.read_jsonl(storage.effects_path())


def is_irreversible(record: dict) -> bool:
    """Irreversible and not known to have failed."""
    if not isinstance(record, dict) or record.get("reversible", True):
        return False
    return record.get("status") in (None, "succeeded")


def ledger_since(
    created_at: str | None,
    workspace_str: str | None = None,
    session_id: str | None = None,
    records: list[dict] | None = None,
) -> list[dict]:
    """Ledger records at or after *created_at* that belong to this workspace or session."""
    out = []
    for record in read_ledger() if records is None else records:
        if not isinstance(record, dict):
            continue
        if created_at and str(record.get("ts", "")) < created_at:
            continue
        rec_ws = record.get("workspace")
        same_ws = workspace_str is None or rec_ws is None or rec_ws == workspace_str
        same_session = bool(session_id) and record.get("session_id") == session_id
        if same_ws or same_session:
            out.append(record)
    return out


def _effect_view(record: dict) -> dict:
    keep = (
        "ts", "tool", "kind", "reversible", "status", "target", "reason",
        "evidence", "compensation", "compensation_note", "checkpoint_id", "session_id",
    )
    return {key: record.get(key) for key in keep if key in record}


def irreversible_since(manifest: dict, session_id: str | None = None) -> list[dict]:
    records = ledger_since(manifest.get("created_at"), manifest.get("workspace"), session_id)
    return [_effect_view(r) for r in records if is_irreversible(r)]


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def _persist_seed(target: Path, partial: dict) -> int:
    files = partial.get("files") if isinstance(partial, dict) else None
    if not isinstance(files, dict) or not files:
        return 0
    try:
        storage.write_json_atomic(
            _seed_path(target),
            {"format": snapshot.TREE_FORMAT, "workspace": str(target), "files": files},
        )
    except OSError:
        return 0
    return len(files)


def create_checkpoint(label: str, trigger: dict | None = None, auto: bool = False) -> dict | None:
    """Capture the workspace. Returns the manifest, or None for a skipped auto checkpoint.

    Manual checkpoints raise on failure. Automatic ones are time-bounded
    (``auto_budget_seconds``); when the budget runs out the work done so far is
    kept as a seed so the next attempt resumes instead of starting over.
    """
    global _LAST
    with _LOCK:
        target = workspace()
        excludes = snapshot.excludes_for(_CONFIG)
        limit = int(_CONFIG.get("max_snapshot_mb", 200) or 200) * 1024 * 1024
        started = time.monotonic()
        budget = (
            float(_CONFIG.get("auto_budget_seconds", 5.0) or 5.0) if auto else _MANUAL_BUDGET_SECONDS
        )
        if auto:
            size, _ = snapshot.measure(target, excludes, limit, deadline=started + budget)
            if size < 0:
                _journal({"type": "checkpoint_skipped", "reason": "measure_timeout",
                          "budget_seconds": budget, "workspace": str(target)})
                return None
            if size > limit:
                _journal(
                    {
                        "type": "checkpoint_skipped",
                        "reason": "max_snapshot_mb",
                        "bytes": size,
                        "limit": limit,
                        "workspace": str(target),
                    }
                )
                return None

        max_file = int(_CONFIG.get("max_file_mb", 50) or 50) * 1024 * 1024
        previous = _previous_tree(target)
        try:
            tree = snapshot.capture(
                target,
                excludes,
                previous_tree=previous,
                deadline=started + budget,
                max_file_bytes=max_file,
            )
        except snapshot.SnapshotTimeout as exc:
            done = _persist_seed(target, getattr(exc, "partial", None) or {})
            _journal(
                {
                    "type": "checkpoint_skipped",
                    "reason": "timeout_warming" if done else "timeout",
                    "files_done": done,
                    "budget_seconds": budget,
                    "workspace": str(target),
                }
            )
            if auto:
                return None
            raise snapshot.SnapshotError(
                f"Checkpoint ran out of time after {done} files; the work is kept, run it again to continue."
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        checkpoint_id = storage.new_id()
        directory = snapshot.checkpoint_dir(checkpoint_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            storage.secure_dir(directory)
            manifest = {
                "id": checkpoint_id,
                "label": label,
                "created_at": storage.now_iso(),
                "auto": auto,
                "trigger": trigger or {},
                "workspace": str(target),
                "excludes": sorted(excludes),
                "tree": tree,
                "bytes": tree.get("bytes", 0),
                "files": tree.get("files_count", len(tree.get("files") or {})),
                "new_bytes": tree.get("new_bytes", 0),
                "reused": tree.get("reused", 0),
                "skipped_count": len(tree.get("skipped") or []),
                "duration_ms": duration_ms,
                "format": FORMAT,
            }
            storage.write_json_atomic(_manifest_path(checkpoint_id), manifest)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        try:
            _seed_path(target).unlink()
        except OSError:
            pass
        _journal({"type": "checkpoint", **{k: v for k, v in manifest.items() if k != "tree"}})
        _LAST = checkpoint_id
        _prune()
        return manifest


def _current_key() -> str | None:
    target = safe_workspace(journal=False)
    return str(target) if target is not None else None


def _resolve_id(value: str | None) -> dict | None:
    if value:
        return _load(str(value))
    key = _current_key()
    for manifest in _all_manifests():
        if key is None or manifest.get("workspace") == key:
            return manifest
    return None


def resolve(value: str | None) -> dict | None:
    return _resolve_id(value)


def _capped(items: list) -> list:
    return list(items[:_LIST_CAP])


def preview_changes(manifest: dict, paths: list[str] | None = None) -> dict:
    """File-level diff between the current workspace and the checkpoint."""
    target = workspace()
    excludes = set(manifest.get("excludes") or snapshot.excludes_for(_CONFIG))
    diff = snapshot.diff(target, manifest["tree"], excludes, paths or None)
    modified = list(diff.get("modified") or [])
    deleted = list(diff.get("deleted") or [])
    added = list(diff.get("added") or [])
    return {
        "modified": _capped(modified),
        "deleted": _capped(deleted),
        "added": _capped(added),
        "counts": {
            "modified": len(modified),
            "deleted": len(deleted),
            "added": len(added),
            "unchanged": diff.get("unchanged", 0),
        },
        "truncated": max(len(modified), len(deleted), len(added)) > _LIST_CAP,
        "meaning": (
            "modified = files that will be reverted; deleted = files that will be brought back; "
            "added = files created since the checkpoint that will be moved to the trash."
        ),
    }


def _clean_paths(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise snapshot.SnapshotError("paths must be a list of relative paths")
    cleaned = [str(item) for item in value if isinstance(item, str) and item.strip()]
    return cleaned or None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _flag(value) -> bool:
    """Models sometimes send "false" as a string; bool("false") must not mean yes."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    return bool(value)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def atm_checkpoint(args: dict, **kwargs) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        label = str(args.get("label") or "manual").strip()[:120]
        manifest = create_checkpoint(
            label,
            {"type": "manual", "tool": kwargs.get("tool_name"), "session_id": kwargs.get("session_id")},
            auto=False,
        )
        info = _public(manifest or {})
        return _dumps(
            {
                "ok": True,
                "checkpoint": info,
                "duration_ms": info.get("duration_ms"),
                "reused": info.get("reused"),
                "new_bytes": info.get("new_bytes"),
            }
        )
    except Exception as exc:
        return _dumps({"ok": False, "error": str(exc)})


def atm_list(args: dict, **kwargs) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        limit = max(1, min(int(args.get("limit") or 20), 200))
        items = [_public(item) for item in list_manifests(_flag(args.get("all_workspaces")))[:limit]]
        return _dumps({"ok": True, "count": len(items), "checkpoints": items})
    except Exception as exc:
        return _dumps({"ok": False, "error": str(exc)})


def atm_effects(args: dict, **kwargs) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        manifest = _resolve_id(args.get("since"))
        since = manifest.get("created_at") if manifest else None
        scope = manifest.get("workspace") if manifest else _current_key()
        records = ledger_since(since, scope, kwargs.get("session_id"))
        views = [_effect_view(r) for r in records]
        blocking = [_effect_view(r) for r in records if is_irreversible(r)]
        return _dumps(
            {
                "ok": True,
                "since": manifest.get("id") if manifest else None,
                "count": len(views),
                "effects": views[-100:],
                "irreversible": blocking[-100:],
                "safe_to_rewind": not blocking,
            }
        )
    except Exception as exc:
        return _dumps({"ok": False, "error": str(exc)})


def atm_rewind(args: dict, **kwargs) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        manifest = _resolve_id(args.get("checkpoint_id"))
        if not manifest:
            return _dumps({"ok": False, "error": "No checkpoint found."})
        paths = _clean_paths(args.get("paths"))
        blocking = irreversible_since(manifest, kwargs.get("session_id"))
        target = workspace()
        if manifest.get("workspace") != str(target):
            return _dumps(
                {
                    "ok": False,
                    "error": "That checkpoint belongs to a different workspace.",
                    "checkpoint": _public(manifest),
                    "hint": f"It protects {manifest.get('workspace')}; the current workspace is {target}.",
                }
            )

        if not _flag(args.get("confirm")):
            return _dumps(
                {
                    "ok": True,
                    "preview": True,
                    "checkpoint": _public(manifest),
                    "changes": preview_changes(manifest, paths),
                    "irreversible": blocking,
                    "safe_to_rewind": not blocking,
                    "hint": (
                        "Call again with confirm=true to restore."
                        if not blocking
                        else (
                            "These external effects will NOT be undone by a restore. "
                            "Call again with confirm=true and allow_irreversible=true to restore files anyway."
                        )
                    ),
                }
            )

        if blocking and not _flag(args.get("allow_irreversible")):
            return _dumps(
                {
                    "ok": False,
                    "error": "Irreversible effects happened after this checkpoint. A restore cannot undo them.",
                    "checkpoint": _public(manifest),
                    "irreversible": blocking,
                    "hint": "Call again with allow_irreversible=true to restore files anyway.",
                }
            )

        excludes = set(manifest.get("excludes") or snapshot.excludes_for(_CONFIG))
        result = snapshot.restore(target, manifest["tree"], excludes, paths=paths)
        _journal(
            {
                "type": "rewind",
                "checkpoint_id": manifest["id"],
                "paths": paths,
                "restored": result.get("restored"),
                "removed": result.get("removed"),
                "previous_contents": result.get("previous_contents"),
            }
        )
        payload = {
            "ok": True,
            "restored": result.get("restored", 0),
            "removed": result.get("removed", 0),
            "unchanged": result.get("unchanged", 0),
            "checkpoint": _public(manifest),
            "previous_contents": result.get("previous_contents"),
            "errors": result.get("errors", []),
            "still_true_outside": blocking,
        }
        if blocking:
            payload["note"] = (
                "Files were restored. The external effects listed in still_true_outside already "
                "happened and were NOT undone; use their compensation command if they must be reverted."
            )
        return _dumps(payload)
    except Exception as exc:
        return _dumps({"ok": False, "error": str(exc)})


def atm_fork(args: dict, **kwargs) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        manifest = _resolve_id(args.get("checkpoint_id"))
        if not manifest:
            return _dumps({"ok": False, "error": "No checkpoint found."})

        base = workspace()
        suffix = str(args.get("name") or "").strip()
        suffix = "-" + "".join(ch for ch in suffix if ch.isalnum() or ch in "-_") if suffix else ""
        destination = base.parent / f"{base.name}-fork{suffix}"
        if destination.exists():
            destination = base.parent / f"{base.name}-fork{suffix}-{storage.new_id()[-6:]}"

        result = snapshot.materialize(manifest["tree"], destination)
        _journal({"type": "fork", "checkpoint_id": manifest["id"], "destination": str(destination)})
        return _dumps(
            {
                "ok": True,
                "checkpoint": _public(manifest),
                "path": str(destination),
                "files": result.get("files"),
                "errors": result.get("errors", []),
            }
        )
    except Exception as exc:
        return _dumps({"ok": False, "error": str(exc)})


