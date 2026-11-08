"""Effect ledger: actions a workspace rewind cannot undo.

The ledger is intentionally conservative. If a command might have changed
state outside the protected workspace, it is recorded and flagged.

Every rollback tool restores files. None of them can undo a ``git push``, an
``npm publish`` or a ``terraform apply``. hermes-time-machine records those
effects *with the state needed to compensate them*: ``preflight`` reads that
state from files on disk before the call runs (never by spawning a process),
so the human can be shown the exact blast radius and the exact compensation
command at the point of no return.

Public API (none of these ever raises):

    classify(tool_name, args, workspace)   -> list[dict]   (backward compatible)
    preflight(tool_name, args, workspace)  -> list[dict]   (classify + pre_state + compensation)
    outcome(tool_name, result)             -> dict
    finalize(effect, result, workspace)    -> dict
    split_commands(command)                -> list[str]
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
    (r"\bcurl\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|--request[ =](POST|PUT|PATCH|DELETE)|--data|--upload-file|--json|--form|\s-(?-i:[dFT])\b)", "http_write", "Sent an HTTP write request."),
    (r"\bwget\b[^\n]*(--post-data|--post-file)", "http_write", "Sent an HTTP write request."),
    (r"\b(psql|mysql|sqlite3)\b[^\n]*\b(drop|truncate|delete|update|insert)\b", "database_write", "Changed a database."),
    (r"\b(sendmail|mail)\b", "email", "Sent an email."),
    (r"\bssh\b[^\n]+\s+['\"]?[^\n]*\b(rm|systemctl|docker|kubectl)\b", "remote_shell", "Ran a command on a remote host."),
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

_LOCAL_NOTE = "Local files: restore them with atm_rewind or /rollback from the checkpoint."
_UNKNOWN_REMOTE_NOTE = (
    "Remote state before the push is unknown to the plugin; "
    "inspect it with `git ls-remote {remote}` before acting."
)
_FORCE_NOTE = (
    "This was a force push: commits that were only on the remote may now be unreachable there."
)
_NO_UNDO_NOTES = {
    "docker_push": "Registries have no generic undo; delete the tag {target} in the registry UI or API.",
    "terraform_apply": (
        "Restore the previous configuration from the checkpoint, then run `terraform plan` and "
        "`terraform apply`. The previous state is usually in terraform.tfstate.backup."
    ),
    "database_write": "Restore from a database backup; the plugin did not capture database state.",
    "email": "A sent email cannot be recalled; send a correction if needed.",
    "http_write": "The remote service already received the request; undo it with that service's own API.",
    "remote_shell": "The command ran on another host; inspect and revert it there, the plugin has no copy of that state.",
    "aws_write": "Revert the change in AWS (console or CLI); the plugin did not capture cloud state.",
    "gcloud_write": "Revert the change in GCP (console or gcloud); the plugin did not capture cloud state.",
    "azure_write": "Revert the change in Azure (portal or az); the plugin did not capture cloud state.",
    "outside_workspace_write": "The file is outside the protected workspace, so no checkpoint holds its previous content.",
    "kubectl_write": "Inspect the cluster with `kubectl get` and revert the resources by hand.",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _command_from(args) -> str:
    if not isinstance(args, dict):
        return ""
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
        # Resolve both sides: on Windows an 8.3 short path (C:\Users\JOSEC~1.DES\...) and its
        # long form are the same folder, and comparing them raw flagged in-workspace writes
        # as "outside the workspace" (a false point-of-no-return prompt).
        root = Path(workspace).expanduser().resolve()
        return not (resolved == root or root in resolved.parents)
    except (OSError, ValueError, RuntimeError):
        return False


def _effect(
    kind: str,
    reversible: bool,
    reason: str,
    evidence: str,
    target: str | None = None,
    compensation: str | None = None,
    note: str = "",
    pre_state: dict | None = None,
) -> dict:
    return {
        "kind": kind,
        "reversible": reversible,
        "reason": reason,
        "evidence": evidence[:300],
        "target": target,
        "compensation": compensation,
        "compensation_note": note,
        "pre_state": pre_state or {},
    }


_HEREDOC = re.compile(r"""<<-?\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z_][\w.-]*))""")


def split_commands(command: str) -> list[str]:
    """Split a shell line on ``&&``, ``||``, ``;``, ``|``, ``&``, subshell parens and newlines.

    Quoted text, ``#`` comments and heredoc bodies never produce commands.
    """
    try:
        if not isinstance(command, str):
            return []
        parts: list[str] = []
        buf: list[str] = []
        quote = ""
        heredocs: list[str] = []
        i, n = 0, len(command)
        while i < n:
            ch = command[i]
            if quote:
                buf.append(ch)
                if ch == "\\" and quote == '"' and i + 1 < n:
                    buf.append(command[i + 1])
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
                buf.append(ch)
            elif ch == "#" and (not buf or buf[-1].isspace()):
                while i < n and command[i] != "\n":
                    i += 1
                continue
            elif ch == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
                m = _HEREDOC.match(command, i)
                if m:
                    heredocs.append(m.group(1) or m.group(2) or m.group(3))
                    i = m.end()
                    continue
                buf.append(ch)
            elif ch == "\\" and i + 1 < n and command[i + 1] in "\n;|&":
                buf.append(ch)
                buf.append(command[i + 1])
                i += 1
            elif ch == "\n" and heredocs:
                parts.append("".join(buf))
                buf = []
                i += 1
                while heredocs and i <= n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n + 1 if end < 0 else end + 1
                    if line.strip() == heredocs[0]:
                        heredocs.pop(0)
                continue
            elif ch in ";\n|()`" or (ch == "&" and not (buf and buf[-1] in "<>") and not command.startswith("&>", i)):
                if ch in "|&" and i + 1 < n and command[i + 1] == ch:
                    i += 1
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1
        parts.append("".join(buf))
        return [p.strip() for p in parts if p.strip()]
    except Exception:
        return []


def _tokenize(segment: str) -> list[str]:
    text = segment
    if os.name == "nt":
        # keep Windows path separators alive through POSIX-mode shlex
        text = re.sub(r"\\(?![\"'])", "/", text)
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = segment.split()
    return _strip_redirections(tokens)


# `2>&1`, `>/dev/null`, `&>log`, `1>>out`, `<in` — and the bare forms whose target is the next token.
_REDIRECT_INLINE = re.compile(r"^(\d*|&)(>>?|<)(&?\d+|&-|.+)$")
_REDIRECT_BARE = re.compile(r"^(\d*|&)(>>?|<)$")


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Drop shell redirections so `git push origin main 2>&1` parses as a one-refspec push.

    Agents append `2>&1` or `> log` to most commands; without this the redirection
    was read as a second refspec and the compensation command was lost.
    """
    out: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_BARE.match(token):
            skip_next = True
            continue
        if _REDIRECT_INLINE.match(token):
            continue
        out.append(token)
    return out


_WRAPPERS = {"sudo", "env", "time", "command", "nohup", "exec", "timeout", "nice", "xargs", "stdbuf",
             "setsid", "doas", "builtin"}
_CONTROL = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "&", "fi", "done"}
_WRAPPER_VALUE_OPTS = {"-u", "-g", "-U", "-C", "-k", "-s", "-n", "-p", "-i", "-I", "-P"}
_SHELLS = {"sh", "bash", "zsh", "dash"}


def _head_index(tokens: list[str]) -> int:
    """Index of the real command: skips ``VAR=x``, ``then``/``do``, ``sudo -u x``, ``timeout 30`` ..."""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        low = Path(tok).name.lower()
        if tok.lower() in _CONTROL or re.match(r"^\w+=", tok):
            i += 1
            continue
        if low in _WRAPPERS:
            i += 1
            while i < n and tokens[i].startswith("-"):
                i += 2 if tokens[i] in _WRAPPER_VALUE_OPTS and i + 1 < n else 1
            while i < n and re.match(r"^\w+=", tokens[i]):
                i += 1
            if low == "timeout" and i < n and re.match(r"^[\d.]+[smhd]?$", tokens[i]):
                i += 1
            continue
        break
    return i


def _segments(command: str, workspace: Path | None, base: Path | None = None, depth: int = 0) -> list[tuple[list[str], Path | None]]:
    """Tokenised sub-commands paired with the directory each one runs in (tracks ``cd``)."""
    out: list[tuple[list[str], Path | None]] = []
    cwd = base if base is not None else workspace
    for seg in split_commands(command):
        tokens = _tokenize(seg)
        tokens = tokens[_head_index(tokens):]
        if not tokens:
            continue
        if tokens[0] == "cd" or (tokens[0].lower() == "set-location"):
            args = [t for t in tokens[1:] if t.lower() not in ("/d", "-path", "-literalpath")]
            if args:
                try:
                    target = Path(args[-1]).expanduser()
                    cwd = target if target.is_absolute() or cwd is None else cwd / target
                except Exception:
                    pass
            continue
        tokens = tokens[_head_index(tokens):]
        if not tokens:
            continue
        name = Path(tokens[0]).name.lower()
        if depth < 2 and name in _SHELLS and "-c" in tokens:
            idx = tokens.index("-c")
            if idx + 1 < len(tokens):
                out.extend(_segments(tokens[idx + 1], workspace, cwd, depth + 1))
                continue
        out.append((tokens, cwd))
    return out


def _strip_creds(url: str) -> str:
    return re.sub(r"^([a-zA-Z][\w+.-]*://)[^/@\s]+@", r"\1", url or "")


def _short(sha: str | None) -> str:
    return (sha or "")[:7]


# --------------------------------------------------------------------------
# git on-disk reading (files only, no subprocess)
# --------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _find_git(repo: Path | None) -> tuple[Path, Path, Path] | None:
    """Return (repo_root, git_dir, common_dir) for the repo containing ``repo``."""
    if repo is None:
        return None
    try:
        current = repo if repo.is_absolute() else repo.resolve()
        for candidate in [current, *list(current.parents)[:12]]:
            dot = candidate / ".git"
            if dot.is_dir():
                return candidate, dot, dot
            if dot.is_file():
                text = _read_text(dot) or ""
                m = re.search(r"^gitdir:\s*(.+)$", text, re.MULTILINE)
                if not m:
                    continue
                gitdir = Path(m.group(1).strip())
                if not gitdir.is_absolute():
                    gitdir = candidate / gitdir
                common = gitdir
                ctext = _read_text(gitdir / "commondir")
                if ctext and ctext.strip():
                    common = Path(ctext.strip())
                    if not common.is_absolute():
                        common = gitdir / common
                return candidate, gitdir, common
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def _packed_refs(common: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    text = _read_text(common / "packed-refs")
    if not text:
        return refs
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#^":
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and _SHA_RE.match(parts[0]):
            refs[parts[1].strip()] = parts[0]
    return refs


def _read_ref(git: tuple[Path, Path, Path], ref: str, packed: dict[str, str] | None = None, _depth: int = 0) -> str | None:
    _, gitdir, common = git
    for base in (common, gitdir):
        text = _read_text(base / ref)
        if text is None:
            continue
        text = text.strip()
        if _SHA_RE.match(text):
            return text
        if text.startswith("ref:") and _depth < 3:
            return _read_ref(git, text[4:].strip(), packed, _depth + 1)
    if packed is None:
        packed = _packed_refs(common)
    return packed.get(ref)


def _read_head(git: tuple[Path, Path, Path]) -> tuple[str | None, str | None]:
    """Return (branch, sha). branch is None when HEAD is detached."""
    text = (_read_text(git[1] / "HEAD") or "").strip()
    if text.startswith("ref:"):
        ref = text[4:].strip()
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        return branch, _read_ref(git, ref)
    if _SHA_RE.match(text):
        return None, text
    return None, None


def _read_config(common: Path) -> dict[str, dict[str, str]]:
    """Minimal git-config parser: {'branch "main"': {'remote': 'origin', ...}}."""
    sections: dict[str, dict[str, str]] = {}
    text = _read_text(common / "config")
    if not text:
        return sections
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = re.match(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]', line)
        if m:
            name = m.group(1).lower()
            key = f'{name} "{m.group(2)}"' if m.group(2) is not None else name
            current = sections.setdefault(key, {})
            continue
        if current is not None and "=" in line:
            k, v = line.split("=", 1)
            current.setdefault(k.strip().lower(), v.strip().strip('"'))
    return sections


# --------------------------------------------------------------------------
# git push analysis
# --------------------------------------------------------------------------

_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec", "--signed", "--recurse-submodules"}


def _parse_push(tokens: list[str], cwd: Path | None) -> dict | None:
    """Parse ``git [opts] push [opts] [remote [refspec...]]``; None if not a push."""
    start = None
    for i, tok in enumerate(tokens[:5]):
        if Path(tok).name.lower() in ("git", "git.exe"):
            start = i
            break
        if not (re.match(r"^\w+=", tok) or tok in _WRAPPERS):
            return None
    if start is None:
        return None
    i = start + 1
    repo = cwd
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        if opt == "-C" and i + 1 < len(tokens):
            d = Path(tokens[i + 1]).expanduser()
            repo = d if d.is_absolute() or repo is None else repo / d
            i += 2
        elif opt in _GIT_OPTS_WITH_VALUE and i + 1 < len(tokens):
            i += 2
        else:
            i += 1
    if i >= len(tokens) or tokens[i] != "push":
        return None
    info = {
        "repo": repo, "force": False, "lease": False, "set_upstream": False, "tags": False,
        "all": False, "mirror": False, "delete": False, "dry_run": False,
        "positional": [],
    }
    j = i + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":
            info["positional"].extend(tokens[j + 1:])
            break
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name == "--force":
                info["force"] = True
            elif name in ("--force-with-lease", "--force-if-includes"):
                info["force"] = True
                info["lease"] = True
            elif name == "--set-upstream":
                info["set_upstream"] = True
            elif name == "--tags":
                info["tags"] = True
            elif name == "--all":
                info["all"] = True
            elif name == "--mirror":
                info["mirror"] = True
            elif name == "--delete":
                info["delete"] = True
            elif name == "--dry-run":
                info["dry_run"] = True
            elif name in _PUSH_OPTS_WITH_VALUE and "=" not in tok:
                j += 1
        elif tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "f":
                    info["force"] = True
                elif ch == "u":
                    info["set_upstream"] = True
                elif ch == "d":
                    info["delete"] = True
                elif ch == "n":
                    info["dry_run"] = True
                elif ch == "o":
                    j += 1
                    break
        else:
            info["positional"].append(tok)
        j += 1
    return info


_COMMIT_MAKERS = {"commit", "merge", "cherry-pick", "rebase", "am", "revert", "pull"}


def _git_subcommand(tokens: list[str]) -> str | None:
    """The git subcommand of a tokenised command, or None when it is not git."""
    start = None
    for i, tok in enumerate(tokens[:5]):
        if Path(tok).name.lower() in ("git", "git.exe"):
            start = i
            break
        if not (re.match(r"^\w+=", tok) or tok in _WRAPPERS):
            return None
    if start is None:
        return None
    i = start + 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _GIT_OPTS_WITH_VALUE else 1
    return tokens[i] if i < len(tokens) else None


def _git_pushes(command: str, workspace: Path | None) -> list[dict]:
    pushes = []
    creates_commits = False
    for tokens, cwd in _segments(command, workspace):
        info = _parse_push(tokens, cwd)
        if info is not None:
            info["evidence"] = " ".join(tokens)
            info["pending_commits"] = creates_commits
            pushes.append(info)
        elif _git_subcommand(tokens) in _COMMIT_MAKERS:
            creates_commits = True
    return pushes


def _strip_heads(name: str) -> str:
    return name[len("refs/heads/"):] if name.startswith("refs/heads/") else name


def _git_push_effect(info: dict, command: str, read_disk: bool) -> dict:
    reason = "Pushed commits or tags to a remote."
    evidence = command[:300]
    positional = info["positional"]
    explicit_remote = positional[0] if positional else None
    refspecs = positional[1:] if positional else []
    force = info["force"]
    git = _find_git(info["repo"]) if read_disk else None
    cfg: dict[str, dict[str, str]] = {}
    packed: dict[str, str] = {}
    head_branch: str | None = None
    head_sha: str | None = None
    if git:
        cfg = _read_config(git[2])
        packed = _packed_refs(git[2])
        head_branch, head_sha = _read_head(git)

    branch_cfg = cfg.get(f'branch "{head_branch}"', {}) if head_branch else {}
    remote = explicit_remote or branch_cfg.get("remote") or "origin"
    if remote == ".":
        remote = "origin"
    remote_url = _strip_creds(cfg.get(f'remote "{remote}"', {}).get("url", "")) or None

    bulk = info["tags"] or info["all"] or info["mirror"]
    suffix = " (+tags)" if info["tags"] else " (all branches)" if info["all"] else " (mirror)" if info["mirror"] else ""

    # resolve the (src, dst) pairs
    pairs: list[tuple[str | None, str | None, bool]] = []  # (src, dst, plus_forced)
    for spec in refspecs:
        plus = spec.startswith("+")
        spec = spec.lstrip("+")
        if info["delete"]:
            pairs.append((None, _strip_heads(spec), plus))
        elif ":" in spec:
            src, dst = spec.split(":", 1)
            if not src:
                info["delete"] = True
            pairs.append((src or None, _strip_heads(dst) or None, plus))
        else:
            pairs.append((spec, _strip_heads(spec), plus))
    if not pairs and not bulk:
        if head_branch:
            upstream = _strip_heads(branch_cfg.get("merge", "")) if branch_cfg.get("merge") else head_branch
            pairs.append((head_branch, upstream or head_branch, False))
        elif git is None:
            pairs.append((None, None, False))

    if any(p for _, _, p in pairs):
        force = True

    pre: dict = {}
    target: str | None = None
    compensation: str | None = None
    note = ""

    if bulk or len(pairs) != 1 or pairs[0][1] is None and pairs[0][0] is None and not git:
        # tags / all / mirror / several refspecs / unknown
        if len(pairs) > 1:
            names = ", ".join(f"{remote}/{d}" for _, d, _ in pairs if d)
            target = names or None
        elif len(pairs) == 1 and pairs[0][1]:
            target = f"{remote}/{pairs[0][1]}{suffix}"
        else:
            target = f"{remote}{suffix}" if (bulk or explicit_remote or git) else None
        if git:
            pre = {"repo": str(git[0]), "remote": remote, "remote_url": remote_url, "branch": None,
                   "local_sha": None, "remote_sha_before": None, "force": force,
                   "delete": info["delete"], "new_branch": False}
        note = _UNKNOWN_REMOTE_NOTE.format(remote=remote)
    else:
        src, dst, _ = pairs[0]
        src_name = src
        if src == "HEAD" or (src is None and not info["delete"]):
            src_name = head_branch
        if dst in (None, "HEAD"):
            dst = src_name or None
        branch = dst
        target = f"{remote}/{branch}{suffix}" if branch else (f"{remote}{suffix}" if explicit_remote else None)
        local_sha = None
        remote_sha = None
        if git:
            if not info["delete"]:
                if src == "HEAD" or (src is None):
                    local_sha = head_sha
                elif src and _SHA_RE.match(src):
                    local_sha = src
                elif src:
                    local_sha = (
                        _read_ref(git, src if src.startswith("refs/") else f"refs/heads/{src}", packed)
                        or _read_ref(git, f"refs/tags/{src}", packed)
                    )
            pending = bool(info.get("pending_commits")) and not info["delete"] and (
                src == "HEAD" or src is None or src == head_branch
            )
            if pending:
                local_sha = None  # HEAD moves before the push runs; it is read again afterwards
            if branch:
                remote_sha = _read_ref(git, f"refs/remotes/{remote}/{branch}", packed)
            new_branch = remote_sha is None and not info["delete"]
            pre = {"repo": str(git[0]), "remote": remote, "remote_url": remote_url, "branch": branch,
                   "local_sha": local_sha, "remote_sha_before": remote_sha, "force": force,
                   "delete": info["delete"], "new_branch": new_branch}
            if pending:
                pre["pending_commits"] = True
            if branch:
                if pending:
                    if remote_sha:
                        compensation = (
                            f"git push --force-with-lease=refs/heads/{branch} "
                            f"{remote} {remote_sha}:refs/heads/{branch}"
                        )
                        note = (
                            "This command creates new commit(s) before pushing; the exact new sha is filled "
                            f"in after the push. The command restores {remote}/{branch} to {_short(remote_sha)}."
                        )
                    else:
                        compensation = f"git push {remote} --delete {branch}"
                        note = (
                            "This command creates new commit(s) before pushing, and no remote-tracking ref "
                            f"for {remote}/{branch} existed locally. Confirm the branch is new before deleting it."
                        )
                elif info["delete"]:
                    if remote_sha:
                        compensation = f"git push {remote} {remote_sha}:refs/heads/{branch}"
                        note = f"Recreates {remote}/{branch} at the commit it had before it was deleted."
                    else:
                        note = _UNKNOWN_REMOTE_NOTE.format(remote=remote)
                elif new_branch:
                    # No remote-tracking ref only means "not seen locally"; in a single-branch clone
                    # or before any fetch the branch may already exist remotely. Lease the delete on
                    # the commit we pushed and tell the human to verify before running it.
                    if local_sha:
                        compensation = (
                            f"git push --force-with-lease=refs/heads/{branch}:{local_sha} "
                            f"{remote} :refs/heads/{branch}"
                        )
                    else:
                        compensation = f"git push {remote} --delete {branch}"
                    note = (
                        f"No remote-tracking ref for {remote}/{branch} existed locally, so this push most "
                        "likely created the branch. Confirm it is new before deleting it."
                    )
                elif remote_sha and local_sha and remote_sha == local_sha:
                    note = "The remote already pointed at this commit; the push should not change it."
                elif remote_sha and local_sha:
                    compensation = (
                        f"git push --force-with-lease=refs/heads/{branch}:{local_sha} "
                        f"{remote} {remote_sha}:refs/heads/{branch}"
                    )
                    note = (
                        f"Restores {remote}/{branch} to the commit it had before this push; "
                        "the lease makes it fail if anyone pushed since."
                    )
            if not compensation and not note:
                note = _UNKNOWN_REMOTE_NOTE.format(remote=remote)
        else:
            note = _UNKNOWN_REMOTE_NOTE.format(remote=remote)

    if force:
        note = f"{note} {_FORCE_NOTE}".strip()
    return _effect("git_push", False, reason, evidence, target, compensation, note, pre)


# --------------------------------------------------------------------------
# other kinds
# --------------------------------------------------------------------------


def _patterns_for(kind: str) -> list[str]:
    return [p for p, k, _ in IRREVERSIBLE_PATTERNS + LOCAL_DESTRUCTIVE_PATTERNS if k == kind]


def _find_segment(kind: str, command: str, workspace: Path | None) -> tuple[list[str], Path | None] | None:
    pats = _patterns_for(kind)
    for tokens, cwd in _segments(command, workspace):
        text = " ".join(tokens)
        for pat in pats:
            try:
                if re.match(pat, text, re.IGNORECASE):
                    return tokens, cwd
            except re.error:
                continue
    return None


def _after(tokens: list[str], word: str) -> list[str]:
    for i, tok in enumerate(tokens):
        if tok.lower() == word:
            return tokens[i + 1:]
    return []


def _first_positional(tokens: list[str], with_value: set[str] = frozenset()) -> str | None:
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in with_value:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        return tok
    return None


def _enrich_npm(effect: dict, command: str, workspace: Path | None) -> None:
    found = _find_segment("npm_publish", command, workspace)
    if not found:
        return
    tokens, cwd = found
    rest = _after(tokens, "publish")
    arg = _first_positional(rest, {"--tag", "--access", "--otp", "--registry", "--workspace", "-w"})
    base = cwd
    if arg and base is not None:
        p = Path(arg).expanduser()
        p = p if p.is_absolute() else base / p
        if p.is_dir():
            base = p
    name = version = None
    if base is not None:
        text = _read_text(base / "package.json")
        if text:
            try:
                data = json.loads(text)
                name, version = data.get("name"), data.get("version")
            except (ValueError, AttributeError):
                pass
    if isinstance(name, str) and isinstance(version, str):
        spec = f"{name}@{version}"
        effect["target"] = spec
        effect["compensation"] = f"npm unpublish {spec}"
        effect["pre_state"] = {"package": name, "version": version}
        effect["compensation_note"] = (
            "npm only allows unpublish within 72 hours and when no other package depends on it; "
            f"otherwise use `npm deprecate {spec} \"<reason>\"`."
        )
    else:
        effect["compensation_note"] = (
            "package.json was not readable, so the package name and version are unknown; "
            "see `npm unpublish <name>@<version>` (allowed within 72 hours) or `npm deprecate`."
        )


_GH_RELEASE_VALUE_OPTS = {"-t", "--title", "-n", "--notes", "-F", "--notes-file", "--target", "-R", "--repo",
                          "--discussion-category", "--notes-start-tag"}


def _enrich_release(effect: dict, command: str, workspace: Path | None) -> None:
    found = _find_segment("github_release", command, workspace)
    if not found:
        return
    tag = _first_positional(_after(found[0], "create"), _GH_RELEASE_VALUE_OPTS)
    if tag:
        effect["target"] = tag
        effect["compensation"] = f"gh release delete {tag} --cleanup-tag --yes"
        effect["pre_state"] = {"tag": tag}
        effect["compensation_note"] = "Deletes the release and its tag from GitHub; downloads already made cannot be recalled."
    else:
        effect["compensation_note"] = "The release tag could not be determined; delete it with `gh release delete <tag> --cleanup-tag --yes`."


def _enrich_docker(effect: dict, command: str, workspace: Path | None) -> None:
    found = _find_segment("docker_push", command, workspace)
    image = _first_positional(_after(found[0], "push")) if found else None
    effect["target"] = image
    effect["pre_state"] = {"image": image} if image else {}
    effect["compensation_note"] = _NO_UNDO_NOTES["docker_push"].format(target=image or "you pushed")


def _enrich_kubectl(effect: dict, command: str, workspace: Path | None) -> None:
    found = _find_segment("kubectl_write", command, workspace)
    effect["compensation_note"] = _NO_UNDO_NOTES["kubectl_write"]
    if not found:
        return
    tokens = found[0]
    verb = next((t.lower() for t in tokens if t.lower() in ("apply", "delete", "patch", "replace")), "")
    path = None
    for i, tok in enumerate(tokens):
        if tok in ("-f", "--filename") and i + 1 < len(tokens):
            path = tokens[i + 1]
        elif tok.startswith("--filename=") or tok.startswith("-f="):
            path = tok.split("=", 1)[1]
    if path:
        effect["target"] = path
        effect["pre_state"] = {"verb": verb, "file": path}
        if verb == "apply":
            effect["compensation"] = f"kubectl delete -f {path}"
            effect["compensation_note"] = (
                "Only if these resources were created by this apply; "
                "for updated deployments use `kubectl rollout undo deployment/<name>`."
            )
        elif verb == "delete":
            effect["compensation"] = f"kubectl apply -f {path}"
            effect["compensation_note"] = f"Restore {path} from the checkpoint first if it changed."


def _enrich_other(effect: dict) -> None:
    template = _NO_UNDO_NOTES.get(effect["kind"])
    if template and not effect.get("compensation_note"):
        effect["compensation_note"] = template.format(target=effect.get("target") or "it")


_GIT_PUSH_HINT = re.compile(r"\bgit\b[^\n]*\bpush\b", re.IGNORECASE)
_GIT_PUSH_PLAIN = re.compile(r"\bgit\s+push\b", re.IGNORECASE)
_DRY_RUN = re.compile(r"--dry-run|\s-n\b")

def _is_dry_run(tokens: list[str]) -> bool:
    """`npm publish --dry-run`, `kubectl apply --dry-run=client`: nothing is changed."""
    for tok in tokens:
        if tok == "--dry-run":
            return True
        if tok.startswith("--dry-run="):
            return tok.split("=", 1)[1].lower() not in ("none", "false")
    return False


def _analyse(tool_name: str, args, workspace: Path | None, read_disk: bool) -> list[dict]:
    effects: list[dict] = []
    seen: set[str] = set()
    if not isinstance(args, dict):
        args = {}
    command = _command_from(args)
    if command:
        reason = IRREVERSIBLE_PATTERNS[0][2]
        if _GIT_PUSH_HINT.search(command):
            pushes = _git_pushes(command, workspace)
            if pushes:
                for info in pushes:
                    if info["dry_run"]:
                        continue
                    try:
                        effects.append(_git_push_effect(info, info["evidence"], read_disk))
                    except Exception:
                        effects.append(_effect("git_push", False, reason, info["evidence"],
                                               note=_UNKNOWN_REMOTE_NOTE.format(remote="origin")))
            seen.add("git_push")
        heads = _segments(command, workspace)
        for pattern, kind, reason in IRREVERSIBLE_PATTERNS:
            if kind == "git_push":
                continue
            try:
                matched = next(
                    (t for t, _c in heads if re.match(pattern, " ".join(t), re.IGNORECASE) and not _is_dry_run(t)),
                    None,
                )
            except re.error:
                continue
            if not matched:
                continue
            if kind in seen:
                continue
            seen.add(kind)
            effect = _effect(kind, False, reason, command)
            try:
                if read_disk or kind in ("docker_push", "kubectl_write", "github_release"):
                    if kind == "npm_publish":
                        if read_disk:
                            _enrich_npm(effect, command, workspace)
                    elif kind == "github_release":
                        _enrich_release(effect, command, workspace)
                    elif kind == "docker_push":
                        _enrich_docker(effect, command, workspace)
                    elif kind == "kubectl_write":
                        _enrich_kubectl(effect, command, workspace)
                if kind == "github_pr":
                    effect["compensation_note"] = "The pull request URL is only known once the call returns; `gh pr close <url>` will close it."
                _enrich_other(effect)
            except Exception:
                pass
            effects.append(effect)
        for pattern, kind, reason in LOCAL_DESTRUCTIVE_PATTERNS:
            if kind in seen:
                continue
            try:
                matched = next((t for t, _c in heads if re.match(pattern, " ".join(t), re.IGNORECASE)), None)
            except re.error:
                continue
            if matched:
                seen.add(kind)
                effects.append(_effect(kind, True, reason, command, note=_LOCAL_NOTE))

    if tool_name not in TERMINAL_TOOLS:
        for key in PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and _outside_workspace(value, workspace):
                effects.append(
                    _effect(
                        "outside_workspace_write",
                        False,
                        "Wrote outside the protected workspace, where no checkpoint exists.",
                        value,
                        target=value[:200],
                        note=_NO_UNDO_NOTES["outside_workspace_write"],
                    )
                )
                break
    return effects


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def classify(tool_name: str, args: dict, workspace: Path | None = None) -> list[dict]:
    """Return the effects a tool call may produce (no disk reads of git state)."""
    try:
        return _analyse(tool_name, args, workspace, read_disk=False)
    except Exception:
        return []


def preflight(tool_name: str, args: dict, workspace: Path | None) -> list[dict]:
    """classify() plus pre_state and compensation computed from files on disk, before the tool runs."""
    try:
        return _analyse(tool_name, args, workspace, read_disk=True)
    except Exception:
        return classify(tool_name, args, workspace)


_EXIT_KEYS = ("exit_code", "returncode", "exit_status", "code")
_FAIL_TEXT = re.compile(r"(error|fatal|rejected|failed to push|permission denied)", re.IGNORECASE)


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _as_text(result) -> str:
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def outcome(tool_name: str, result) -> dict:
    """Return ``{"status": "succeeded"|"failed"|"unknown", "exit_code": int|None, "detail": str}``."""
    try:
        data = result
        if isinstance(result, (bytes, str)):
            text = _as_text(result).strip()
            data = None
            if text[:1] in "{[":
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
            if data is None:
                if not text:
                    return {"status": "unknown", "exit_code": None, "detail": ""}
                status = "failed" if _FAIL_TEXT.search(text) else "unknown"
                return {"status": status, "exit_code": None, "detail": text[-200:]}
        if not isinstance(data, dict):
            return {"status": "unknown", "exit_code": None, "detail": ""}

        output = data.get("output")
        err = data.get("error")
        stderr = data.get("stderr")
        detail_src = err if isinstance(err, str) and err.strip() else (
            output if isinstance(output, str) and output.strip() else (stderr if isinstance(stderr, str) else ""))
        detail = detail_src.strip()[-200:]

        code = None
        for key in _EXIT_KEYS:
            if key in data:
                code = _to_int(data[key])
                if code is not None:
                    break
        if code is not None:
            return {"status": "succeeded" if code == 0 else "failed", "exit_code": code, "detail": detail}
        status_field = str(data.get("status") or "").lower()
        if status_field in ("error", "failed", "failure", "timeout", "timed_out"):
            return {"status": "failed", "exit_code": None, "detail": detail or status_field}
        if (isinstance(err, str) and err.strip()) or (err and not isinstance(err, str)):
            return {"status": "failed", "exit_code": None, "detail": detail or "error"}
        if isinstance(stderr, str) and stderr.strip() and not (isinstance(output, str) and output.strip()) and not data.get("stdout"):
            return {"status": "failed", "exit_code": None, "detail": detail}
        if status_field in ("ok", "success", "succeeded", "completed", "done"):
            return {"status": "succeeded", "exit_code": None, "detail": detail}
        return {"status": "unknown", "exit_code": None, "detail": detail}
    except Exception:
        return {"status": "unknown", "exit_code": None, "detail": ""}


_PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def _resolve_pushed_sha(out: dict) -> None:
    """After a chained `commit && push` ran: read the sha git recorded for the pushed branch."""
    try:
        pre = out["pre_state"]
        repo, remote, branch = pre.get("repo"), pre.get("remote"), pre.get("branch")
        if not (repo and remote and branch):
            return
        git = _find_git(Path(repo))
        if not git:
            return
        sha = _read_ref(git, f"refs/remotes/{remote}/{branch}")
        if not sha:
            return
        before = pre.get("remote_sha_before")
        pre["local_sha"] = sha
        pre["pushed_sha"] = sha
        pre["pending_commits"] = False
        if before and before == sha:
            out["compensation"] = None
            out["compensation_note"] = "The push did not move the remote ref; nothing to undo."
        elif before:
            out["compensation"] = (
                f"git push --force-with-lease=refs/heads/{branch}:{sha} {remote} {before}:refs/heads/{branch}"
            )
            out["compensation_note"] = (
                f"Restores {remote}/{branch} to the commit it had before this push; "
                "the lease makes it fail if anyone pushed since."
            )
        else:
            out["compensation"] = (
                f"git push --force-with-lease=refs/heads/{branch}:{sha} {remote} :refs/heads/{branch}"
            )
            out["compensation_note"] = (
                f"No remote-tracking ref for {remote}/{branch} existed before, so this push most likely "
                "created the branch. Confirm it is new before deleting it."
            )
    except Exception:
        pass


def finalize(effect: dict, result, workspace: Path | None) -> dict:
    """Copy of ``effect`` enriched with post-call facts (e.g. PR URL) and the call ``outcome``."""
    try:
        out = dict(effect)
        out["pre_state"] = dict(effect.get("pre_state") or {})
        res = outcome("", result)
        out["outcome"] = res
        out["executed"] = res["status"] != "failed"
        if out.get("kind") == "git_push" and out["executed"] and out["pre_state"].get("pending_commits"):
            _resolve_pushed_sha(out)
        if out.get("kind") == "github_pr":
            text = _as_text(result)
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    text = " ".join(str(v) for v in parsed.values() if isinstance(v, (str, int)))
            except ValueError:
                pass
            m = _PR_URL.search(text)
            if m:
                url = m.group(0)
                out["target"] = f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
                out["compensation"] = f"gh pr close {url}"
                out["compensation_note"] = "Closes the pull request; it stays visible on GitHub and can be reopened."
                out["pre_state"]["url"] = url
        return out
    except Exception:
        return dict(effect) if isinstance(effect, dict) else {}
