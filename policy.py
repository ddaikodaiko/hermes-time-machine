"""Risk scoring for tool calls.

The policy answers one question: does this call deserve a checkpoint before
it runs? It is deliberately cheap and deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import effects

DEFAULT_RISKY_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f?\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bdrop\s+table\b",
    r"\btruncate\s+table\b",
    r"\bnpm\s+publish\b",
    r"\bdocker\s+push\b",
    r"\bkubectl\s+(apply|delete|patch|replace)\b",
    r"\bterraform\s+apply\b",
    r"\bchmod\s+-R\b",
    r"\bdel\s+/[sfq]\b",
    r"\bformat\s+[a-z]:",
]

WRITE_TOOLS = {"write_file", "patch", "edit_file", "apply_patch", "str_replace_editor"}
PATH_KEYS = ("path", "file_path", "filename", "target")

# Commands that change files even when they are not destructive enough to
# cross the risk threshold. The first one in a turn earns a restore point.
MUTATING_PATTERNS = [
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\bdel\b",
    r"\bmkdir\b",
    r"\btouch\b",
    r"\bsed\s+-i",
    r"\btruncate\b",
    r"\btee\b",
    r"(?<![0-9&])>{1,2}\s*\S",
    r"\bgit\s+(checkout|restore|reset|clean|rebase|merge|stash|switch)\b",
    r"\b(npm|pnpm|yarn)\s+(install|ci|add|remove|uninstall)\b",
    r"\bpip\s+install\b",
    r"\bpoetry\s+(add|remove)\b",
    r"\b(alembic|prisma|flyway|liquibase)\b",
    r"\bmigrate\b",
    r"\bchmod\b",
    r"\bchown\b",
]


def is_mutating(tool_name: str, args: dict) -> bool:
    """True when the call can change files, even if it is not high-risk."""
    if tool_name in WRITE_TOOLS:
        return True
    command = _command(args)
    if not command:
        return False
    for pattern in MUTATING_PATTERNS:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False



def _command(args: dict) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _outside(value: str, workspace: Path) -> bool:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return False
        resolved = candidate.resolve()
        return not (resolved == workspace or workspace in resolved.parents)
    except OSError:
        return False


def evaluate(
    tool_name: str,
    args: dict,
    workspace: Path,
    extra_patterns: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Return (score, reasons). Score is 0..1; 0 means 'nothing to do'."""
    if not tool_name or tool_name.startswith("atm_"):
        return 0.0, []

    score = 0.0
    reasons: list[str] = []

    command = _command(args)
    if command:
        patterns = list(DEFAULT_RISKY_PATTERNS)
        for extra in extra_patterns or []:
            if isinstance(extra, str) and extra.strip():
                patterns.append(extra)
        for pattern in patterns:
            try:
                if re.search(pattern, command, re.IGNORECASE):
                    score = max(score, 0.9)
                    reasons.append(f"command matches /{pattern}/")
            except re.error:
                continue

    if tool_name in WRITE_TOOLS:
        for key in PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and _outside(value, workspace):
                score = max(score, 0.8)
                reasons.append(f"write outside workspace: {value}")
                break

    # Any known irreversible effect is worth a checkpoint even if the
    # command did not match a risky pattern.
    if command and any(not item["reversible"] for item in effects.classify(tool_name, args, workspace)):
        score = max(score, 0.75)
        reasons.append("irreversible external effect")

    return score, reasons
