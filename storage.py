"""Durable storage helpers.

State lives outside the install tree so `hermes plugins update` and
`hermes plugins remove` never delete a user's checkpoints.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_NAME = "hermes-time-machine"
_LOCK = threading.RLock()


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
    return root


def checkpoints_dir() -> Path:
    path = data_dir() / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def trash_dir() -> Path:
    path = data_dir() / "trash"
    path.mkdir(parents=True, exist_ok=True)
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


def append_jsonl(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


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
