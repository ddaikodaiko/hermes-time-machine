"""Filesystem snapshots: create, restore and branch a workspace.

This module deliberately uses only the Python standard library. No native
binaries, no shell, no external services.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import uuid
from pathlib import Path

from . import storage


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be created or restored safely."""


BASE_EXCLUDES = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".turbo",
    "target",
    ".idea",
    ".DS_Store",
}


def resolve_workspace(raw: str | None) -> Path:
    """Resolve the protected workspace and refuse obviously unsafe roots."""
    candidate = Path(raw).expanduser() if raw else Path.cwd()
    workspace = candidate.resolve()
    assert_safe(workspace)
    return workspace


def assert_safe(workspace: Path) -> None:
    if not workspace.exists() or not workspace.is_dir():
        raise SnapshotError(f"Workspace does not exist or is not a directory: {workspace}")

    if workspace.parent == workspace:
        raise SnapshotError("Refusing to protect a filesystem root.")

    home = Path.home().resolve()
    if workspace == home:
        raise SnapshotError("Refusing to protect the home directory itself.")
    if workspace in home.parents:
        raise SnapshotError(
            f"Refusing to protect a directory that contains the home directory: {workspace}"
        )

    system_roots = {
        Path("C:/Windows"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("/usr"),
        Path("/etc"),
        Path("/var"),
        Path("/bin"),
        Path("/sbin"),
        Path("/lib"),
        Path("/boot"),
        Path("/dev"),
    }
    for root in system_roots:
        try:
            if workspace == root.resolve():
                raise SnapshotError(f"Refusing to protect a system directory: {workspace}")
        except OSError:
            continue

    data = storage.data_dir().resolve()
    if workspace == data or data in workspace.parents or workspace in data.parents:
        raise SnapshotError("Workspace and plugin data directory must not contain each other.")


def excludes_for(config: dict) -> set[str]:
    excludes = set(BASE_EXCLUDES)
    if config.get("exclude_git"):
        excludes.add(".git")
    return excludes


def _is_excluded(relative: Path, excludes: set[str]) -> bool:
    return any(part in excludes for part in relative.parts)


def measure(workspace: Path, excludes: set[str]) -> tuple[int, int]:
    total = 0
    files = 0
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in excludes]
        for name in names:
            if name in excludes:
                continue
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def checkpoint_dir(checkpoint_id: str) -> Path:
    return storage.checkpoints_dir() / checkpoint_id


def archive_path(checkpoint_id: str) -> Path:
    return checkpoint_dir(checkpoint_id) / "workspace.tar.gz"


def create(workspace: Path, checkpoint_id: str, excludes: set[str]) -> dict:
    """Write a gzipped tar of the workspace and return its stats."""
    target = checkpoint_dir(checkpoint_id)
    target.mkdir(parents=True, exist_ok=False)
    archive = target / "workspace.tar.gz"

    files = 0
    with tarfile.open(archive, "w:gz") as tar:
        for root, dirs, names in os.walk(workspace):
            dirs[:] = [name for name in dirs if name not in excludes]
            for name in names:
                if name in excludes:
                    continue
                path = Path(root) / name
                try:
                    relative = path.relative_to(workspace)
                except ValueError:
                    continue
                if _is_excluded(relative, excludes):
                    continue
                try:
                    tar.add(path, arcname=relative.as_posix(), recursive=False)
                    files += 1
                except OSError:
                    continue

    return {
        "archive": archive.name,
        "bytes": archive.stat().st_size,
        "files": files,
    }


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(destination, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(destination)


def restore(workspace: Path, checkpoint_id: str, excludes: set[str]) -> dict:
    """Replace the workspace contents with the checkpoint contents.

    The current contents are moved to a trash directory first, so a failed
    restore (or a mistaken one) can be recovered manually.
    """
    assert_safe(workspace)
    archive = archive_path(checkpoint_id)
    if not archive.exists():
        raise SnapshotError(f"Checkpoint archive not found: {checkpoint_id}")

    trash = storage.trash_dir() / f"{checkpoint_id}_{uuid.uuid4().hex[:6]}"
    trash.mkdir(parents=True, exist_ok=False)

    moved: list[str] = []
    for child in workspace.iterdir():
        if child.name in excludes:
            continue
        shutil.move(str(child), str(trash / child.name))
        moved.append(child.name)

    try:
        _extract(archive, workspace)
    except Exception:
        # Best effort: put the previous contents back.
        for child in workspace.iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError:
                pass
        for name in moved:
            try:
                shutil.move(str(trash / name), str(workspace / name))
            except OSError:
                pass
        raise

    return {
        "restored_from": str(archive),
        "previous_contents": str(trash),
        "moved_entries": len(moved),
    }


def extract_to(checkpoint_id: str, destination: Path) -> dict:
    """Extract a checkpoint into a new directory (used by atm_fork)."""
    archive = archive_path(checkpoint_id)
    if not archive.exists():
        raise SnapshotError(f"Checkpoint archive not found: {checkpoint_id}")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise SnapshotError(f"Fork destination already exists and is not empty: {destination}")
    _extract(archive, destination)
    return {"destination": str(destination)}
