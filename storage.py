"""Durable storage helpers.

State lives outside the install tree so `hermes plugins update` and
`hermes plugins remove` never delete a user's checkpoints.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_NAME = "hermes-time-machine"
_LOCK = threading.RLock()

# Canonical checkpoint id. Every public entry point validates against this
# before a value is used to build a filesystem path.
CHECKPOINT_ID_RE = re.compile(r"^ckpt_[0-9]{8}T[0-9]{6}_[0-9a-f]{8}$")


def is_valid_id(value: object) -> bool:
    return bool(value) and bool(CHECKPOINT_ID_RE.fullmatch(str(value)))


def _hermes_home() -> Path:
    """Locate the Hermes home without assuming a POSIX layout."""
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    try:
        from hermes_cli.config import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def secure_dir(path: Path) -> None:
    """Best-effort private permissions for checkpoint data.

    On POSIX the snapshots contain a full copy of the workspace, so they must
    not be world-readable. Windows only has a read-only bit, so this is a
    no-op there.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def secure_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def data_dir() -> Path:
    """Return the per-plugin data root, creating it on first use."""
    override = os.environ.get("ATM_DATA_DIR")
    if override:
        root = Path(override).expanduser()
    else:
        try:
            from plugins.plugin_storage import plugin_data_dir  # type: ignore

            root = Path(plugin_data_dir(PLUGIN_NAME))
        except Exception:
            root = _hermes_home() / "plugin-data" / PLUGIN_NAME
    root.mkdir(parents=True, exist_ok=True)
    secure_dir(root)
    return root


def checkpoints_dir() -> Path:
    path = data_dir() / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    secure_dir(path)
    return path


def trash_dir() -> Path:
    path = data_dir() / "trash"
    path.mkdir(parents=True, exist_ok=True)
    secure_dir(path)
    return path


def journal_path() -> Path:
    return data_dir() / "journal.jsonl"


def effects_path() -> Path:
    return data_dir() / "effects.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ckpt_{stamp}_{uuid.uuid4().hex[:8]}"


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    secure_file(temporary)
    temporary.replace(path)
    secure_file(path)


def _rotate(path: Path, keep: int) -> None:
    """Rewrite *path* keeping only its last *keep* non-empty lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    tail = [line for line in lines if line.strip()][-max(1, keep) :]
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text("\n".join(tail) + ("\n" if tail else ""), encoding="utf-8")
        secure_file(temporary)
        temporary.replace(path)
        secure_file(path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def append_jsonl(
    path: Path,
    record: dict,
    max_bytes: int | None = None,
    keep: int = 2000,
) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if max_bytes is not None:
            try:
                if path.stat().st_size > max_bytes:
                    _rotate(path, keep)
            except OSError:
                pass


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with _LOCK:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if limit is not None:
        return records[-limit:]
    return records
