"""Point-of-no-return gate.

Rollback restores files; it cannot un-push a commit or un-publish a package.
When a tool call is about to cross that line, this module builds the directive
that makes Hermes ask the human first, through its own approval gate, with the
exact blast radius and the exact compensation command.

Pure functions, stdlib only, never raise.
"""

from __future__ import annotations

import re

MODES = ("approve", "warn", "off")
MAX_MESSAGE = 700

_NO_REWIND = "No rewind (atm_rewind or /rollback) can undo this."


def _mode(config) -> str:
    try:
        value = config.get("irreversible_gate", "approve") if isinstance(config, dict) else "approve"
        value = str(value).strip().lower()
        return value if value in MODES else "approve"
    except Exception:
        return "approve"


def _irreversible(effects) -> list[dict]:
    if not isinstance(effects, (list, tuple)):
        return []
    return [e for e in effects if isinstance(e, dict) and e.get("reversible") is False]


def rule_key(effect: dict) -> str:
    """Stable per-kind-and-target key, e.g. ``hermes-time-machine:git_push:origin/main``."""
    try:
        kind = str(effect.get("kind") or "unknown")
        target = effect.get("target") or "any"
        raw = f"hermes-time-machine:{kind}:{target}".lower()
        return re.sub(r"[^a-z0-9:/._@#+-]", "-", raw)[:120]
    except Exception:
        return "hermes-time-machine:unknown:any"


def _short(sha) -> str:
    return str(sha or "")[:7]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split()) if "\n" not in str(text) else str(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _describe(effect: dict) -> str:
    label = str(effect.get("kind") or "effect").replace("_", " ")
    target = effect.get("target")
    pre = effect.get("pre_state") or {}
    head = f"• {label}" + (f" → {target}" if target else "")
    kind = effect.get("kind")
    if kind == "git_push" and isinstance(pre, dict) and pre.get("pending_commits"):
        before = _short(pre.get("remote_sha_before"))
        prose = (
            f"publishes the commit(s) this command creates on top of {before}."
            if before
            else "publishes the commit(s) this command creates (new branch on the remote)."
        )
        if pre.get("force"):
            prose += " Force push."
    elif kind == "git_push" and isinstance(pre, dict) and pre.get("local_sha"):
        local, before = _short(pre.get("local_sha")), _short(pre.get("remote_sha_before"))
        if pre.get("delete"):
            prose = f"deletes the remote branch (was {before})."
        elif pre.get("new_branch"):
            prose = f"creates the branch on the remote at {local}."
        elif before and before != local:
            prose = f"publishes {before}..{local}."
        else:
            prose = f"publishes {local}."
        if pre.get("force"):
            prose += " Force push."
    elif kind == "git_push" and isinstance(pre, dict) and pre.get("delete"):
        before = _short(pre.get("remote_sha_before"))
        prose = f"deletes the remote branch{' (was ' + before + ')' if before else ''}."
    else:
        prose = str(effect.get("reason") or "changes state outside the workspace.")
    return f"{head}: {prose} {_NO_REWIND}"


def build_message(irreversible: list[dict], checkpoint_id: str | None) -> str:
    """Plain-text approval prompt, at most 700 characters."""
    try:
        items = [e for e in (irreversible or []) if isinstance(e, dict)]
        footer = (
            f"Local files are covered by checkpoint {checkpoint_id}."
            if checkpoint_id
            else "No checkpoint covers the local files for this call."
        )
        title = "Point of no return (hermes-time-machine)"
        shown, extra = items[:3], max(len(items) - 3, 0)
        tail = [f"(+{extra} more irreversible effect{'s' if extra > 1 else ''})"] if extra else []
        fixed = len(title) + len(footer) + sum(len(t) + 1 for t in tail) + 2
        budget = max(MAX_MESSAGE - fixed, 80)
        per = max(budget // max(len(shown), 1), 80)
        blocks: list[str] = []
        for effect in shown:
            desc = _describe(effect)
            comp = effect.get("compensation")
            if comp:
                second = f"  If needed later: {comp}"
            else:
                note = effect.get("compensation_note")
                second = f"  No automatic undo. {note}" if note else "  No automatic undo."
            first_limit = min(len(desc), max(per // 2, 60))
            first = _clip(desc, first_limit)
            second = _clip(second, max(per - len(first) - 1, 40))
            blocks.append(f"{first}\n{second}")
        body = "\n".join([title, *blocks, *tail])
        room = MAX_MESSAGE - len(footer) - 1
        if len(body) > room:
            body = body[: max(room - 1, 0)].rstrip() + "…"
        return f"{body}\n{footer}"
    except Exception:
        return "Point of no return (hermes-time-machine): this call has effects a rewind cannot undo."


def decide(effects: list[dict], config: dict, *, checkpoint_id: str | None, command: str = "") -> dict | None:
    """Return an approval directive for irreversible effects, or None to let the call through."""
    try:
        bad = _irreversible(effects)
        if not bad:
            return None
        if _mode(config) != "approve":
            return None
        patterns = config.get("gate_allow_patterns") if isinstance(config, dict) else None
        if isinstance(patterns, (list, tuple)):
            for pattern in patterns:
                try:
                    if isinstance(pattern, str) and re.search(pattern, command or "", re.IGNORECASE):
                        return None
                except re.error:
                    continue
        return {
            "action": "approve",
            "message": build_message(bad, checkpoint_id),
            "rule_key": rule_key(bad[0]),
        }
    except Exception:
        return None
