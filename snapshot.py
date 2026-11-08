"""Incremental content-addressed workspace snapshots.

A checkpoint is a *tree*: a mapping of relative paths to sha256 hashes. File
contents live once in ``objects/<h[0:2]>/<h>``; an unchanged file costs only a
``stat()`` call on the next capture. The tree also gives file-level diff and
single-file restore for free.

This module deliberately uses only the Python standard library. No native
binaries, no shell, no external services.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat as _stat
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import storage


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be created or restored safely."""


class SnapshotTimeout(SnapshotError):
    """Raised when a capture runs past its deadline."""


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

TREE_FORMAT = "atm-tree-v1"
_CHUNK = 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LIST_CAP = 200


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


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


# --------------------------------------------------------------------------
# Object store
# --------------------------------------------------------------------------


def objects_dir() -> Path:
    path = storage.data_dir() / "objects"
    path.mkdir(parents=True, exist_ok=True)
    storage.secure_dir(path)
    return path


def _tmp_dir() -> Path:
    path = objects_dir() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    storage.secure_dir(path)
    return path


def object_path(h: str) -> Path:
    if not isinstance(h, str) or not _HASH_RE.fullmatch(h):
        raise SnapshotError(f"Invalid object hash: {h!r}")
    return objects_dir() / h[:2] / h


class _Store:
    """Per-operation handle on the object store; avoids repeated mkdir/stat noise."""

    def __init__(self) -> None:
        self.root = objects_dir()
        self.tmp = _tmp_dir()
        self._dirs: set[str] = set()

    def path(self, h: str) -> Path:
        return self.root / h[:2] / h

    def exists(self, h: str) -> bool:
        return os.path.exists(self.path(h))

    def put(self, source: Path) -> tuple[str, int, bool]:
        """Store *source*. Returns ``(hash, bytes_read, created)``."""
        digest = hashlib.sha256()
        size = 0
        with open(source, "rb") as src:
            first = src.read(_CHUNK)
            if len(first) < _CHUNK:
                # Small file: hash in memory and skip the write for known content.
                digest.update(first)
                h = digest.hexdigest()
                final = self.path(h)
                if os.path.exists(final):
                    return h, len(first), False
                return self._write(h, final, [first]), len(first), True
            chunks_tmp = self.tmp / f"{uuid.uuid4().hex}.part"
            try:
                with open(chunks_tmp, "wb") as out:
                    chunk = first
                    while chunk:
                        digest.update(chunk)
                        out.write(chunk)
                        size += len(chunk)
                        chunk = src.read(_CHUNK)
                h = digest.hexdigest()
                final = self.path(h)
                if os.path.exists(final):
                    os.unlink(chunks_tmp)
                    return h, size, False
                self._ensure(h)
                os.replace(chunks_tmp, final)
                storage.secure_file(final)
                return h, size, True
            except BaseException:
                try:
                    os.unlink(chunks_tmp)
                except OSError:
                    pass
                raise

    def _ensure(self, h: str) -> None:
        sub = h[:2]
        if sub not in self._dirs:
            d = self.root / sub
            d.mkdir(parents=True, exist_ok=True)
            storage.secure_dir(d)
            self._dirs.add(sub)

    def _write(self, h: str, final: Path, parts: list[bytes]) -> str:
        self._ensure(h)
        tmp = self.tmp / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp, "wb") as out:
                for part in parts:
                    out.write(part)
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        storage.secure_file(final)
        return h


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as src:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------


_PARENT_OK: dict = {}


def _rel_is_valid(workspace_root: Path, rel: object) -> bool:
    """True when *rel* is a safe relative posix path inside *workspace_root*."""
    if not isinstance(rel, str) or not rel or "\0" in rel:
        return False
    for flavour in (PurePosixPath, PureWindowsPath):
        pure = flavour(rel)
        if pure.is_absolute() or pure.anchor or getattr(pure, "drive", ""):
            return False
        if any(part in ("..", ".", "") for part in pure.parts):
            return False
    if any(part in ("..", ".", "") for part in rel.split("/")):
        return False
    if rel.startswith(("/", "\\")):
        return False
    parent = rel.rpartition("/")[0]
    cached = _PARENT_OK.get((workspace_root, parent))
    if cached is None:
        cached = _is_within(workspace_root / parent, workspace_root) if parent else True
        if len(_PARENT_OK) > 50000:
            _PARENT_OK.clear()
        _PARENT_OK[(workspace_root, parent)] = cached
    if not cached:
        return False
    # The leaf itself may be a symlink pointing elsewhere.
    leaf = workspace_root / rel
    if os.path.islink(leaf):
        return _is_within(leaf, workspace_root)
    return True


def _norm_paths(paths: list[str] | None) -> list[str] | None:
    if paths is None:
        return None
    out: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise SnapshotError(f"Invalid path filter: {raw!r}")
        p = raw.replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.rstrip("/")
        if (
            not p
            or p == "."
            or p.startswith("/")
            or PureWindowsPath(p).is_absolute()
            or PureWindowsPath(p).drive
            or ".." in p.split("/")
        ):
            raise SnapshotError(f"Invalid path filter: {raw!r}")
        out.append(p)
    return out


def _matcher(paths: list[str] | None):
    if paths is None:
        return lambda rel: True
    return lambda rel: any(rel == p or rel.startswith(p + "/") for p in paths)


def _is_link(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return False


def _mtime_ns(st: os.stat_result) -> int:
    return st.st_mtime_ns


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def capture(
    workspace: Path,
    excludes: set[str],
    *,
    previous_tree: dict | None = None,
    deadline: float | None = None,
    max_file_bytes: int | None = None,
) -> dict:
    """Walk *workspace* and store new content; return the tree describing it."""
    workspace = Path(workspace)
    prev_files = {}
    if isinstance(previous_tree, dict):
        prev_files = previous_tree.get("files") or {}

    files: dict[str, dict] = {}
    dirs: list[str] = []
    skipped: list[dict] = []
    total_bytes = 0
    new_objects = 0
    new_bytes = 0
    reused = 0
    seen = 0
    store = _Store()

    for root, dnames, fnames in os.walk(workspace, followlinks=False):
        root_path = Path(root)
        keep: list[str] = []
        for name in sorted(dnames):
            if name in excludes:
                continue
            full = os.path.join(root, name)
            rel = (root_path / name).relative_to(workspace).as_posix()
            if _is_link(full):
                skipped.append({"path": rel, "reason": "symlink", "bytes": 0})
                continue
            keep.append(name)
            dirs.append(rel)
        dnames[:] = keep

        for name in sorted(fnames):
            if name in excludes:
                continue
            if deadline is not None and time.monotonic() > deadline:
                timeout = SnapshotTimeout(
                    f"checkpoint exceeded its time budget after {seen} files"
                )
                # Work already done is not wasted: callers persist this as a
                # seed and pass it as previous_tree, so the next attempt only
                # stats these files and carries on where this one stopped.
                timeout.partial = {
                    "format": TREE_FORMAT,
                    "partial": True,
                    "files": files,
                    "files_count": len(files),
                }
                raise timeout
            seen += 1
            full = os.path.join(root, name)
            rel = (root_path / name).relative_to(workspace).as_posix()
            try:
                st = os.lstat(full)
            except OSError:
                skipped.append({"path": rel, "reason": "unreadable", "bytes": 0})
                continue
            if _stat.S_ISLNK(st.st_mode) or _is_link(full):
                skipped.append({"path": rel, "reason": "symlink", "bytes": 0})
                continue
            if not _stat.S_ISREG(st.st_mode):
                skipped.append({"path": rel, "reason": "unreadable", "bytes": 0})
                continue
            if max_file_bytes is not None and st.st_size > max_file_bytes:
                skipped.append({"path": rel, "reason": "too_large", "bytes": st.st_size})
                continue

            mtime = _mtime_ns(st)
            exec_bit = bool(os.name != "nt" and st.st_mode & 0o100)
            prev = prev_files.get(rel)
            if (
                isinstance(prev, dict)
                and prev.get("s") == st.st_size
                and prev.get("m") == mtime
                and isinstance(prev.get("h"), str)
                and _HASH_RE.fullmatch(prev["h"])
                and store.exists(prev["h"])
            ):
                files[rel] = {"h": prev["h"], "s": st.st_size, "m": mtime, "x": exec_bit}
                total_bytes += st.st_size
                reused += 1
                continue

            try:
                h, size, created = store.put(Path(full))
            except OSError:
                skipped.append({"path": rel, "reason": "unreadable", "bytes": 0})
                continue
            if created:
                new_objects += 1
                new_bytes += size
            files[rel] = {"h": h, "s": size, "m": mtime, "x": exec_bit}
            total_bytes += size

    return {
        "format": TREE_FORMAT,
        "files": files,
        "dirs": sorted(dirs),
        "skipped": skipped,
        "bytes": total_bytes,
        "files_count": len(files),
        "new_objects": new_objects,
        "new_bytes": new_bytes,
        "reused": reused,
    }


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def _scan(workspace: Path, excludes: set[str]) -> tuple[dict[str, tuple[str, int, int]], list[str]]:
    """Return ``({rel: (abs, size, mtime_ns)}, [dir rels])``; symlinks are ignored."""
    found: dict[str, tuple[str, int, int]] = {}
    dirs: list[str] = []
    for root, dnames, fnames in os.walk(workspace, followlinks=False):
        root_path = Path(root)
        keep = []
        for name in dnames:
            if name in excludes:
                continue
            full = os.path.join(root, name)
            if _is_link(full):
                continue
            keep.append(name)
            dirs.append((root_path / name).relative_to(workspace).as_posix())
        dnames[:] = keep
        for name in fnames:
            if name in excludes:
                continue
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            rel = (root_path / name).relative_to(workspace).as_posix()
            found[rel] = (full, st.st_size, _mtime_ns(st))
    return found, dirs


def _diff(
    workspace: Path,
    tree: dict,
    excludes: set[str],
    paths: list[str] | None,
) -> tuple[dict, list[dict], dict[str, tuple[str, int, int]], list[str]]:
    _PARENT_OK.clear()
    workspace = Path(workspace)
    root = workspace.resolve()
    match = _matcher(_norm_paths(paths))
    tree_files = tree.get("files") or {}
    skipped_paths = {
        s.get("path") for s in (tree.get("skipped") or []) if isinstance(s, dict)
    }
    disk, disk_dirs = _scan(workspace, excludes)

    modified: list[str] = []
    deleted: list[str] = []
    unchanged = 0
    errors: list[dict] = []
    valid: set[str] = set()

    for rel, meta in tree_files.items():
        if not _rel_is_valid(root, rel):
            errors.append({"path": str(rel), "error": "unsafe path in tree"})
            continue
        valid.add(rel)
        if not match(rel):
            continue
        entry = disk.get(rel)
        if entry is None:
            deleted.append(rel)
            continue
        full, size, mtime = entry
        if size == meta.get("s") and mtime == meta.get("m"):
            unchanged += 1
            continue
        if size != meta.get("s"):
            modified.append(rel)
            continue
        try:
            same = _hash_file(Path(full)) == meta.get("h")
        except OSError:
            same = False
        if same:
            unchanged += 1
        else:
            modified.append(rel)

    added = [
        rel
        for rel in disk
        if rel not in tree_files and rel not in skipped_paths and match(rel)
        and not any(rel.startswith(sp + "/") for sp in skipped_paths if sp)
    ]
    result = {
        "modified": sorted(modified),
        "deleted": sorted(deleted),
        "added": sorted(added),
        "unchanged": unchanged,
    }
    return result, errors, disk, disk_dirs


def diff(
    workspace: Path,
    tree: dict,
    excludes: set[str],
    paths: list[str] | None = None,
) -> dict:
    return _diff(workspace, tree, excludes, paths)[0]


# --------------------------------------------------------------------------
# restore / materialize
# --------------------------------------------------------------------------


def _write_object(
    h: str, target: Path, mtime: int | None, exec_bit: bool, store: "_Store | None" = None
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.atm-tmp")
    try:
        shutil.copyfile(store.path(h) if store else object_path(h), tmp)
        if os.name != "nt" and exec_bit:
            os.chmod(tmp, os.stat(tmp).st_mode | 0o100)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if mtime is not None:
        os.utime(target, ns=(mtime, mtime))


def _check_objects(tree: dict, rels: list[str]) -> None:
    files = tree.get("files") or {}
    store = _Store()
    for rel in rels:
        h = files[rel].get("h")
        if not isinstance(h, str) or not _HASH_RE.fullmatch(h):
            raise SnapshotError(f"Invalid object hash: {h!r}")
        if not store.exists(h):
            raise SnapshotError(f"Snapshot object missing for {rel}: {h}")


def _cap(items: list) -> list:
    return items[:_LIST_CAP]


def restore(
    workspace: Path,
    tree: dict,
    excludes: set[str],
    *,
    paths: list[str] | None = None,
    label: str = "rewind",
) -> dict:
    """Bring *workspace* back to *tree*; replaced and added files go to the trash."""
    workspace = Path(workspace)
    assert_safe(workspace)
    result, errors, disk, _ = _diff(workspace, tree, excludes, paths)
    tree_files = tree.get("files") or {}

    _check_objects(tree, result["modified"] + result["deleted"])

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(label))[:40] or "rewind"
    trash = storage.trash_dir() / f"{safe_label}_{stamp}_{uuid.uuid4().hex[:6]}"
    trash.mkdir(parents=True, exist_ok=False)

    store = _Store()
    restored_ok: list[str] = []
    for rel in result["modified"]:
        target = workspace / rel
        meta = tree_files[rel]
        try:
            dest = trash / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            _write_object(meta["h"], target, meta.get("m"), bool(meta.get("x")), store)
            restored_ok.append(rel)
        except Exception as exc:  # noqa: BLE001 - per-file failures are reported
            errors.append({"path": rel, "error": str(exc)})

    for rel in result["deleted"]:
        meta = tree_files[rel]
        try:
            _write_object(meta["h"], workspace / rel, meta.get("m"), bool(meta.get("x")), store)
            restored_ok.append(rel)
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": rel, "error": str(exc)})

    removed_ok: list[str] = []
    for rel in result["added"]:
        try:
            dest = trash / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(workspace / rel), str(dest))
            removed_ok.append(rel)
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": rel, "error": str(exc)})

    _restore_dirs(workspace, tree, excludes, paths, errors)

    return {
        "restored": len(result["modified"]) + len(result["deleted"]),
        "removed": len(result["added"]),
        "unchanged": result["unchanged"],
        "previous_contents": str(trash),
        "errors": _cap(errors),
        "modified": _cap(result["modified"]),
        "deleted": _cap(result["deleted"]),
        "added": _cap(result["added"]),
    }


def _restore_dirs(
    workspace: Path,
    tree: dict,
    excludes: set[str],
    paths: list[str] | None,
    errors: list[dict],
) -> None:
    _PARENT_OK.clear()
    root = workspace.resolve()
    norm = _norm_paths(paths)
    match = _matcher(norm)
    tree_dirs = [d for d in (tree.get("dirs") or []) if _rel_is_valid(root, d)]
    for rel in sorted(tree_dirs):
        if _is_excluded(Path(rel), excludes):
            continue
        # With a filter, only dirs inside it or leading to it are touched.
        if norm is not None and not match(rel):
            continue
        try:
            (workspace / rel).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append({"path": rel, "error": str(exc)})

    if norm is not None:
        return
    keep = set(tree_dirs)
    kept_files = list((tree.get("files") or {}))
    kept_files += [s.get("path") for s in (tree.get("skipped") or []) if isinstance(s, dict)]
    for rel in kept_files:
        if not _rel_is_valid(root, rel):
            continue
        parent = PurePosixPath(rel).parent
        while parent.parts:
            keep.add(parent.as_posix())
            parent = parent.parent
    for current, dnames, _ in os.walk(workspace, topdown=False, followlinks=False):
        if Path(current) == workspace:
            continue
        rel = Path(current).relative_to(workspace).as_posix()
        if _is_excluded(Path(rel), excludes) or _is_link(current) or rel in keep:
            continue
        try:
            os.rmdir(current)  # fails unless empty
        except OSError:
            pass


def materialize(tree: dict, destination: Path) -> dict:
    """Write the whole tree into a new (or empty) directory."""
    _PARENT_OK.clear()
    destination = Path(destination)
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise SnapshotError(
                f"Fork destination already exists and is not empty: {destination}"
            )
    files = tree.get("files") or {}
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    errors: list[dict] = []
    good: list[str] = []
    for rel in files:
        if _rel_is_valid(root, rel):
            good.append(rel)
        else:
            errors.append({"path": str(rel), "error": "unsafe path in tree"})
    _check_objects(tree, good)

    for rel in tree.get("dirs") or []:
        if not _rel_is_valid(root, rel):
            errors.append({"path": str(rel), "error": "unsafe path in tree"})
            continue
        try:
            (destination / rel).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append({"path": rel, "error": str(exc)})

    written = 0
    store = _Store()
    for rel in good:
        meta = files[rel]
        try:
            _write_object(meta["h"], destination / rel, meta.get("m"), bool(meta.get("x")), store)
            written += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": rel, "error": str(exc)})
    return {"destination": str(destination), "files": written, "errors": _cap(errors)}


# --------------------------------------------------------------------------
# gc
# --------------------------------------------------------------------------


def referenced_hashes(trees: list[dict]) -> set[str]:
    out: set[str] = set()
    for tree in trees:
        for meta in ((tree or {}).get("files") or {}).values():
            h = meta.get("h") if isinstance(meta, dict) else None
            if isinstance(h, str):
                out.add(h)
    return out


def gc(referenced: set[str]) -> dict:
    removed = 0
    freed = 0
    base = objects_dir()
    for sub in list(base.iterdir()):
        if sub.name == "tmp" or not sub.is_dir():
            continue
        for obj in list(sub.iterdir()):
            if not _HASH_RE.fullmatch(obj.name) or obj.name in referenced:
                continue
            try:
                size = obj.stat().st_size
                obj.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
        try:
            sub.rmdir()  # only succeeds when empty
        except OSError:
            pass
    cutoff = time.time() - 3600
    tmp = base / "tmp"
    if tmp.is_dir():
        for part in list(tmp.iterdir()):
            try:
                if part.is_file() and part.stat().st_mtime < cutoff:
                    size = part.stat().st_size
                    part.unlink()
                    removed += 1
                    freed += size
            except OSError:
                continue
    return {"removed_objects": removed, "freed_bytes": freed}
