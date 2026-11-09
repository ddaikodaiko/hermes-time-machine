"""Irreversible effects exposed as *tools* (MCP servers, plugin tools), not shell commands.

``effects.py`` reads terminal commands. A model that has a GitHub or Slack MCP server
never needs ``gh`` or ``curl``: it calls ``mcp__github__create_pull_request`` and the
shell analysis sees nothing. This module classifies those calls by tool name and
arguments, conservatively:

* Hermes 0.23 registers MCP tools as ``mcp__<server>__<tool>`` (server and tool
  sanitised to ``[A-Za-z0-9_]``, clamped to 64 characters with a ``_<hash8>``
  suffix). The legacy single-underscore form ``mcp_<server>_<tool>`` is accepted too.
* Known families (GitHub, messaging) get a target and, when one exists, a
  compensation. Everything else falls back to a verb in the tool name
  (publish, deploy, delete, ...), with an honest "unknown compensation" note.
* Read-only verbs (get, list, search, ...) never trigger. Arguments are never
  searched for words; only the tool name decides, so a PR body that mentions
  "delete" is not a deletion.
* Nothing here runs a process or touches the network. Values that end up in a
  compensation command are validated first (they come from the model or from
  tool results).

Never raises.
"""

from __future__ import annotations

import json
import re

# Hermes built-ins and our own tools: not external effects (or handled elsewhere).
EXCLUDED_EXACT = frozenset(
    {
        "terminal", "shell", "bash", "run_command", "execute_command", "exec", "local_terminal",
        "process_manage", "close_terminal", "read_terminal", "read_window_below",
        "read_file", "write_file", "patch", "search_files",
        "memory", "todo", "todo_list", "clarify", "delegate_task", "execute_code", "session_search",
        "web_search", "web_extract", "x_search", "vision_analyze", "video_analyze",
        "image_generate", "video_generate", "text_to_speech",
        "cronjob", "cronjob_manage", "react_to_message",
        "set_theme", "show_tip", "focus_pane", "gui_tour", "annotate_preview", "desktop_preview",
        "desktop_project", "drive_preview", "computer_use", "setup_beat",
    }
)
EXCLUDED_PREFIXES = ("atm_", "browser_", "skill_", "skills_")

ACTION_VERBS = frozenset(
    {"publish", "deploy", "delete", "drop", "send", "merge", "transfer", "pay", "charge", "refund"}
)
READ_VERBS = frozenset(
    {"get", "list", "search", "read", "fetch", "query", "describe",
     "find", "show", "view", "lookup", "check", "preview", "validate", "count"}
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_HASH_SUFFIX = re.compile(r"_[0-9a-f]{8}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]{0,199}$")
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")

_UNKNOWN_COMP = (
    "Compensation is unknown to the plugin: it cannot tell what this tool changed. "
    "Check the service and revert it there."
)
_MSG_NOTE = "A sent message can't be unsent; some platforms allow delete/edit of your own messages."


def safe_token(value, pattern: str = "ref") -> str | None:
    """*value* when it is safe to paste into a shell command, else None."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    rx = {"name": _SAFE_NAME, "ref": _SAFE_REF, "sha": _SAFE_SHA}[pattern]
    return text if rx.match(text) else None


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


def split_name(tool_name) -> tuple[str | None, str, bool]:
    """``(server, body, is_mcp)``: lower-case snake_case, hash suffix removed."""
    raw = str(tool_name or "").strip()
    if len(raw) == 64:
        raw = _HASH_SUFFIX.sub("", raw)
    low = raw.lower()
    is_mcp = low.startswith("mcp_")
    server = None
    body = raw
    if low.startswith("mcp__"):
        rest = raw[5:]
        if "__" in rest:
            server, body = rest.split("__", 1)
        else:
            body = rest
    elif is_mcp:
        body = raw[4:]
    body = re.sub(r"[-.\s]+", "_", _CAMEL.sub("_", body)).lower().strip("_")
    return (server.lower() if server else None), body, is_mcp


def is_excluded(tool_name) -> bool:
    name = str(tool_name or "").strip().lower()
    if not name or name in EXCLUDED_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def _tail_is(body: str, *names: str) -> bool:
    return any(body == n or body.endswith("_" + n) for n in names)


# --------------------------------------------------------------------------
# argument helpers
# --------------------------------------------------------------------------


def _s(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _first(args: dict, *keys: str) -> str:
    for key in keys:
        text = _s(args.get(key))
        if text:
            return text
    return ""


def _number(args: dict, *keys: str) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, bool):
            continue
        text = _s(value)
        if text.isdigit():
            return text
    return None


def _repo(args: dict) -> tuple[str, str]:
    owner = _first(args, "owner", "org", "organization", "repo_owner")
    repo = _first(args, "repo", "repository", "repo_name", "full_name", "repository_name")
    if "/" in repo:
        head, tail = repo.split("/", 1)
        owner = owner or head
        repo = tail
    return owner, repo


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _dry_run(args: dict) -> bool:
    for key in ("dry_run", "dryRun", "dry-run", "validate_only", "validateOnly"):
        value = args.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")):
            return True
    return False


def _fx(
    kind: str,
    reason: str,
    evidence: str,
    tool_name: str,
    server: str | None,
    target: str | None = None,
    compensation: str | None = None,
    note: str = "",
    pre_state: dict | None = None,
) -> dict:
    pre = {"tool": str(tool_name), "server": server}
    pre.update(pre_state or {})
    return {
        "kind": kind,
        "reversible": False,
        "reason": reason,
        "evidence": (evidence or str(tool_name))[:300],
        "target": target,
        "compensation": compensation,
        "compensation_note": note,
        "pre_state": pre,
        "source": "tool",
    }


# --------------------------------------------------------------------------
# families
# --------------------------------------------------------------------------

_GH_PR_CREATE = ("create_pull_request", "open_pull_request", "create_pr")
_GH_ISSUE_CREATE = ("create_issue",)
_GH_MERGE = ("merge_pull_request", "merge_pr")
_GH_RELEASE = ("create_release", "publish_release")
_GH_COMMIT = ("push_files", "create_or_update_file")
_GH_BRANCH_DELETE = ("delete_branch",)

_MSG_TAIL = re.compile(
    r"(?:^|_)(?:send|post|reply|broadcast)_(?:message|email|mail|dm|sms|text|chat|reply|notification|msg)(?:_|$)"
)
_MSG_PLATFORM = re.compile(
    r"^(?:slack|discord|telegram|whatsapp|teams|matrix|signal|mattermost|email|gmail|smtp|twilio)_.*(?:send|post|reply)"
)
_MSG_PLATFORMS = ("slack", "discord", "telegram", "whatsapp", "teams", "matrix", "signal", "mattermost",
                  "email", "gmail", "smtp", "twilio")


def _github_like(server: str | None, body: str, args: dict) -> bool:
    hay = f"{server or ''}_{body}"
    if re.search(r"(?:^|_)(?:github|gh|gitea|forgejo)(?:_|$)", hay):
        return True
    owner, repo = _repo(args)
    return bool(owner and repo)


def _github(tool_name, server, body, args) -> dict | None:
    if not _github_like(server, body, args):
        return None
    owner, repo = _repo(args)
    slug = f"{owner}/{repo}" if owner and repo else ""
    safe_slug = slug if (safe_token(owner, "name") and safe_token(repo, "name")) else ""
    repo_flag = f" --repo {safe_slug}" if safe_slug else ""
    pre = {"owner": owner or None, "repo": repo or None}

    if _tail_is(body, *_GH_PR_CREATE):
        head, base, title = _first(args, "head"), _first(args, "base"), _first(args, "title")
        reason = f"Opens a pull request{' ' + repr(_clip(title, 60)) if title else ''}" + (
            f" ({_clip(head, 40)} → {_clip(base, 40)})" if head and base else ""
        ) + f" on {slug or 'GitHub'}."
        note = (
            "The pull request number is only known once the call returns; "
            f"`gh pr close <number>{repo_flag}` will close it."
        )
        return _fx("github_pr", reason, title or head, tool_name, server, slug or None, None, note,
                   {**pre, "head": head or None, "base": base or None})

    if _tail_is(body, *_GH_ISSUE_CREATE):
        title = _first(args, "title")
        reason = f"Opens an issue{' ' + repr(_clip(title, 60)) if title else ''} on {slug or 'GitHub'}."
        note = (
            "The issue number is only known once the call returns; "
            f"`gh issue close <number>{repo_flag}` will close it. Notifications already went out."
        )
        return _fx("github_issue", reason, title, tool_name, server, slug or None, None, note, pre)

    if _tail_is(body, *_GH_MERGE):
        number = _number(args, "pullNumber", "pull_number", "pr_number", "prNumber", "number",
                         "pull_request_number", "pullRequestNumber", "pr")
        method = _first(args, "merge_method", "mergeMethod", "method") or "merge"
        target = f"{slug}#{number}" if slug and number else (slug or None)
        reason = f"Merges pull request {'#' + number + ' ' if number else ''}into its base branch on {slug or 'GitHub'} ({_clip(method, 20)})."
        note = (
            "No automatic undo. Revert with a new pull request: "
            + ("`git revert -m 1 <merge-sha>`" if method == "merge" else "`git revert <merge-sha>`")
            + " on the base branch (the merge sha is in the call result)."
        )
        return _fx("github_merge", reason, number or slug, tool_name, server, target, None, note,
                   {**pre, "number": number, "merge_method": method})

    if _tail_is(body, *_GH_RELEASE):
        tag = _first(args, "tag_name", "tagName", "tag")
        safe_tag = safe_token(tag, "ref")
        comp = None
        if safe_tag and safe_slug:
            comp = f"gh release delete {safe_tag}{repo_flag} --cleanup-tag --yes"
        reason = f"Publishes release {_clip(tag, 60) or '(tag unknown)'} on {slug or 'GitHub'}."
        note = (
            "Deletes the release and its tag from GitHub; downloads already made cannot be recalled."
            if comp
            else "The release tag or repository could not be read safely; delete it with "
            "`gh release delete <tag> --repo <owner/repo> --cleanup-tag --yes`."
        )
        return _fx("github_release", reason, tag, tool_name, server, tag or slug or None, comp, note,
                   {**pre, "tag": tag or None})

    if _tail_is(body, *_GH_COMMIT):
        branch = _first(args, "branch", "ref")
        files = args.get("files")
        count = len(files) if isinstance(files, (list, tuple)) else 1
        path = _first(args, "path")
        what = f"{count} file(s)" if isinstance(files, (list, tuple)) else (_clip(path, 60) or "a file")
        reason = f"Commits {what} directly to {_clip(branch, 60) or 'a branch'} of {slug or 'GitHub'}."
        note = (
            "No automatic undo. After the call, `git pull` the branch and run `git revert <commit-sha>` "
            "(the commit sha is in the call result), then push; on a protected branch use a pull request."
        )
        target = f"{slug}@{branch}" if slug and branch else (slug or None)
        return _fx("github_commit", reason, _first(args, "message") or path, tool_name, server, target, None, note,
                   {**pre, "branch": branch or None, "files": count})

    if _tail_is(body, *_GH_BRANCH_DELETE):
        branch = _first(args, "branch", "ref", "name")
        target = f"{slug}@{branch}" if slug and branch else (slug or None)
        reason = f"Deletes branch {_clip(branch, 60) or '(unknown)'} on {slug or 'GitHub'}."
        note = (
            "No automatic undo. If you know the branch's last commit sha (the pull request page or "
            "`git reflog` shows it), recreate it with `git push <remote> <sha>:refs/heads/<branch>`."
        )
        return _fx("github_branch_delete", reason, branch, tool_name, server, target, None, note,
                   {**pre, "branch": branch or None})
    return None


def _messaging(tool_name, server, body, args) -> dict | None:
    joined = f"{server}_{body}" if server else body
    if not (_MSG_TAIL.search(body) or _MSG_PLATFORM.search(joined)):
        return None
    if any(t in READ_VERBS for t in joined.split("_")):
        return None  # slack_get_user_posts, discord_list_sent_messages: reading, not sending
    platform = next((p for p in _MSG_PLATFORMS if re.search(rf"(?:^|_){p}(?:_|$)", joined)), "")
    if not platform:
        platform = server or "messaging"
    recipients = [
        _clip(_s(args.get(k)), 60)
        for k in ("channel", "channel_id", "chat_id", "to", "recipient", "recipients", "user", "user_id",
                  "room", "room_id", "email", "address", "target")
        if _s(args.get(k))
    ]
    dest = recipients[0] if recipients else ""
    target = f"{platform}:{dest}" if dest else platform
    subject = _clip(_first(args, "subject", "title"), 60)
    reason = f"Sends a message via {platform}{' to ' + dest if dest else ''}" + (f" ({subject!r})" if subject else "") + "."
    return _fx("message_send", reason, subject or dest or tool_name, tool_name, server, target[:120], None, _MSG_NOTE,
               {"platform": platform, "recipient": dest or None})


def _generic(tool_name, server, body, args, is_mcp) -> dict | None:
    tokens = [t for t in body.split("_") if t]
    if not tokens or tokens[0] in READ_VERBS:
        return None
    verb = next((t for t in tokens if t in ACTION_VERBS), None)
    if verb is None:
        return None
    ident = ""
    for key in ("url", "repo", "repository", "name", "id", "path", "resource", "project", "service", "bucket",
                "table", "database", "to", "recipient", "account"):
        text = _s(args.get(key))
        if text:
            ident = _clip(text, 60)
            break
    base = f"{server}:{body}" if server else body
    target = f"{base} {ident}".strip()[:120] if ident else base[:120]
    reason = f"Tool {tool_name} looks like an irreversible external action ({verb})."
    return _fx("tool_call", reason, ident or tool_name, tool_name, server, target, None, _UNKNOWN_COMP,
               {"verb": verb})


def classify_tool(tool_name, args) -> list[dict]:
    """Effects of a non-terminal tool call, decided by tool name and arguments. Never raises."""
    try:
        if is_excluded(tool_name):
            return []
        args = args if isinstance(args, dict) else {}
        if _dry_run(args):
            return []
        server, body, is_mcp = split_name(tool_name)
        if not body or body.split("_")[0] in READ_VERBS:
            return []
        for fn in (_github, _messaging):
            found = fn(tool_name, server, body, args)
            if found:
                return [found]
        found = _generic(tool_name, server, body, args, is_mcp)
        return [found] if found else []
    except Exception:
        return []


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

_PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)|repos/([\w.-]+)/([\w.-]+)/pulls/(\d+)")
_ISSUE_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)|repos/([\w.-]+)/([\w.-]+)/issues/(\d+)")
_UNCERTAIN = re.compile(r"outcome is\s+UNKNOWN|may or may not have taken effect", re.IGNORECASE)
_FAILED_PREFIX = re.compile(r"^\s*(error|failed|exception)\b\s*[:\-]", re.IGNORECASE)


def _text(result) -> str:
    if isinstance(result, bytes):
        text = result.decode("utf-8", errors="replace")
    elif isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, default=str, ensure_ascii=False)
        except Exception:
            text = str(result)
    return text.replace("\\/", "/")


def _objects(result) -> list[dict]:
    out: list[dict] = []

    def add(value, depth=0):
        if depth > 3 or len(out) > 20:
            return
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] not in "{[":
                return
            try:
                value = json.loads(stripped)
            except ValueError:
                return
        if isinstance(value, dict):
            out.append(value)
            for item in value.values():
                if isinstance(item, (dict, list, str)):
                    add(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:5]:
                add(item, depth + 1)

    add(result)
    return out


def tool_outcome(result) -> dict:
    """``{"status": "succeeded"|"failed"|"unknown", "uncertain": bool, ...}`` from structured results only.

    Free text is never scanned for the word "error": a successful PR titled "Fix error
    handling" must not be recorded as a failed call.
    """
    try:
        text = _text(result)
        objs = _objects(result)
        for obj in objs:
            err = obj.get("error")
            flagged = (
                (err and not (isinstance(err, str) and not err.strip()))
                or obj.get("isError") is True
                or obj.get("is_error") is True
                or obj.get("ok") is False
                or obj.get("success") is False
                or str(obj.get("status") or "").lower() in ("error", "failed", "failure")
            )
            if flagged:
                detail = _s(err) if isinstance(err, (str, int)) else ""
                if _UNCERTAIN.search(detail or text):
                    return {"status": "unknown", "uncertain": True, "exit_code": None, "detail": detail[-200:]}
                return {"status": "failed", "uncertain": False, "exit_code": None, "detail": detail[-200:]}
        if not objs and _FAILED_PREFIX.match(text):
            return {"status": "failed", "uncertain": False, "exit_code": None, "detail": text.strip()[-200:]}
        return {"status": "unknown", "uncertain": False, "exit_code": None, "detail": ""}
    except Exception:
        return {"status": "unknown", "uncertain": False, "exit_code": None, "detail": ""}


def _match_groups(rx: re.Pattern, text: str):
    m = rx.search(text)
    if not m:
        return None
    g = m.groups()
    owner, repo, number = (g[0], g[1], g[2]) if g[0] else (g[3], g[4], g[5])
    return owner, repo, number


def _find_url(rx: re.Pattern, result, text: str):
    for obj in _objects(result):
        for key in ("html_url", "url"):
            value = obj.get(key)
            if isinstance(value, str):
                found = _match_groups(rx, value)
                if found:
                    return found
    return _match_groups(rx, text)


def _find_sha(result, *, commit: bool) -> str | None:
    for obj in _objects(result):
        candidates = []
        inner = obj.get("commit")
        if isinstance(inner, dict):
            candidates.append(inner.get("sha"))
        if not commit or not isinstance(inner, dict):
            candidates.append(obj.get("sha"))
        for value in candidates:
            sha = safe_token(value, "sha")
            if sha:
                return sha
    return None


def finalize_tool(out: dict, result) -> None:
    """Fill target, compensation and notes from the call result. Mutates *out*; never raises."""
    try:
        kind = out.get("kind")
        pre = out.setdefault("pre_state", {})
        text = _text(result)
        if kind in ("github_pr", "github_issue"):
            rx = _PR_URL if kind == "github_pr" else _ISSUE_URL
            found = _find_url(rx, result, text)
            number = owner = repo = None
            if found:
                owner, repo, number = found
            else:
                number = next((str(o["number"]) for o in _objects(result)
                               if isinstance(o.get("number"), int) and not isinstance(o.get("number"), bool)), None)
                owner, repo = pre.get("owner"), pre.get("repo")
            if number and owner and repo:
                word = "pr" if kind == "github_pr" else "issue"
                out["target"] = f"{owner}/{repo}#{number}"
                pre["number"] = int(number)
                if safe_token(owner, "name") and safe_token(repo, "name"):
                    out["compensation"] = f"gh {word} close {number} --repo {owner}/{repo}"
                    out["compensation_note"] = (
                        "Closes it; it stays visible on GitHub and can be reopened."
                        + (" Notifications already went out." if word == "issue" else "")
                    )
        elif kind == "github_merge":
            sha = _find_sha(result, commit=False)
            method = pre.get("merge_method") or "merge"
            if sha:
                pre["merge_sha"] = sha
                flag = "-m 1 " if method == "merge" else ""
                out["compensation_note"] = (
                    f"No automatic undo. Revert with a new pull request: `git revert {flag}{sha}` "
                    "on the base branch, then push it through a pull request."
                )
        elif kind == "github_commit":
            sha = _find_sha(result, commit=True)
            if sha:
                pre["commit_sha"] = sha
                out["compensation_note"] = (
                    f"No automatic undo. `git pull` the branch and run `git revert {sha}`, then push "
                    "(or open a pull request if the branch is protected)."
                )
        elif kind == "github_release":
            for obj in _objects(result):
                tag = safe_token(obj.get("tag_name"), "ref")
                if tag and not pre.get("tag"):
                    pre["tag"] = tag
                    out["target"] = tag
                    owner, repo = pre.get("owner"), pre.get("repo")
                    if owner and repo and safe_token(owner, "name") and safe_token(repo, "name"):
                        out["compensation"] = f"gh release delete {tag} --repo {owner}/{repo} --cleanup-tag --yes"
                    break
    except Exception:
        pass
