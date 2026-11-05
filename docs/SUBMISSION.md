# Catalog entry (template)

Place this file at `plugin-catalog/hermes-time-machine.yaml` in a PR to
`NousResearch/hermes-agent`. Replace the SHA after the final commit.

```yaml
name: hermes-time-machine
title: Hermes Time Machine
description: >
  Policy-driven workspace checkpoints with one-command rewind, fork, and an
  effect ledger that flags actions a rollback cannot undo (git push, publishes,
  HTTP writes, cloud changes).
repo: https://github.com/ddaikodaiko/hermes-time-machine
sha: TODO_40_CHARACTER_COMMIT_SHA
version: "0.1.0"
tier: community
category: tools
maintainer: ddaikodaiko
requires_hermes: ">=0.19.0"
capabilities:
  tools:
    - atm_checkpoint
    - atm_rewind
    - atm_fork
    - atm_list
    - atm_effects
  hooks:
    - pre_tool_call
    - post_tool_call
  env: []
```

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

## Validation

- [x] Loads in Hermes 0.19.0 with 5 tools and 2 hooks (`PluginManager` integration test).
- [x] `python -m unittest discover -s tests -v` — 11 tests passing.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [ ] `hermes plugins doctor . --ci` / `hermes plugins validate . --install-deps`
      (not present in 0.19.0; run if your build provides them).

## Demo

<90 s: [link]
```

## Typeform answers (draft)

- **Plugin name:** hermes-time-machine
- **Repository:** https://github.com/ddaikodaiko/hermes-time-machine
- **Catalog PR:** https://github.com/NousResearch/hermes-agent/pull/TODO
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
