# Design notes

## The gap

Coding agents now have write access to real repositories. The failure mode is
not that the model cannot edit files; it is that a bad edit lands before anyone
notices, and the recovery path assumes the user committed first.

Git covers committed history. It does not cover:

- uncommitted work,
- untracked files created by the agent,
- generated files that were never meant to be committed,
- the moment between "the agent ran a command" and "the user reviewed the diff".

Hermes already gives the user approvals and a tool-call trace. What it lacks is
a **restore point that exists before the damage**, and an honest account of the
damage a restore cannot reach.

## Why filesystem-first

A full time machine would snapshot RAM, process state and external services.
That is the wrong first version:

| Approach | Why not for the catalog MVP |
|---|---|
| CRIU / process checkpoint | Requires native binaries, root/privileged mode, and Linux-specific kernel features. Fails catalog rules on binaries and platform support. |
| MicroVM hot snapshots | Requires a VM provider and network credentials; not portable to the local CLI. |
| Conversation rewind | Hermes owns the session store. A plugin that rewrites it would be a core override and a data-loss risk. |
| External-service undo | There is no generic undo for a push, an email or a payment. Pretending otherwise is worse than saying so. |
| Git-only | Misses uncommitted and untracked state, which is exactly where agents operate. |

The filesystem is the part a plugin can snapshot safely, portably and without
privileges. It is also the part users care about most in the first minute after
an agent goes wrong.

## Architecture

```
pre_tool_call ──> policy.evaluate ──> checkpoint (tar.gz) ──> tool runs
                                                │
post_tool_call ─> effects.classify ─────────────┴──> effects.jsonl
                                                │
atm_rewind ─────> preview ──> confirm ──> move current -> trash/ -> extract
```

- **Hooks** are fail-open. A checkpoint failure never blocks a tool call and
  never bypasses an approval.
- **Tools** are explicit: the model can checkpoint, preview, restore, fork and
  inspect effects.
- **Storage** lives outside the install tree, so `hermes plugins update` and
  `remove` do not delete user data.

## Policy

Two triggers, because two different things go wrong:

1. **Risk trigger.** A terminal command matches a destructive or remote-write
   pattern, a write tool targets a path outside the workspace, or the command
   has a known irreversible effect. Score 0.75–0.90.
2. **Mutation trigger.** The first file-changing call of a turn (`write_file`,
   `patch`, `rm`, `mv`, `sed -i`, migrations, installs) takes one checkpoint.
   Later edits in the same turn reuse it.

The mutation trigger is what makes the tool feel automatic: the restore point
exists before the agent starts editing, not after the user notices. It is
bounded by `max_snapshot_mb` and pruned by `retention`, so a large repository
does not turn into an accidental backup service.

The hook never returns `block`. Blocking is Hermes' approval system's job; this
plugin only prepares the escape route.

## Effect ledger

A rewind that silently hides a `git push` is a lie. The ledger records:

- irreversible external effects (`git push`, `gh pr create`, `npm publish`,
  `docker push`, `kubectl apply`, `terraform apply`, cloud writes, HTTP writes,
  SQL writes, email, remote shell),
- reversible local destruction (`rm -rf`, `git reset --hard`, `git clean -f`),
- writes outside the protected workspace.

`atm_rewind` previews by default and refuses to restore while irreversible
effects exist after the checkpoint. `allow_irreversible=true` does not undo
them; it acknowledges them. That distinction is the core of the product.

## Safety model

`assert_safe` refuses to protect:

- filesystem and drive roots,
- the home directory,
- directories that contain the home directory,
- system directories,
- workspaces that overlap the plugin data directory.

A restore moves the current contents to `trash/` before extracting. If
extraction fails, it moves them back. Trash keeps the last 10 rewind backups.

## Evaluation

Measured on the demo workspace (2 files, ~300 bytes) on Windows, Python 3.14:

- checkpoint creation: < 10 ms,
- restore: ~18 ms,
- policy evaluation: microseconds, no filesystem walk.

The integration suite loads the plugin through the real Hermes
`PluginManager`, fires the real hooks and calls the tools through
`tools.registry`; it is not a mock of the plugin API.

## Limitations and next steps

- **No RAM/process snapshot.** The roadmap is to use Hermes' existing remote
  backends (Modal/Daytona) for hot pause, not native binaries.
- **No conversation rewind.** That belongs in Hermes core or a session hook.
- **No incremental snapshots yet.** v0.2 will use content-addressed chunks.
- **Large repositories.** `max_snapshot_mb` protects the user; a future version
  should measure incrementally and snapshot only changed paths.
- **Windows metadata.** tar preserves paths and content; ACLs and some file
  attributes are not restored.

The Spanish research report that motivated these choices lives outside this
repository (`informe-time-travel-rollback-nov2026.md`).
