"""Hook callbacks.

* ``pre_tool_call``: takes a checkpoint before a risky call and before the first
  file-changing call of each turn, and escalates irreversible external effects
  to Hermes' own human-approval gate (the point-of-no-return gate). A restore
  requested by the model (``atm_rewind`` with ``confirm=true``) always asks the
  human too. It never approves anything and never blocks by itself.
* ``post_tool_call``: records every irreversible effect that actually
  succeeded in the effect ledger, and tracks failing streaks.
* ``transform_tool_result``: tells the model where its restore point is.
* ``pre_llm_call``: effect memory, a reminder of irreversible effects that are
  no longer visible in the conversation.

Hermes 0.23 fails CLOSED when ``pre_tool_call`` raises or runs past 30 s, so
every hook swallows its own exceptions and ``pre_tool_call`` is time-bounded.
"""

from __future__ import annotations

import json
import threading
import time

from . import effects, gate, handlers, memory, policy, storage

_LOCK = threading.Lock()
_MUTATION_TURNS: list[str] = []
_MUTATION_TURNS_MAX = 500
_LAST_MUTATION_TS = 0.0

_MAX_ENTRIES = 500
_PENDING_TTL = 3600.0
_PENDING: dict = {}  # key -> (monotonic ts, effects)
_AUTO_CKPT: dict = {}  # tool_call_id -> checkpoint id
_STRUGGLE: dict = {}  # session_id -> {"count", "ckpt", "fired"}
_HINTS: dict = {}  # session_id -> hint text
_NOTIFIED: dict = {}  # session_id -> set of effect keys already reminded
_LINEAGE: dict = {}  # new session id -> old session id (compaction rotation)
_LINEAGE_MAX = 2000
_LINEAGE_DEPTH = 20

_CHECKPOINT_NOTE = "Restore point taken before this call. atm_rewind previews or restores it."


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _claim_turn(key: str) -> bool:
    """Return True the first time a turn asks for a mutation checkpoint."""
    with _LOCK:
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
    with _LOCK:
        if seconds > 0 and now - _LAST_MUTATION_TS < seconds:
            return False
        _LAST_MUTATION_TS = now
        return True


def _reset_for_tests() -> None:
    global _LAST_MUTATION_TS
    with _LOCK:
        _MUTATION_TURNS.clear()
        _LAST_MUTATION_TS = 0.0
        _PENDING.clear()
        _AUTO_CKPT.clear()
        _STRUGGLE.clear()
        _HINTS.clear()
        _NOTIFIED.clear()
        _LINEAGE.clear()


def _bounded_put(store: dict, key, value) -> None:
    store[key] = value
    while len(store) > _MAX_ENTRIES:
        store.pop(next(iter(store)), None)


def _command(args) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _pending_key(tool_name, kwargs, args):
    tool_call_id = kwargs.get("tool_call_id")
    if tool_call_id:
        return str(tool_call_id)
    return (str(kwargs.get("session_id") or ""), str(tool_name), _command(args))


def _stash(key, found: list) -> None:
    now = time.monotonic()
    with _LOCK:
        for stale in [k for k, (ts, _) in _PENDING.items() if now - ts > _PENDING_TTL]:
            _PENDING.pop(stale, None)
        _bounded_put(_PENDING, key, (now, found))


def _unstash(key):
    with _LOCK:
        entry = _PENDING.pop(key, None)
    return entry[1] if entry else None


def _safe_journal(record: dict) -> None:
    try:
        handlers.record_journal(record)
    except Exception:
        pass


def _is_irreversible(found) -> bool:
    return any(isinstance(item, dict) and item.get("reversible") is False for item in found or [])


# ---------------------------------------------------------------------------
# pre_tool_call
# ---------------------------------------------------------------------------


def _decide_trigger(tool_name, args, config, ws, kwargs) -> dict | None:
    score, reasons = policy.evaluate(
        str(tool_name), args, ws, config.get("extra_risky_patterns")
    )
    threshold = float(config.get("risk_threshold", 0.6) or 0.6)
    if score >= threshold:
        return {"type": "risk", "tool": tool_name, "score": round(score, 3), "reasons": reasons}
    if config.get("auto_checkpoint_mutations", True) and policy.is_mutating(str(tool_name), args):
        turn_key = str(kwargs.get("turn_id") or kwargs.get("session_id") or "")
        if turn_key:
            if _claim_turn(turn_key):
                return {"type": "mutation", "tool": tool_name, "turn_id": turn_key}
        elif _claim_cooldown(float(config.get("mutation_cooldown_seconds", 120) or 0)):
            return {"type": "mutation", "tool": tool_name}
    return None


def _reset_struggle(session_id, checkpoint_id) -> None:
    with _LOCK:
        _STRUGGLE[str(session_id or "")] = {"count": 0, "ckpt": checkpoint_id, "fired": False}


def _auto_checkpoint(tool_name, args, config, kwargs, session) -> str | None:
    """Take an automatic checkpoint when warranted; return its id (or None)."""
    ws = handlers.safe_workspace()
    if ws is None:
        return None
    trigger = _decide_trigger(tool_name, args, config, ws, kwargs)
    if trigger is None:
        return None
    trigger["session_id"] = kwargs.get("session_id")
    trigger["turn_id"] = kwargs.get("turn_id")
    manifest = handlers.create_checkpoint(f"auto:{tool_name}", trigger, auto=True)
    if not manifest:
        return None
    checkpoint_id = manifest["id"]
    tool_call_id = kwargs.get("tool_call_id")
    if tool_call_id:
        with _LOCK:
            _bounded_put(_AUTO_CKPT, str(tool_call_id), checkpoint_id)
    _reset_struggle(kwargs.get("session_id"), checkpoint_id)
    return checkpoint_id


_REWIND_GENERIC = (
    "Restore workspace (hermes-time-machine)\n"
    "The agent asked to restore the workspace to a checkpoint. Files changed since then "
    "will be reverted (previous contents are kept in the plugin trash). "
    "Preview unavailable; use /timemachine rewind to inspect."
)


def _rewind_message(args, kwargs) -> str:
    """Plain-text approval message for a model-driven restore (<= gate.MAX_MESSAGE chars)."""
    manifest = handlers.resolve(args.get("checkpoint_id"))
    if not manifest:
        return _REWIND_GENERIC
    paths = handlers._clean_paths(args.get("paths"))
    counts = handlers.preview_changes(manifest, paths).get("counts") or {}
    blocking = handlers.irreversible_since(manifest, kwargs.get("session_id"))
    when = str(manifest.get("created_at") or "")
    when = when[11:16] if len(when) >= 16 else when
    label = manifest.get("label") or "checkpoint"
    parts = []
    for key, word in (("modified", "modified"), ("deleted", "deleted (will be restored)"), ("added", "added")):
        n = int(counts.get(key) or 0)
        if n:
            parts.append(f"{n} {word}")
    summary = ", ".join(parts) if parts else "no file differences"
    lines = [
        "Restore workspace (hermes-time-machine)",
        f"Checkpoint {manifest.get('id')} ({label}, {when}): {summary}"
        + (" file(s) will be reverted; new files go to the trash." if parts else "."),
    ]
    changes = handlers.preview_changes(manifest, paths)
    names = [str(n) for k in ("modified", "deleted", "added") for n in (changes.get(k) or [])][:3]
    if names:
        lines.append("Files: " + ", ".join(names))
    if blocking:
        shown = []
        for e in blocking[:2]:
            kind = str(e.get("kind") or "effect").replace("_", " ")
            shown.append(kind + (f" -> {e['target']}" if e.get("target") else ""))
        more = f" (+{len(blocking) - 2} more)" if len(blocking) > 2 else ""
        lines.append("Still true outside (not undone): " + "; ".join(shown) + more + ".")
        comp = next((str(e.get("compensation")) for e in blocking if e.get("compensation")), "")
        if comp:
            lines.append("To revert: " + comp)
    lines.append("Previous contents are kept in the plugin trash.")
    return gate._clip("\n".join(lines), gate.MAX_MESSAGE)


def _rewind_directive(args, kwargs) -> dict:
    """Approval for a model-callable restore. No rule_key: the human approves every one."""
    try:
        message = _rewind_message(args, kwargs)
    except Exception as exc:
        _safe_journal({"type": "hook_error", "hook": "pre_tool_call.rewind_preview", "error": str(exc)[:300]})
        message = _REWIND_GENERIC
    return {"action": "approve", "message": message}


def pre_tool_call(tool_name=None, args=None, **kwargs):
    try:
        if str(tool_name or "") == "atm_rewind":
            # A restore overwrites workspace files: a model-driven one always needs the human.
            if isinstance(args, dict) and handlers._flag(args.get("confirm")):
                return _rewind_directive(args, kwargs)
            return None
        if not tool_name or str(tool_name).startswith("atm_"):
            return None
        config = handlers.config()
        args = args if isinstance(args, dict) else {}
        session = handlers.session_dir()

        found = effects.preflight(str(tool_name), args, session)

        checkpoint_id = None
        if config.get("auto_checkpoint", True):
            try:
                checkpoint_id = _auto_checkpoint(tool_name, args, config, kwargs, session)
            except Exception as exc:
                _safe_journal({"type": "hook_error", "hook": "pre_tool_call.checkpoint", "error": str(exc)[:300]})

        _stash(_pending_key(tool_name, kwargs, args), found)

        if not _is_irreversible(found):
            return None
        mode = config.get("irreversible_gate", "approve")
        if mode == "warn":
            _safe_journal(
                {
                    "type": "gate_warning",
                    "tool": tool_name,
                    "session_id": kwargs.get("session_id"),
                    "effects": [
                        {"kind": e.get("kind"), "target": e.get("target")}
                        for e in found
                        if e.get("reversible") is False
                    ],
                }
            )
            return None
        return gate.decide(
            found,
            config,
            checkpoint_id=checkpoint_id or handlers.workspace_checkpoint(),
            command=_command(args),
        )
    except Exception as exc:
        # Hermes 0.23 blocks the tool when this callback raises: never raise.
        _safe_journal({"type": "hook_error", "hook": "pre_tool_call", "error": str(exc)[:300]})
        return None


# ---------------------------------------------------------------------------
# post_tool_call
# ---------------------------------------------------------------------------


def _call_failed(tool_name, result, status) -> bool:
    if status == "error":
        return True
    if str(tool_name) in effects.TERMINAL_TOOLS:
        return effects.outcome(str(tool_name), result).get("status") == "failed"
    return False


def _hint_text(session_id, count) -> str | None:
    with _LOCK:
        checkpoint_id = (_STRUGGLE.get(session_id) or {}).get("ckpt")
    checkpoint_id = checkpoint_id or handlers.last_checkpoint()
    if not checkpoint_id:
        return None
    manifest = handlers.get_checkpoint(checkpoint_id) or {}
    label = manifest.get("label") or "checkpoint"
    return (
        f"{count} failing calls since checkpoint {checkpoint_id} ({label}). "
        "If the workspace is in a bad state, "
        f'atm_rewind(checkpoint_id="{checkpoint_id}") restores it in one call '
        "instead of more debugging."
    )


def _track_struggle(tool_name, result, status, session_id, config) -> None:
    sid = str(session_id or "")
    failed = _call_failed(tool_name, result, status)
    threshold = int(config.get("struggle_threshold", 4) or 0)
    with _LOCK:
        state = _STRUGGLE.setdefault(sid, {"count": 0, "ckpt": None, "fired": False})
        state["count"] = state["count"] + 1 if failed else 0
        count, fired = state["count"], state["fired"]
    if threshold <= 0 or not failed or fired or count < threshold:
        return
    text = _hint_text(sid, count)
    if not text:
        return
    with _LOCK:
        _STRUGGLE[sid]["fired"] = True
        _bounded_put(_HINTS, sid, text)


def post_tool_call(tool_name=None, args=None, result=None, status=None, **kwargs):
    try:
        if status in ("blocked", "cancelled"):
            return None
        if not tool_name or str(tool_name).startswith("atm_"):
            return None
        config = handlers.config()
        args = args if isinstance(args, dict) else {}
        session = handlers.session_dir()

        found = _unstash(_pending_key(tool_name, kwargs, args))
        if found is None:
            found = effects.classify(str(tool_name), args, session)

        tool_call_id = kwargs.get("tool_call_id")
        with _LOCK:
            checkpoint_id = _AUTO_CKPT.get(str(tool_call_id)) if tool_call_id else None
        checkpoint_id = checkpoint_id or handlers.last_checkpoint()
        workspace_key = handlers.workspace_key()

        for effect in found:
            try:
                final = effects.finalize(effect, result, session)
                outcome = final.get("outcome") or {}
                failed = outcome.get("status") == "failed" or status == "error"
                handlers.append_effect(
                    {
                        **final,
                        "ts": storage.now_iso(),
                        "tool": tool_name,
                        "session_id": kwargs.get("session_id"),
                        "turn_id": kwargs.get("turn_id"),
                        "tool_call_id": tool_call_id,
                        "checkpoint_id": checkpoint_id,
                        "workspace": workspace_key,
                        "status": "failed" if failed else "succeeded",
                        "exit_code": outcome.get("exit_code"),
                    }
                )
            except Exception as exc:
                _safe_journal({"type": "hook_error", "hook": "post_tool_call.record", "error": str(exc)[:300]})

        _track_struggle(tool_name, result, status, kwargs.get("session_id"), config)
    except Exception as exc:
        _safe_journal({"type": "hook_error", "hook": "post_tool_call", "error": str(exc)[:300]})
    return None


# ---------------------------------------------------------------------------
# transform_tool_result
# ---------------------------------------------------------------------------


def transform_tool_result(tool_name=None, args=None, result=None, **kwargs):
    try:
        config = handlers.config()
        if not config.get("annotate_results", True):
            return None
        if not tool_name or str(tool_name).startswith("atm_"):
            return None

        tool_call_id = kwargs.get("tool_call_id")
        sid = str(kwargs.get("session_id") or "")
        note: dict = {}
        with _LOCK:
            checkpoint_id = _AUTO_CKPT.pop(str(tool_call_id), None) if tool_call_id else None
            hint = _HINTS.pop(sid, None)
        if checkpoint_id:
            note["checkpoint"] = checkpoint_id
            note["note"] = _CHECKPOINT_NOTE
        if hint:
            note["hint"] = hint
        if not note:
            return None

        if isinstance(result, dict):
            return json.dumps({**result, "time_machine": note}, ensure_ascii=False, default=str)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                parsed["time_machine"] = note
                return json.dumps(parsed, ensure_ascii=False)
            parts = []
            if checkpoint_id:
                parts.append(f"{_CHECKPOINT_NOTE} (checkpoint {checkpoint_id})")
            if hint:
                parts.append(hint)
            text = " ".join(parts)
            return f"{result}\n\n[time-machine] {text}"
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# pre_llm_call (effect memory)
# ---------------------------------------------------------------------------


def _lineage_path():
    return storage.data_dir() / "lineage.json"


def _read_lineage_file() -> dict:
    try:
        data = json.loads(_lineage_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v and isinstance(v, str)}


def post_compaction(**kwargs):
    """Remember that a compaction rotated the session id (new -> old)."""
    try:
        new_id = str(kwargs.get("session_id") or "")
        old_id = str(kwargs.get("old_session_id") or "")
        if not new_id or not old_id or new_id == old_id:
            return None
        with _LOCK:
            disk = _read_lineage_file()
            disk.pop(new_id, None)
            disk[new_id] = old_id
            while len(disk) > _LINEAGE_MAX:
                disk.pop(next(iter(disk)), None)
            _LINEAGE.pop(new_id, None)
            _LINEAGE[new_id] = old_id
            while len(_LINEAGE) > _LINEAGE_MAX:
                _LINEAGE.pop(next(iter(_LINEAGE)), None)
            try:
                storage.write_json_atomic(_lineage_path(), disk)
            except Exception as exc:
                _safe_journal({"type": "hook_error", "hook": "post_compaction.persist", "error": str(exc)[:300]})
    except Exception as exc:
        _safe_journal({"type": "hook_error", "hook": "post_compaction", "error": str(exc)[:300]})
    return None


def lineage(session_id) -> list[str]:
    """The session id followed by its ancestors (compaction chain, depth-limited, cycle-safe)."""
    sid = str(session_id or "")
    if not sid:
        return []
    with _LOCK:
        known = {**_read_lineage_file(), **_LINEAGE}
    chain = [sid]
    current = sid
    while len(chain) <= _LINEAGE_DEPTH:
        parent = known.get(current)
        if not parent or parent in chain:
            break
        chain.append(parent)
        current = parent
    return chain


def pre_llm_call(**kwargs):
    try:
        config = handlers.config()
        if not config.get("effect_memory", True):
            return None
        session_id = kwargs.get("session_id")
        history = kwargs.get("conversation_history")
        if not session_id or not isinstance(history, list):
            return None
        sid = str(session_id)
        records = handlers.read_ledger()
        with _LOCK:
            already = _NOTIFIED.setdefault(sid, set())
            snapshot_already = set(already)
        # Only the compaction lineage recorded by post_compaction counts. Hermes also
        # passes parent_session_id for delegated subagents, which must NOT inherit the
        # parent's effects (a fresh subagent was never "rewound").
        ids = set(lineage(sid))
        items = memory.forgotten(ids, history, records, snapshot_already)
        if not items:
            return None
        text = memory.render(items)
        if not text:
            return None
        with _LOCK:
            already = _NOTIFIED.setdefault(sid, set())
            already.update(memory.key_of(item) for item in items)
            if len(already) > _MAX_ENTRIES:
                for old in list(already)[: len(already) - _MAX_ENTRIES]:
                    already.discard(old)
            while len(_NOTIFIED) > _MAX_ENTRIES:
                _NOTIFIED.pop(next(iter(_NOTIFIED)), None)
        return {"context": text}
    except Exception as exc:
        _safe_journal({"type": "hook_error", "hook": "pre_llm_call", "error": str(exc)[:300]})
        return None
