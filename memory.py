"""Effect memory.

``/rollback``, ``/undo``, ``/retry`` and context compaction make the *model*
forget what it did; the remote does not forget. If a recorded irreversible
effect of this session is no longer visible in the conversation the model is
about to see, a short reminder is injected so it does not repeat the action or
assume it never happened.

Pure functions, stdlib only, never raise.
"""

from __future__ import annotations

import json
from datetime import datetime

MAX_CHARS = 1500
MAX_ITEMS = 5

HEADER = (
    "[hermes-time-machine] Effect memory: the conversation was rewound or compacted, "
    "but these actions already happened outside the workspace and were NOT undone:"
)
FOOTER = (
    "Treat them as done. Do not repeat them, and do not assume they never happened. "
    "If the user wants them reverted, propose the command above; it goes through approval."
)
FOOTER_NO_COMMAND = (
    "Treat them as done. Do not repeat them, and do not assume they never happened. "
    "Nothing can undo them automatically; if the user wants them reverted, work out the "
    "compensation with them first."
)


def _args_text(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def visible_ids(history) -> tuple[set[str], str]:
    """Return ``(tool_call ids, concatenated assistant tool_call arguments)`` of the history."""
    ids: set[str] = set()
    chunks: list[str] = []
    try:
        for message in history or []:
            if not isinstance(message, dict):
                continue
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                ids.add(tool_call_id)
            calls = message.get("tool_calls")
            if not isinstance(calls, (list, tuple)):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id:
                    ids.add(call_id)
                function = call.get("function")
                if isinstance(function, dict):
                    chunks.append(_args_text(function.get("arguments", "")))
    except Exception:
        pass
    return ids, "\n".join(chunks)


def key_of(record: dict) -> str:
    return f"{record.get('ts')}|{record.get('tool_call_id')}"


def _is_candidate(record, session_ids) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("session_id") not in session_ids or record.get("reversible", True):
        return False
    return record.get("status") in (None, "succeeded")


def _evidence_visible(evidence, text: str) -> bool:
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    needle = evidence.strip()
    if needle in text:
        return True
    try:
        return json.dumps(needle, ensure_ascii=False)[1:-1] in text
    except Exception:
        return False


def forgotten(session_ids, history, records, already) -> list[dict]:
    """Irreversible effects of the given session(s) that the visible history no longer shows.

    *session_ids* is a set of ids (a session and its compaction ancestors) or a single id.
    """
    try:
        if isinstance(session_ids, str):
            session_ids = {session_ids}
        session_ids = {str(s) for s in (session_ids or ()) if s}
        if not session_ids or not isinstance(history, list) or not history:
            return []
        ids, text = visible_ids(history)
        seen = already if already is not None else set()
        out = []
        for record in records or []:
            if not _is_candidate(record, session_ids):
                continue
            tool_call_id = record.get("tool_call_id")
            if tool_call_id:
                visible = tool_call_id in ids
            else:
                visible = _evidence_visible(record.get("evidence"), text)
            if visible or key_of(record) in seen:
                continue
            out.append(record)
        return out
    except Exception:
        return []


def _clock(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%H:%M")
    except Exception:
        return "an earlier time"


def _short(sha) -> str:
    return str(sha or "")[:7]


def _line(index: int, effect: dict, comp_limit: int = 360) -> str:
    kind = str(effect.get("kind") or "effect")
    label = "git push" if kind == "git_push" else kind.replace("_", " ")
    target = effect.get("target")
    head = f"{index}. {label}" + (f" → {target}" if target else "")
    head += f" at {_clock(effect.get('ts'))}"
    pre = effect.get("pre_state") if isinstance(effect.get("pre_state"), dict) else {}
    local, before = _short(pre.get("local_sha")), _short(pre.get("remote_sha_before"))
    if local and before and local != before:
        head += f" ({before}..{local})"
    elif local:
        head += f" ({local})"
    comp = effect.get("compensation")
    if comp:
        tail = f" To revert if the user asks: {str(comp)[:comp_limit]}"
    else:
        note = effect.get("compensation_note") or effect.get("reason") or "No automatic undo."
        tail = f" No automatic undo. {str(note)[:200]}"
    return head + "." + tail


def render(effects) -> str:
    """Plain-text reminder, at most 1,500 characters."""
    try:
        items = [e for e in (effects or []) if isinstance(e, dict)]
        if not items:
            return ""
        shown = items[:MAX_ITEMS]
        extra = len(items) - len(shown)
        fixed = len(HEADER) + len(FOOTER) + 3 + (40 if extra else 0)
        per = max((MAX_CHARS - fixed) // len(shown), 120)
        lines = []
        for number, effect in enumerate(shown, 1):
            line = _line(number, effect, comp_limit=max(per - 120, 80))
            if len(line) > per:
                line = line[: per - 1].rstrip() + "…"
            lines.append(line)
        if extra > 0:
            lines.append(f"(+{extra} more; see /timemachine effects)")
        footer = FOOTER if any(e.get("compensation") for e in shown) else FOOTER_NO_COMMAND
        text = "\n".join([HEADER, *lines, footer])
        if len(text) > MAX_CHARS:
            room = MAX_CHARS - len(footer) - 2
            text = text[:room].rstrip() + "…\n" + footer
        return text[:MAX_CHARS]
    except Exception:
        return ""
