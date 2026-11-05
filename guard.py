"""Hook callbacks.

`pre_tool_call` takes a checkpoint before a risky call and before the first
file-changing call of each turn. It never blocks a tool and never bypasses
Hermes approvals; if anything fails, it fails open.
"""

from __future__ import annotations

import threading
import time

from . import effects, handlers, policy, storage

_MUTATION_LOCK = threading.Lock()
_MUTATION_TURNS: list[str] = []
_MUTATION_TURNS_MAX = 500
_LAST_MUTATION_TS = 0.0


def _claim_turn(key: str) -> bool:
    """Return True the first time a turn asks for a mutation checkpoint."""
    with _MUTATION_LOCK:
        if key in _MUTATION_TURNS:
            return False
        _MUTATION_TURNS.append(key)
        if len(_MUTATION_TURNS) > _MUTATION_TURNS_MAX:
            del _MUTATION_TURNS[0 : len(_MUTATION_TURNS) - _MUTATION_TURNS_MAX]
        return True


def _claim_cooldown(seconds: float) -> bool:
    """Fallback when the host does not provide turn or session ids."""
    global _LAST_MUTATION_TS
    now = time.monotonic()
    with _MUTATION_LOCK:
        if seconds > 0 and now - _LAST_MUTATION_TS < seconds:
            return False
        _LAST_MUTATION_TS = now
        return True


def _reset_for_tests() -> None:
    global _LAST_MUTATION_TS
    with _MUTATION_LOCK:
        _MUTATION_TURNS.clear()
        _LAST_MUTATION_TS = 0.0


def pre_tool_call(tool_name=None, args=None, **kwargs):
    try:
        config = handlers.config()
        if not config.get("auto_checkpoint", True):
            return None
        if not tool_name or str(tool_name).startswith("atm_"):
            return None

        workspace = handlers.workspace()
        score, reasons = policy.evaluate(
            str(tool_name),
            args or {},
            workspace,
            config.get("extra_risky_patterns"),
        )
        threshold = float(config.get("risk_threshold", 0.6) or 0.6)

        trigger = None
        if score >= threshold:
            trigger = {
                "type": "risk",
                "tool": tool_name,
                "score": round(score, 3),
                "reasons": reasons,
            }
        elif config.get("auto_checkpoint_mutations", True) and policy.is_mutating(
            str(tool_name), args or {}
        ):
            turn_key = str(kwargs.get("turn_id") or kwargs.get("session_id") or "")
            if turn_key:
                if _claim_turn(turn_key):
                    trigger = {
                        "type": "mutation",
                        "tool": tool_name,
                        "turn_id": turn_key,
                    }
            else:
                cooldown = float(config.get("mutation_cooldown_seconds", 120) or 0)
                if _claim_cooldown(cooldown):
                    trigger = {"type": "mutation", "tool": tool_name}

        if trigger is None:
            return None

        trigger["session_id"] = kwargs.get("session_id")
        trigger["turn_id"] = kwargs.get("turn_id")
        handlers.create_checkpoint(f"auto:{tool_name}", trigger, auto=True)
    except Exception:
        # Hooks are fail-open: a checkpoint problem must never stop the agent.
        return None
    return None


def post_tool_call(tool_name=None, args=None, status=None, **kwargs):
    try:
        if status in ("blocked", "cancelled"):
            return None
        if not tool_name or str(tool_name).startswith("atm_"):
            return None

        workspace = handlers.workspace()
        for effect in effects.classify(str(tool_name), args or {}, workspace):
            storage.append_jsonl(
                storage.effects_path(),
                {
                    "ts": storage.now_iso(),
                    "tool": tool_name,
                    "session_id": kwargs.get("session_id"),
                    "checkpoint_id": handlers.last_checkpoint(),
                    **effect,
                },
            )
    except Exception:
        return None
    return None
