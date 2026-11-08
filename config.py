"""Plugin configuration loading across Hermes versions.

Hermes 0.23 reads settings with ``ctx.get_config(key, default=...)`` from
``plugins.entries.<plugin>.settings``. Builds without a keyed ``get_config``
fall back to reading ``config.yaml`` directly.
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
    "irreversible_gate": "approve",
    "gate_allow_patterns": [],
    "auto_budget_seconds": 5.0,
    "max_file_mb": 50,
    "effect_memory": True,
    "struggle_threshold": 4,
    "annotate_results": True,
}

GATE_MODES = ("approve", "warn", "off")

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def _as_bool(value) -> bool:
    """Coerce YAML/string/CLI values without ``bool("false") is True``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE:
            return True
        if normalized in _FALSE:
            return False
    return bool(value)


def _as_int(value, default: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _as_patterns(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def normalize(candidate: dict | None) -> dict:
    """Return a complete, type-safe settings dict."""
    config = dict(DEFAULTS)
    for key, value in (candidate or {}).items():
        if key in DEFAULTS:
            config[key] = value

    try:
        config["risk_threshold"] = max(0.0, min(float(config["risk_threshold"]), 1.0))
    except (TypeError, ValueError):
        config["risk_threshold"] = DEFAULTS["risk_threshold"]

    # A retention of 0 would delete the checkpoint that was just created.
    config["max_snapshot_mb"] = _as_int(
        config["max_snapshot_mb"], DEFAULTS["max_snapshot_mb"], 1
    )
    config["retention"] = _as_int(config["retention"], DEFAULTS["retention"], 1)
    config["mutation_cooldown_seconds"] = _as_int(
        config["mutation_cooldown_seconds"],
        DEFAULTS["mutation_cooldown_seconds"],
        0,
    )

    config["max_file_mb"] = _as_int(config["max_file_mb"], DEFAULTS["max_file_mb"], 1)
    config["struggle_threshold"] = _as_int(
        config["struggle_threshold"], DEFAULTS["struggle_threshold"], 0
    )
    try:
        budget = float(config["auto_budget_seconds"])
        if budget != budget:  # NaN
            raise ValueError
        config["auto_budget_seconds"] = max(0.5, min(budget, 20.0))
    except (TypeError, ValueError):
        config["auto_budget_seconds"] = DEFAULTS["auto_budget_seconds"]

    mode = str(config["irreversible_gate"] or "").strip().lower()
    config["irreversible_gate"] = mode if mode in GATE_MODES else "approve"
    config["gate_allow_patterns"] = _as_patterns(config["gate_allow_patterns"])

    for key in (
        "auto_checkpoint",
        "auto_checkpoint_mutations",
        "exclude_git",
        "effect_memory",
        "annotate_results",
    ):
        config[key] = _as_bool(config[key])

    if config["workspace"] is None:
        config["workspace"] = ""
    elif not isinstance(config["workspace"], str):
        config["workspace"] = str(config["workspace"])

    config["extra_risky_patterns"] = _as_patterns(config["extra_risky_patterns"])
    return config


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
    """Return the effective settings, defaults filled in.

    Hermes 0.23 exposes ``ctx.get_config(key, default=...)``. Builds without a
    keyed ``get_config`` fall back to reading ``config.yaml`` directly.
    """
    candidate: dict = {}
    keyed = False
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    if callable(getter):
        try:
            for key, default in DEFAULTS.items():
                fallback = list(default) if isinstance(default, list) else default
                candidate[key] = getter(key, default=fallback)
            keyed = True
        except TypeError:
            candidate = {}
        except Exception:
            candidate = {}
            keyed = True  # the API exists but failed; defaults are safer than guessing
    if not keyed:
        candidate = _from_config_file()
    return normalize(candidate)
