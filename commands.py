"""The ``/timemachine`` slash command.

Every rollback restores files. hermes-time-machine is the one that knows what a
rollback can't undo. This command is the human-facing view of that: checkpoints,
a file-level rewind preview, and the external effects a rewind will not undo.

It runs in the Hermes gateway/CLI process: it returns text and never prints.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import os
import shlex

from . import handlers, storage

MAX_CHARS = 3500
USAGE = (
    "/timemachine [status|list [n]|effects [id]|diff <id> [path]|"
    "rewind <id> --yes [--allow-irreversible] [paths...]|help]"
)
HELP = (
    "hermes-time-machine: every rollback restores files; this one knows what a rollback can't undo.\n"
    + USAGE
    + "\n  status    protected folder, newest checkpoint, gate mode, effects a rewind would not undo"
    "\n  list      checkpoints of this workspace, newest first"
    "\n  effects   external effects since a checkpoint, with their compensation commands"
    "\n  diff      files that differ from a checkpoint; add a path for its unified diff"
    "\n  rewind    preview a restore; add --yes to apply it"
)


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


def _json(raw: str) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"ok": False, "error": "unexpected result"}
    except ValueError:
        return {"ok": False, "error": "unexpected result"}


def _size(path) -> int:
    total = 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def _when(ts) -> str:
    """Stored timestamps are UTC ISO; humans see local time, like Hermes' own UI."""
    try:
        parsed = datetime.fromisoformat(str(ts))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts or "")[:19].replace("T", " ")


def _effect_line(effect: dict) -> str:
    kind = str(effect.get("kind") or "effect").replace("_", " ")
    target = f" -> {effect['target']}" if effect.get("target") else ""
    status = effect.get("status")
    flag = "" if status in (None, "succeeded") else f" [{status}]"
    kind_flag = "" if effect.get("reversible") is False else " (reversible)"
    return f"{kind}{target}{flag}{kind_flag}"


def _status(session_id: str) -> str:
    target, error = handlers.workspace_status()
    lines = ["hermes-time-machine"]
    if target is None:
        lines.append(f"Workspace: not protected ({error})")
        lines.append("Checkpoints are off here; the approval gate and the effect ledger still work.")
    else:
        lines.append(f"Workspace: {target}")
    manifests = handlers.list_manifests()
    newest = manifests[0] if manifests else None
    lines.append(f"Checkpoints: {len(manifests)}")
    if newest:
        lines.append(
            f"Newest: {newest['id']} {_when(newest.get('created_at'))} "
            f"{newest.get('label')!r} ({newest.get('files')} files, {_mb(int(newest.get('bytes') or 0))})"
        )
    config = handlers.config()
    lines.append(f"Irreversible-effect gate: {config.get('irreversible_gate', 'approve')}")
    if newest:
        blocking = handlers.irreversible_since(newest, session_id)
        if blocking:
            lines.append(f"Effects a rewind to the newest checkpoint would NOT undo ({len(blocking)}):")
            for effect in blocking[-5:]:
                lines.append("  - " + _effect_line(effect))
                if effect.get("compensation"):
                    lines.append(f"    undo: {effect['compensation']}")
        else:
            lines.append("No irreversible effects since the newest checkpoint: a rewind is clean.")
    try:
        lines.append(f"Store size: {_mb(_size(storage.data_dir()))}")
    except Exception:
        pass
    return "\n".join(lines)


def _list(rest: list[str]) -> str:
    limit = 10
    if rest and rest[0].isdigit():
        limit = max(1, min(int(rest[0]), 50))
    data = _json(handlers.atm_list({"limit": limit}))
    if not data.get("ok"):
        return f"Error: {data.get('error')}"
    items = data.get("checkpoints") or []
    if not items:
        return "No checkpoints for this workspace yet."
    lines = [f"Checkpoints ({len(items)}):"]
    for item in items:
        kind = "auto" if item.get("auto") else "manual"
        lines.append(
            f"{item['id']}  {_when(item.get('created_at'))}  {kind:6} {item.get('files')} files  {item.get('label')}"
        )
    return "\n".join(lines)


def _effects(rest: list[str], session_id: str) -> str:
    args = {"since": rest[0]} if rest else {}
    data = _json(handlers.atm_effects(args, session_id=session_id))
    if not data.get("ok"):
        return f"Error: {data.get('error')}"
    items = data.get("effects") or []
    since = data.get("since")
    lines = [f"Effects since {since or 'the beginning'}: {len(items)}"]
    if not items:
        lines.append("None recorded.")
    for effect in items[-12:]:
        lines.append(f"{_when(effect.get('ts'))}  {_effect_line(effect)}")
        if effect.get("compensation"):
            lines.append(f"    undo: {effect['compensation']}")
        elif effect.get("compensation_note") and effect.get("reversible") is False:
            lines.append(f"    note: {effect['compensation_note']}")
    lines.append("Safe to rewind: " + ("yes" if data.get("safe_to_rewind") else "files only; effects above stay"))
    return "\n".join(lines)


def _split(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _changes(data: dict) -> list[str]:
    changes = data.get("changes") or {}
    counts = changes.get("counts") or {}
    lines = [
        f"Will revert {counts.get('modified', 0)}, restore {counts.get('deleted', 0)} "
        f"deleted, remove {counts.get('added', 0)} new file(s); {counts.get('unchanged', 0)} unchanged."
    ]
    for label, key in (("revert", "modified"), ("restore", "deleted"), ("remove", "added")):
        for path in (changes.get(key) or [])[:8]:
            lines.append(f"  {label}: {path}")
        extra = counts.get(key, 0) - min(len(changes.get(key) or []), 8)
        if extra > 0:
            lines.append(f"  ... and {extra} more {label}")
    return lines


def _diff(rest: list[str]) -> str:
    if not rest:
        return "Usage: /timemachine diff <checkpoint id> [path]"
    if len(rest) > 1:
        data = _json(handlers.atm_diff({"checkpoint_id": rest[0], "path": " ".join(rest[1:])}))
        if not data.get("ok"):
            return f"Error: {data.get('error')}"
        if data.get("note"):
            return f"{data.get('path')}: {data['note']}"
        body = data.get("diff") or "No text differences."
        return body + ("\n... (truncated)" if data.get("truncated") else "")
    data = _json(handlers.atm_rewind({"checkpoint_id": rest[0]}))
    if not data.get("ok"):
        return f"Error: {data.get('error')}"
    return "\n".join([f"Differences from {rest[0]}:", *_changes(data)])


def _rewind(rest: list[str], session_id: str) -> str:
    yes = "--yes" in rest
    allow = "--allow-irreversible" in rest
    tokens = [t for t in rest if t not in ("--yes", "--allow-irreversible")]
    checkpoint_id = None
    if tokens and tokens[0].startswith("ckpt_"):
        checkpoint_id = tokens.pop(0)
    args: dict = {"confirm": yes, "allow_irreversible": allow}
    if checkpoint_id:
        args["checkpoint_id"] = checkpoint_id
    if tokens:
        args["paths"] = tokens
    data = _json(handlers.atm_rewind(args, session_id=session_id))
    lines: list[str] = []
    if data.get("preview"):
        info = data.get("checkpoint") or {}
        lines.append(f"Rewind preview: {info.get('id')} {info.get('label')!r}")
        lines.extend(_changes(data))
        blocking = data.get("irreversible") or []
        if blocking:
            lines.append(f"A rewind will NOT undo {len(blocking)} external effect(s):")
            for effect in blocking:
                lines.append("  - " + _effect_line(effect))
                if effect.get("compensation"):
                    lines.append(f"    undo: {effect['compensation']}")
        add = " --allow-irreversible" if blocking else ""
        lines.append(f"Apply with: /timemachine rewind {info.get('id')} --yes{add}")
    elif data.get("ok"):
        info = data.get("checkpoint") or {}
        lines.append(
            f"Restored {info.get('id')}: {data.get('restored')} file(s) reverted, "
            f"{data.get('removed')} removed."
        )
        lines.append(f"Previous contents kept in: {data.get('previous_contents')}")
        for effect in data.get("still_true_outside") or []:
            lines.append("Still true outside: " + _effect_line(effect))
            if effect.get("compensation"):
                lines.append(f"    undo: {effect['compensation']}")
        for err in (data.get("errors") or [])[:5]:
            lines.append(f"  error: {err}")
    else:
        lines.append(f"Error: {data.get('error')}")
        if data.get("hint"):
            lines.append(str(data["hint"]))
        for effect in data.get("irreversible") or []:
            lines.append("  - " + _effect_line(effect))
        if data.get("irreversible"):
            lines.append("Re-run with --allow-irreversible to restore files anyway.")
    return "\n".join(lines)


def handle(raw_args: str = "", *, session_id: str = "", platform: str = "", **kwargs) -> str:
    """Handler registered for ``/timemachine``."""
    try:
        tokens = _split(raw_args or "")
        sub = tokens[0].lower() if tokens else "status"
        rest = tokens[1:]
        sid = str(session_id or "")
        if sub in ("status", ""):
            text = _status(sid)
        elif sub in ("list", "ls"):
            text = _list(rest)
        elif sub == "effects":
            text = _effects(rest, sid)
        elif sub == "diff":
            text = _diff(rest)
        elif sub in ("rewind", "restore"):
            text = _rewind(rest, sid)
        elif sub in ("help", "-h", "--help"):
            text = HELP
        else:
            text = f"Unknown subcommand {sub!r}.\n{USAGE}"
        return _clip(text)
    except Exception as exc:
        return f"hermes-time-machine error: {exc}"


def register(ctx) -> None:
    ctx.register_command(
        "timemachine",
        handle,
        description="Checkpoints, rewind preview and the effects a rewind can't undo",
        args_hint="[status|list|effects|diff <id>|rewind <id> --yes [--allow-irreversible] [paths...]]",
    )
