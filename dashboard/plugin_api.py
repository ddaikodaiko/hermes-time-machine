"""hermes-time-machine dashboard backend, mounted at /api/plugins/hermes-time-machine/.

Read-only: three GET routes over the plugin's own checkpoint manifests, effect ledger and
the protected workspace. No route restores, writes, or runs anything.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

_PKG = "hermes_time_machine_dashboard"
_ROOT = Path(__file__).resolve().parents[1]


def _data():
    """The plugin package's ``dashboard_data`` module, loaded from this plugin's own folder."""
    module = sys.modules.get(f"{_PKG}.dashboard_data")
    if module is not None:
        return module
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PKG, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = package
        try:
            spec.loader.exec_module(package)
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
    return importlib.import_module(f"{_PKG}.dashboard_data")


@router.get("/overview")
def overview(workspace: str | None = None):
    return _data().overview(workspace)


@router.get("/checkpoint/{checkpoint_id}")
def checkpoint(checkpoint_id: str):
    return _data().checkpoint(checkpoint_id)


@router.get("/diff/{checkpoint_id}")
def diff(checkpoint_id: str, path: str = ""):
    return _data().file_diff(checkpoint_id, path)
