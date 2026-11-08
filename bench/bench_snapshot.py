"""Benchmark for the snapshot engine. Run: python bench/bench_snapshot.py"""

from __future__ import annotations

import importlib.util
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
    spec = importlib.util.spec_from_file_location(
        "hermes_time_machine",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_time_machine"] = module
    spec.loader.exec_module(module)
    return module


def timed(fn):
    start = time.perf_counter()
    out = fn()
    return time.perf_counter() - start, out


def main() -> None:
    pkg = _load_package()
    snapshot = pkg.snapshot
    base = Path(tempfile.mkdtemp(prefix="atm_bench_"))
    try:
        os.environ["ATM_DATA_DIR"] = str(base / "data")
        ws = base / "ws"
        rng = random.Random(7)
        target = 50 * 1024 * 1024
        total = 0
        count = 0
        paths: list[Path] = []
        while count < 2000:
            if count < 25:
                size = rng.randint(1024 * 1024, 2 * 1024 * 1024)
            else:
                size = rng.randint(1024, 8 * 1024)
            p = ws / f"d{count % 40}" / f"s{count % 7}" / f"f{count}.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(os.urandom(size))
            paths.append(p)
            total += size
            count += 1
        while total < target:  # top up to ~50 MB with a few big files
            p = ws / "big" / f"b{count}.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            size = rng.randint(1024 * 1024, 2 * 1024 * 1024)
            p.write_bytes(os.urandom(size))
            total += size
            count += 1
        nm = ws / "node_modules" / "dep"
        nm.mkdir(parents=True)
        for i in range(300):
            (nm / f"m{i}.js").write_bytes(os.urandom(2048))
        excludes = snapshot.excludes_for({})

        rows = []
        t, tree = timed(lambda: snapshot.capture(ws, excludes))
        rows.append(("first capture", t, f"{tree['files_count']} files, {tree['bytes'] / 1e6:.1f} MB"))
        t, tree2 = timed(lambda: snapshot.capture(ws, excludes, previous_tree=tree))
        rows.append(("second capture (no changes)", t, f"reused {tree2['reused']}, new {tree2['new_objects']}"))
        changed = rng.sample(paths, 10)
        for p in changed:
            p.write_bytes(os.urandom(4096))
        t, tree3 = timed(lambda: snapshot.capture(ws, excludes, previous_tree=tree2))
        rows.append(("capture after 10 changes", t, f"new objects {tree3['new_objects']}"))
        t, d = timed(lambda: snapshot.diff(ws, tree2, excludes))
        rows.append(("diff", t, f"{len(d['modified'])} modified"))
        t, r = timed(lambda: snapshot.restore(ws, tree2, excludes))
        rows.append(("restore of 10 files", t, f"restored {r['restored']}, errors {len(r['errors'])}"))
        t, m = timed(lambda: snapshot.materialize(tree2, base / "fork"))
        rows.append(("materialize", t, f"{m['files']} files"))

        print("| step | seconds | detail |")
        print("|---|---|---|")
        for name, secs, detail in rows:
            print(f"| {name} | {secs:.3f} | {detail} |")
    finally:
        os.environ.pop("ATM_DATA_DIR", None)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
