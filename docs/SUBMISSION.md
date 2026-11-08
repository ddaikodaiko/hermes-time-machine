# Catalog submission (v0.2.0)

Everything needed to open the PR against `NousResearch/hermes-agent`, plus the Typeform
answers. Replace `PINNED_SHA` with the 40-character commit after the final push.

## Catalog entry

Place this file at `plugin-catalog/hermes-time-machine.yaml` in the PR.

```yaml
name: hermes-time-machine
repo: https://github.com/ddaikodaiko/hermes-time-machine
sha: PINNED_SHA
version: "0.2.0"
title: Hermes Time Machine
description: "The rollback that knows what it can't undo. Incremental, time-bounded checkpoints that the agent itself can preview, restore or fork (atm_* tools, /timemachine); an approval at the point of no return (git push, npm publish, terraform apply and similar) that shows what becomes permanent and the exact compensation command; an effect ledger; and effect memory that reminds the model of pushes it can no longer see after /rollback, /undo, /retry or compaction. Disclosure — no shell commands, subprocesses, network calls, background processes or credentials; reads workspace files plus .git refs/config and package.json to capture pre-state; writes only under HERMES_HOME/plugin-data/hermes-time-machine and, on a rewind, the workspace (replaced files go to a trash directory); never approves, blocks or bypasses a tool call by itself; refuses filesystem roots, home and system directories."
maintainer: ddaikodaiko
tier: community
category: tools
requires_hermes: ">=0.23.0"
docs_url: https://github.com/ddaikodaiko/hermes-time-machine#readme
platforms: [linux, macos, windows]
capabilities:
  provides_tools:
    - atm_checkpoint
    - atm_rewind
    - atm_fork
    - atm_list
    - atm_effects
  provides_hooks:
    - pre_tool_call
    - post_tool_call
    - transform_tool_result
    - pre_llm_call
    - post_compaction
  provides_middleware: []
  requires_env: []
```

The description is about 900 characters; trim it if the catalog schema enforces a lower limit.
The field names matter: `capabilities.provides_tools` and `capabilities.provides_hooks`.
`tier` stays `community`; only Nous maintainers set `verified` / `official`. `platforms`
lists linux, macos and windows because the plugin is pure standard-library Python, but it
has only been run on Windows so far (unverified elsewhere).

## PR title

`Add hermes-time-machine: checkpoints, a point-of-no-return gate and effect memory`

## PR body

```markdown
## What it does

`hermes-time-machine` is the rollback that knows what it can't undo. It adds:

- agent-visible, incremental, time-bounded checkpoints (`atm_checkpoint`, `atm_list`,
  `atm_rewind`, `atm_fork`) and a `/timemachine` command;
- an approval at the point of no return: before `git push`, `npm publish`,
  `terraform apply` and similar, Hermes' own approval prompt shows what becomes
  permanent and the command that would compensate it;
- an effect ledger with the pre-state needed to compensate each irreversible effect;
- effect memory: after `/rollback`, `/undo`, `/retry` or compaction, the model is
  reminded of irreversible effects it can no longer see.

It complements core `/rollback` (`checkpoints.enabled`), which restores files and the last
chat turn but does not know about pushes or publishes.

## Hermes surfaces used

- `pre_tool_call` — automatic checkpoint, and an `approve` directive for irreversible
  effects. Never raises (0.23 fails closed) and bounds automatic captures by
  `auto_budget_seconds`.
- `post_tool_call` — records effects that actually ran in the ledger.
- `transform_tool_result` — tells the model where its restore point is.
- `pre_llm_call` — effect memory, returns `{"context": ...}`.
- `post_compaction` — records session-id lineage so effect memory survives compaction.
- `ctx.register_tool` (5 tools), `ctx.register_command` (`/timemachine`), plugin data root.

## Disclosure

- No shell commands, no subprocesses, no network calls, no background processes, no credentials.
- Reads files in the protected workspace to snapshot them, and `.git/HEAD`, `.git/config`,
  refs and `package.json` to capture pre-state (never by running `git` or `npm`).
- Writes only under `HERMES_HOME/plugin-data/hermes-time-machine`, and into the workspace
  when a rewind is confirmed. Replaced files go to a trash directory (last 10 rewinds kept).
- Never approves, blocks or bypasses a tool call by itself; it only returns an `approve`
  directive that Hermes' approval flow handles.
- `assert_safe` refuses filesystem roots, the home directory, directories containing it,
  system directories and workspaces overlapping the plugin data directory.
- Automatic snapshots are capped by `max_snapshot_mb` (200) and pruned by `retention` (20).

## Validation

- [x] Hermes Agent 0.23.0: `hermes plugins validate` passes (security scan: safe, no core override).
- [x] `hermes plugins doctor --ci` passes.
- [x] `python -m unittest discover -s tests`: 99 tests discovered, 8 of them
      integration tests that load the plugin through the real 0.23 `PluginManager` and
      hook dispatch (they skip when Hermes is not installed).
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [x] `python demo/run_demo.py` replays the full flow without Hermes.

## Demo

<90 s: [link]
```

## Typeform answers (draft)

- **Plugin name:** hermes-time-machine
- **Repository:** https://github.com/ddaikodaiko/hermes-time-machine
- **Catalog PR:** PR_URL (reopened with the v0.2.0 SHA)
- **Demo link:** TODO
- **One-line pitch:** The rollback that knows what it can't undo.
- **What problem does it solve?** Rewinding files is solved, but `/rollback`, `/undo`, `/retry`
  and compaction also rewind the conversation: the model forgets it pushed, the remote does
  not. The plugin asks for approval at the point of no return, keeps a ledger of what a
  rewind cannot undo with the compensation command, and reminds the model after a rewind.
- **What Hermes surfaces does it use?** `pre_tool_call`, `post_tool_call`,
  `transform_tool_result`, `pre_llm_call`, `post_compaction`, five tools, the `/timemachine`
  command and plugin data storage.
- **How does it relate to core `/rollback`?** Complementary. `/rollback` restores files and
  the last turn; this adds what it lacks: agent-callable rewind, push awareness, compensation
  and effect memory.
- **How is it reliable?** Hooks never raise (Hermes 0.23 fails closed), automatic captures are
  time-bounded and resumable, restores are verified before they start and move replaced
  files to trash. Tests include a run through the real Hermes plugin manager.
- **What is intentionally out of scope?** RAM and process state, and executing compensation
  (it computes the command and asks; it never runs it).
- **Benchmark caveat:** measured on a Windows laptop with real-time antivirus (worst case):
  2,005 files / 53.4 MB, unchanged re-capture 1.07 s, restore of 10 files 1.45 s.

## Submission checklist

- [x] Public repo owned by the submitter (catalog rule 5).
- [ ] 40-character SHA pinned (replace `PINNED_SHA`).
- [x] Plugin loads and registers the declared tools and hooks.
- [x] `provides_tools` / `provides_hooks` match registration.
- [x] `requires_hermes` is a SemVer floor, not a date.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [x] Disclosure sentence at the end of the description (rule 13).
- [ ] Catalog PR reopened with the new SHA.
- [ ] Demo recorded and linked in the PR body.
- [ ] Demo posted on X tagging `@NousResearch`.
- [ ] Typeform submitted before Wed 11 Nov 2026, 17:00 EST.
