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

    # Exact matches only: these are broad trees where a real project may live.
    exact_roots = {
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("/usr"),
        Path("/var"),
        Path("/bin"),
        Path("/sbin"),
        Path("/lib"),
        Path("/Library"),
        Path("/Applications"),
    }
    # Exact matches and every descendant: nothing here should be snapshotted.
    critical_roots = {
        Path("C:/Windows"),
        Path("/etc"),
        Path("/boot"),
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
        Path("/System"),
        Path("/private/etc"),
        Path("/private/var"),
    }
    for root in exact_roots:
        try:
            if workspace == root.resolve():
                raise SnapshotError(f"Refusing to protect a system directory: {workspace}")
        except OSError:
            continue
    for root in critical_roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if workspace == resolved or resolved in workspace.parents:
            raise SnapshotError(f"Refusing to protect a system directory: {workspace}")

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


def measure(
    workspace: Path, excludes: set[str], limit_bytes: int | None = None
) -> tuple[int, int]:
    """Return ``(bytes, files)``, stopping early once *limit_bytes* is passed.

    The early exit keeps a huge repository from being walked in full just to
    decide that an automatic checkpoint should be skipped.
    """
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
            if limit_bytes is not None and total > limit_bytes:
                return total, files
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


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _safe_member(member: tarfile.TarInfo, destination: Path, root: Path) -> bool:
    """Reject archive members that would land outside *destination*."""
    if not _is_within(destination / member.name, root):
        return False
    if member.issym():
        link = Path(member.linkname)
        if link.is_absolute():
            return False
        return _is_within(destination / Path(member.name).parent / link, root)
    if member.islnk():
        link = Path(member.linkname)
        if link.is_absolute():
            return False
        return _is_within(destination / link, root)
    return True


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        try:
            # Python 3.12+ applies the same checks natively.
            tar.extractall(destination, filter="data")
        except TypeError:  # Python < 3.12 has no filter= argument
            members = tar.getmembers()
            for member in members:
                if not _safe_member(member, destination, root):
                    raise SnapshotError(
                        f"Unsafe path in checkpoint archive: {member.name}"
                    )
            tar.extractall(destination, members=members)


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
