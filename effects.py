"""Effect ledger: actions a workspace rewind cannot undo.

The ledger is intentionally conservative. If a command might have changed
state outside the protected workspace, it is recorded and flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

IRREVERSIBLE_PATTERNS: list[tuple[str, str, str]] = [
    (r"\bgit\s+push\b", "git_push", "Pushed commits or tags to a remote."),
    (r"\bgh\s+pr\s+create\b", "github_pr", "Opened a pull request."),
    (r"\bgh\s+release\s+create\b", "github_release", "Published a release."),
    (r"\bnpm\s+publish\b", "npm_publish", "Published an npm package."),
    (r"\byarn\s+publish\b", "npm_publish", "Published an npm package."),
    (r"\bpnpm\s+publish\b", "npm_publish", "Published an npm package."),
    (r"\bdocker\s+push\b", "docker_push", "Pushed an image to a registry."),
    (r"\bkubectl\s+(apply|delete|patch|replace)\b", "kubectl_write", "Changed a Kubernetes cluster."),
    (r"\bterraform\s+apply\b", "terraform_apply", "Applied infrastructure changes."),
    (r"\baws\s+\S+\s+(create|delete|update|put|terminate)\b", "aws_write", "Changed AWS resources."),
    (r"\bgcloud\s+\S+\s+(create|delete|update|deploy)\b", "gcloud_write", "Changed GCP resources."),
    (r"\baz\s+\S+\s+(create|delete|update)\b", "azure_write", "Changed Azure resources."),
    (r"\bcurl\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|--data|--upload-file)", "http_write", "Sent an HTTP write request."),
    (r"\bwget\b[^\n]*(--post-data|--post-file)", "http_write", "Sent an HTTP write request."),
    (r"\b(psql|mysql|sqlite3)\b[^\n]*\b(drop|truncate|delete|update|insert)\b", "database_write", "Changed a database."),
    (r"\b(sendmail|mail)\b", "email", "Sent an email."),
    (r"\bssh\b[^\n]+\s+['\"]?[^\n]*(rm|systemctl|docker|kubectl)\b", "remote_shell", "Ran a command on a remote host."),
]

LOCAL_DESTRUCTIVE_PATTERNS: list[tuple[str, str, str]] = [
    (r"\brm\s+-[a-z]*r[a-z]*f?\b", "workspace_delete", "Deleted files inside the workspace."),
    (r"\bgit\s+reset\s+--hard\b", "git_reset", "Discarded uncommitted changes."),
    (r"\bgit\s+clean\s+-[a-z]*f", "git_clean", "Deleted untracked files."),
]

TERMINAL_TOOLS = {
    "terminal",
    "shell",
    "bash",
    "run_command",
    "execute_command",
    "exec",
    "local_terminal",
}

PATH_KEYS = ("path", "file_path", "filename", "target")


def _command_from(args: dict) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _outside_workspace(value: str, workspace: Path | None) -> bool:
    if not workspace:
        return False
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return False
        resolved = candidate.resolve()
        return not (resolved == workspace or workspace in resolved.parents)
    except OSError:
        return False


def classify(tool_name: str, args: dict, workspace: Path | None = None) -> list[dict]:
    """Return the effects a completed tool call may have produced."""
    effects: list[dict] = []
    seen: set[str] = set()

    command = _command_from(args)
    if command:
        for pattern, kind, reason in IRREVERSIBLE_PATTERNS:
            if kind in seen:
                continue
            if re.search(pattern, command, re.IGNORECASE):
                seen.add(kind)
                effects.append(
                    {
                        "kind": kind,
                        "reversible": False,
                        "reason": reason,
                        "evidence": command[:300],
                    }
                )
        for pattern, kind, reason in LOCAL_DESTRUCTIVE_PATTERNS:
            if kind in seen:
                continue
            if re.search(pattern, command, re.IGNORECASE):
                seen.add(kind)
                effects.append(
                    {
                        "kind": kind,
                        "reversible": True,
                        "reason": reason,
                        "evidence": command[:300],
                    }
                )

    if tool_name not in TERMINAL_TOOLS:
        for key in PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and _outside_workspace(value, workspace):
                effects.append(
                    {
                        "kind": "outside_workspace_write",
                        "reversible": False,
                        "reason": "Wrote outside the protected workspace, where no checkpoint exists.",
                        "evidence": value,
                    }
                )
                break

    return effects
