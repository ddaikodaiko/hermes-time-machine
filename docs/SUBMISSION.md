# Catalog submission

Everything needed to open the PR against `NousResearch/hermes-agent`, plus the
Typeform answers. Replace `PINNED_SHA` after the final commit is pushed.

## Catalog entry

Place this file at `plugin-catalog/hermes-time-machine.yaml` in the PR.

```yaml
name: hermes-time-machine
repo: https://github.com/ddaikodaiko/hermes-time-machine
sha: PINNED_SHA
version: "0.1.0"
title: Hermes Time Machine
description: "Policy-driven workspace checkpoints for Hermes: one checkpoint before the first file-changing call of each turn and before risky commands (rm -rf, git push --force, DROP TABLE, writes outside the workspace), one-command rewind, fork into a sibling directory, and an effect ledger that refuses to hide actions a rewind cannot undo. Disclosure - writes a tar.gz snapshot of the configured workspace and JSONL journal/effect ledgers under HERMES_HOME/plugin-data/hermes-time-machine, keeps the last 10 pre-rewind copies in a trash directory, spawns no processes, makes no network calls, reads no credentials, and never blocks, approves or bypasses a tool call; a rewind moves the current workspace contents to trash and extracts the checkpoint over the workspace, refusing filesystem roots, the home directory, directories containing home, system directories, and workspaces overlapping plugin data."
maintainer: ddaikodaiko
tier: community
category: tools
requires_hermes: ">=0.19.0"
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
  provides_middleware: []
  requires_env: []
```

The field names matter: the schema uses `capabilities.provides_tools` and
`capabilities.provides_hooks`, not `capabilities.tools` / `capabilities.hooks`.
`tier` must stay `community`; only Nous maintainers set `verified` / `official`.

## PR title

`Add hermes-time-machine: policy-driven workspace checkpoints with an effect ledger`

## PR body

```markdown
## What it does

`hermes-time-machine` checkpoints the workspace before the first file-changing
call of each turn and before risky commands (`rm -rf`, `git push --force`,
`DROP TABLE`, writes outside the workspace). It restores the workspace with one
command. It also records external effects that a rewind cannot undo, and
refuses to hide them.

## Hermes surfaces used

- `pre_tool_call` — risk scoring and automatic checkpoint creation (never blocks).
- `post_tool_call` — effect ledger.
- `ctx.register_tool` — `atm_checkpoint`, `atm_rewind`, `atm_fork`, `atm_list`, `atm_effects`.
- Plugin data root — checkpoints, journal and effects survive updates.

## Disclosure

- No shell commands, no network calls, no background processes, no credentials.
- Reads and writes the configured workspace and the plugin data directory.
- A rewind moves the current workspace contents to a trash directory and
  extracts a checkpoint over the workspace. It refuses filesystem roots, the
  home directory, directories containing home, system directories, and
  workspaces overlapping plugin data.
- Automatic snapshots are capped by `max_snapshot_mb` (default 200 MB) and
  pruned by `retention` (default 20). Manual checkpoints are never pruned.

## Validation

- [x] Loads in Hermes 0.19.0 with 5 tools and 2 hooks (`PluginManager` integration test).
- [x] `python -m unittest discover -s tests -v` — 13 tests passing.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [ ] `hermes plugins doctor . --ci` / `hermes plugins validate . --install-deps`
      (not present in 0.19.0; run if your build provides them).

## Demo

<90 s: [link]
```

## Typeform answers (draft)

- **Plugin name:** hermes-time-machine
- **Repository:** https://github.com/ddaikodaiko/hermes-time-machine
- **Catalog PR:** https://github.com/NousResearch/hermes-agent/pull/149553
- **Demo link:** TODO
- **One-line pitch:** Policy-driven workspace checkpoints for Hermes, with an effect ledger that tells you what a rewind cannot undo.
- **What problem does it solve?** Agents break workspaces and push to remotes; existing undo only covers the agent's own file edits.
- **What Hermes surfaces does it use?** `pre_tool_call`, `post_tool_call`, `ctx.register_tool`, plugin data storage.
- **What is intentionally out of scope?** RAM/process snapshots and conversation rewind. FS-first is the catalog-safe MVP.

## Submission checklist

- [x] Public repo owned by the submitter (catalog rule 5).
- [x] 40-character SHA pinned.
- [x] Plugin loads and registers the declared tools/hooks.
- [x] `provides_tools` / `provides_hooks` match registration.
- [x] `requires_hermes` is a SemVer floor, not a date.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [ ] Demo posted on X tagging `@NousResearch`.
- [ ] Typeform submitted before 11 Nov 2026, 17:00 EST.
