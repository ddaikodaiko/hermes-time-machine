"""Plugin configuration loading across Hermes versions.

Hermes 0.19.x has no ``ctx.get_config()``; settings live under
``plugins.entries.<plugin>.settings`` (or directly under the entry) in
``config.yaml``. Newer builds expose ``ctx.get_config()``. Support both so the
same plugin works before and after the API lands.
"""

from __future__ import annotations

PLUGIN_ID = "hermes-time-machine"

DEFAULTS = {
    "workspace": "",
    "auto_checkpoint": True,
    "auto_checkpoint_mutations": True,
    "mutation_cooldown_seconds": 120,
    "risk_threshold": 0.6,
    "max_snapshot_mb": 200,
    "retention": 20,
    "exclude_git": False,
    "extra_risky_patterns": [],
}


def _from_config_file() -> dict:
    try:
        from hermes_cli.config import load_config  # type: ignore

        raw = load_config() or {}
        entries = (raw.get("plugins") or {}).get("entries") or {}
        entry = entries.get(PLUGIN_ID) or {}
        if not isinstance(entry, dict):
            return {}
        merged = {key: value for key, value in entry.items() if key != "settings"}
        settings = entry.get("settings")
        if isinstance(settings, dict):
            merged.update(settings)
        return merged
    except Exception:
        return {}


def load(ctx=None) -> dict:
    """Return the effective settings, defaults filled in."""
    config = dict(DEFAULTS)

    candidate: dict = {}
    if ctx is not None:
        getter = getattr(ctx, "get_config", None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, dict):
                    candidate = value
            except Exception:
                candidate = {}

    if not candidate:
        candidate = _from_config_file()

    for key, value in candidate.items():
        if key in DEFAULTS:
            config[key] = value

    # Coerce common YAML/string mistakes instead of failing at runtime.
    try:
        config["risk_threshold"] = max(0.0, min(float(config["risk_threshold"]), 1.0))
    except (TypeError, ValueError):
        config["risk_threshold"] = DEFAULTS["risk_threshold"]
    for key in ("max_snapshot_mb", "retention", "mutation_cooldown_seconds"):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError):
            config[key] = DEFAULTS[key]
    for key in ("auto_checkpoint", "auto_checkpoint_mutations", "exclude_git"):
        config[key] = bool(config[key])
    if not isinstance(config["extra_risky_patterns"], list):
        config["extra_risky_patterns"] = []

    return config
