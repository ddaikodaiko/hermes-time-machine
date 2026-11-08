# Design notes (v0.2)

Thesis: **every rollback restores files; this one knows what a rollback can't undo.**

Target: Hermes Agent 0.23.0. Python standard library only.

## History in one paragraph

v0.1 (5 Nov) was a tar-based checkpoint tool with an effect ledger. On 7 Nov, reading
Hermes 0.23, two facts changed the design. First, core `/rollback` (opt-in via
`checkpoints.enabled`) already restores files and the last chat turn through a shadow
git store, so "another file snapshotter" is not the point. Second, `pre_tool_call`
**fails closed**: a hook that raises or times out blocks the tool. v0.2 therefore
moves the weight from restoring files to what file restore cannot do, and rebuilds the
checkpoint engine so it can never stall a tool call.

## Architecture

```
                      Hermes 0.23 agent loop
  ┌───────────────────────────────────────────────────────────────────┐
  │                                                                   │
  │  user turn ─► pre_llm_call ───────────────────────────┐           │
  │                                                       ▼           │
  │                                              memory.forgotten()   │
  │                                              memory.render()      │
  │                                              {"context": ...}     │
  │  model ─► tool call                                               │
  │             │                                                     │
  │             ▼                                                     │
  │      pre_tool_call ──► effects.preflight  (parse + read .git)     │
  │             │          policy.evaluate    (risk score)             │
  │             │          handlers.create_checkpoint (auto, bounded)  │
  │             │          gate.decide  ──► {"action":"approve",...}   │
  │             ▼                              │                      │
  │      Hermes approval prompt ◄──────────────┘  (human decides)     │
  │             ▼                                                     │
  │          tool runs                                                │
  │             ▼                                                     │
  │      post_tool_call ──► effects.finalize ──► LEDGER (effects.jsonl)│
  │             ▼                                                     │
  │      transform_tool_result ──► adds "time_machine" note           │
  │                                                                   │
  │  context compaction ─► post_compaction ──► session lineage        │
  └───────────────────────────────────────────────────────────────────┘

   ENGINE (snapshot.py + handlers.py)              STATE (HERMES_HOME/plugin-data/hermes-time-machine)
   capture / diff / restore / materialize          objects/  content-addressed files (sha256)
   resumable, deadline-bounded                     checkpoints/<id>/manifest.json  (tree)
   tools: atm_checkpoint atm_list atm_rewind       seeds/    partial captures to resume
          atm_fork atm_effects  + /timemachine     trash/    files replaced by a rewind
                                                   effects.jsonl  journal.jsonl  lineage.json
```

Module map: `guard.py` (hooks), `effects.py` (classification, pre-state, compensation),
`gate.py` (approval message), `memory.py` (effect memory), `policy.py` (risk and mutation
patterns), `snapshot.py` (engine), `handlers.py` (tools and ledger queries), `commands.py`
(`/timemachine`), `config.py`, `storage.py`.

## Why each pillar

1. **Point of no return.** The cheapest moment to prevent a mistake is the moment before
   it, and Hermes already has an approval UI. The plugin adds content, not a new UI: what
   becomes permanent, and the command that would compensate it. It never approves,
   never blocks by itself, and the directive carries a `rule_key` per kind and target so the
   user can approve "for this session" without opening the gate for everything.
2. **Effect ledger.** A restore that cannot say what it did not restore is misleading. The
   ledger stores each irreversible effect with its pre-state, but only after
   `post_tool_call` says it ran; failed attempts are stored as `failed` and ignored by the
   gate and by rewind previews.
3. **Effect memory.** `/rollback`, `/undo`, `/retry` and compaction shorten the history the
   model sees, not the world. The plugin closes that gap without needing to know which of
   those happened.
4. **Agent-visible checkpoints.** Core `/rollback` is not a tool. The agent can only fix a
   bad refactor by asking the human. Here the agent can checkpoint, diff, restore a path or
   fork, and the preview tells it what stays true outside.

## The fail-closed constraint and resumable capture

In Hermes 0.23 a `pre_tool_call` callback that raises, or runs past its time limit
(30 s according to the hook docs the code was written against; unverified here beyond the
integration test), blocks the tool call. Consequences:

- Every hook body is wrapped in `try/except Exception`; failures go to `journal.jsonl` as
  `hook_error` and the hook returns `None`.
- Automatic checkpoints get `auto_budget_seconds` (default 5 s, clamped to 0.5 to 20 s).
  Manual ones get 120 s.
- A capture that runs out of time raises `SnapshotTimeout` carrying the files already
  hashed. `create_checkpoint` stores them as a **seed**
  (`seeds/<sha256(workspace)[:16]>.json`). The next capture starts from the newest
  manifest merged with the seed, so already-hashed files cost one `stat()`. A big
  workspace is therefore captured over a few automatic attempts, never in one blocking call.
- Before an automatic capture, `measure` walks the tree with an early exit at
  `max_snapshot_mb`; over the limit the checkpoint is skipped and journaled.
- Reuse check: a file is reused when size and `mtime_ns` equal the previous tree and its
  object still exists; otherwise it is hashed (sha256, 1 MiB chunks) and stored once.

Checkpoint = a manifest (`tree`: relative path to `{h, s, m, x}` plus dirs and skipped
files). Content lives once in `objects/<h[:2]>/<h>`. Diff, single-path restore and fork
come from the tree. Pruning removes automatic manifests beyond `retention`, then garbage
collects unreferenced objects (seeds count as references).

v0.1 tar checkpoints are not restorable by 0.2 (no released users); `_load` ignores them.

## Effect memory algorithm

`pre_llm_call(session_id, conversation_history)`:

1. Read the ledger. Candidates are records whose `session_id` is the current id or one of its ancestors, `reversible == false`
   and `status` in `{None, succeeded}`.
2. Compute what the model can still see: the set of `tool_call_id` values found on tool
   messages and in assistant `tool_calls[].id`, plus the concatenated `function.arguments`.
3. A candidate is **visible** if its `tool_call_id` is in that set. If the record has no
   `tool_call_id`, fall back to evidence: is the recorded command string (raw or
   JSON-escaped) present in the arguments text?
4. Invisible candidates not already reminded in this process (`_NOTIFIED[session]`, key
   `ts|tool_call_id`) are rendered (`memory.render`: up to 5 items, 1,500 characters) and
   returned as `{"context": text}`. Each effect is reminded once.
5. The closing line is `FOOTER`, or `FOOTER_NO_COMMAND` when no listed effect has a compensation command.
6. An empty history or missing session id yields nothing.

**Compaction lineage.** A compaction can rotate the session id, which would orphan the
records written under the old id. `post_compaction(session_id, old_session_id)` stores
`new -> old` in memory and in `lineage.json` (bounded). `pre_llm_call` then treats the
current session id, its ancestors (depth-limited, cycle-safe) and the `parent_session_id`
kwarg, when the host passes one, as the same conversation. Effects recorded under an
ancestor id stay candidates, so the reminder survives compaction.

Known gap: `_NOTIFIED` is process memory, so after a Hermes restart a reminder can repeat
once.

## Compensation recipes (from `effects.py`)

| Kind | Trigger | Pre-state read from | Compensation shown |
|---|---|---|---|
| `git_push` | `git push` (not `--dry-run`) | `.git/HEAD`, `config`, refs, `packed-refs` | Update: `git push --force-with-lease=refs/heads/<b>:<new> <remote> <old>:refs/heads/<b>`. New branch: `git push --force-with-lease=refs/heads/<b>:<new> <remote> :refs/heads/<b>` (with a "confirm it is new" note). Deleted branch: `git push <remote> <old>:refs/heads/<b>`. Tags, `--all`, `--mirror`, several refspecs: no command, a note to inspect `git ls-remote`. |
| `npm_publish` (npm, yarn, pnpm) | `... publish` | `package.json` name and version | `npm unpublish <name>@<version>`; note about the 72-hour window and `npm deprecate`. |
| `github_pr` | `gh pr create` | PR URL from the result (`finalize`) | `gh pr close <url>`. |
| `github_release` | `gh release create <tag>` | tag from the command | `gh release delete <tag> --cleanup-tag --yes`. |
| `kubectl_write` | `kubectl apply/delete/patch/replace` | `-f` file | `apply`: `kubectl delete -f <file>`; `delete`: `kubectl apply -f <file>`; else a note. |
| `docker_push` | `docker push <image>` | image name | none; delete the tag in the registry. |
| `terraform_apply` | `terraform apply` | none | none; restore config, `plan` and `apply`; `terraform.tfstate.backup`. |
| `aws_write`, `gcloud_write`, `azure_write` | CLI create/delete/update | none | none; revert in the provider. |
| `http_write` | `curl -X POST/PUT/PATCH/DELETE`, `--data`, `wget --post-*` | none | none; the service's own API. |
| `database_write` | `psql/mysql/sqlite3` with drop, truncate, delete, update, insert | none | none; restore from backup. |
| `email` | `mail`, `sendmail` | none | none; send a correction. |
| `remote_shell` | `ssh ... rm|systemctl|docker|kubectl` | none | none; revert on that host. |
| `outside_workspace_write` | non-terminal tool with an absolute path outside the workspace | none | none; no checkpoint holds the previous content. |

Reversible local effects (`workspace_delete`, `git_reset`, `git_clean`) are recorded and
not gated; the checkpoint is the compensation.

Parsing rules worth knowing: shell redirections (`2>&1`, `> log`, `&>/dev/null`, `1>>out`) are stripped first, so `git push origin main 2>&1` gets the normal compensation; commands are split on `&&`, `||`, `;`, `|` and newlines
outside quotes; `sudo`, `env`, `time`, `command`, `nohup`, `exec` wrappers and
`sh/bash/zsh/dash -c` are unwrapped; `cd` is followed so `cd repo && git push` reads the
right repo; credentials in remote URLs are stripped.

## Safety model

- `snapshot.assert_safe` runs on every resolution of the workspace and again on restore.
- Restore: diff, then `_check_objects` (all needed objects exist) before touching anything;
  modified files are copied to `trash/rewind_<stamp>_<id>/` and replaced through a temp
  file and `os.replace`; files added since the checkpoint are moved to trash; empty
  directories that did not exist are removed. Per-file failures are reported, not fatal.
- Checkpoint ids must match `ckpt_YYYYMMDDTHHMMSS_xxxxxxxx`; tree paths are validated as
  relative, inside the workspace, on both POSIX and Windows flavours.
- Data directories get restrictive permissions where the OS supports them.

## Limitations

- Filesystem only: no RAM, processes, containers, databases.
- No external undo: compensation is computed and shown, never executed.
- Remote-tracking refs can be stale; the lease protects the compensation, and messages say
  when the remote state is unknown.
- Windows ACLs and extended attributes are not restored.
- Detecting core `/rollback` is indirect (history only). A rollback to a point before the
  push removes the push from the history, which is the signal.
- Directory names in `BASE_EXCLUDES` (`dist`, `build`, `target`, ...) are skipped
  everywhere in the tree.
- Benchmarks are from one Windows machine with real-time antivirus; see the README.

## Evaluation

99 tests via `python -m unittest discover -s tests` (8 of them integration
tests that need Hermes 0.23 installed and skip otherwise), covering the engine, resumable
capture, effect parsing, the gate message, effect memory and the full push, rollback,
remind, rewind flow. The integration tests load the plugin through the real Hermes
`PluginManager` and fire hooks through Hermes' dispatch. `python demo/run_demo.py` replays
the flow end to end without Hermes.
