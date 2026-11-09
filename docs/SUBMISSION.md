# Catalog submission (v0.3.0)

Record of the Hermes Agent Plugin Hackathon submission and the catalog entry currently proposed.

## Status (9 Nov 2026)

| Piece | State | Link |
|---|---|---|
| First catalog entry (v0.2.1, pin `1ac9c78`) | Merged by teknium1 on 9 Nov | [NousResearch/hermes-agent#149553](https://github.com/NousResearch/hermes-agent/pull/149553) |
| SHA bump to v0.3.0 (pin `78ce6f5`) | Open, awaiting review (rule 4) | [NousResearch/hermes-agent#152080](https://github.com/NousResearch/hermes-agent/pull/152080) |
| Marketplace page | Live (shows the merged pin until #152080 lands) | https://hermes-agent.nousresearch.com/marketplace/hermes-time-machine |
| Demo on X (69 s, v0.2.1) | Posted, tags `@NousResearch` | https://x.com/daik0zz/status/2119454575119737147 |
| Demo on YouTube (82 s, adds the v0.3 dashboard) | Public | https://youtu.be/i84_S98vUs8 |
| Typeform | Submitted on 9 Nov | — |

History of #152080: it first proposed 0.2.2 at `f944ab9`, then 0.3.0 at
`3469f87`, then `78ce6f5` (same code, README links the YouTube demo). It stays a single
one-file bump from `1ac9c78`.

## Catalog entry (as proposed in #152080)

```yaml
name: hermes-time-machine
repo: https://github.com/ddaikodaiko/hermes-time-machine
sha: 78ce6f538892217cbe6f51f258897da63d3a882c
version: "0.3.0"
title: Hermes Time Machine
description: "The rollback that knows what it can't undo. Incremental, time-bounded checkpoints that the agent itself can preview, restore or fork (atm_* tools, /timemachine); an approval at the point of no return (git push, npm/cargo/PyPI/gem publish, terraform apply, and MCP or plugin tools that open PRs, merge, release, send messages or deploy) that shows what becomes permanent and the exact compensation command; an effect ledger; effect memory that reminds the model of pushes it can no longer see after /rollback, /undo, /retry or compaction; and a read-only Time machine dashboard tab that shows checkpoints next to the effects no restore can undo, with per-file diffs and undo commands. Every restore takes a safety checkpoint first, so it can be undone. Disclosure — no shell commands, subprocesses, network calls, background processes or credentials; reads workspace files plus .git refs/config, package.json, Cargo.toml and pyproject.toml to capture pre-state; writes only under HERMES_HOME/plugin-data/hermes-time-machine, on a rewind the workspace (replaced files go to a trash directory), and on atm_fork a new sibling folder next to the workspace; adds a restore-point note to tool results and effect reminders to the model's context; asks for approval through Hermes before every model-requested restore and before irreversible commands and tool calls, and never approves or blocks a tool call by itself; the dashboard tab adds three read-only GET routes (no restore, write or process); refuses filesystem roots, home and system directories."
maintainer: ddaikodaiko
tier: community
category: tools
requires_hermes: ">=0.23.0"
docs_url: https://github.com/ddaikodaiko/hermes-time-machine#readme
image: https://raw.githubusercontent.com/ddaikodaiko/hermes-time-machine/78ce6f538892217cbe6f51f258897da63d3a882c/docs/assets/banner.png
screenshots:
  - https://raw.githubusercontent.com/ddaikodaiko/hermes-time-machine/78ce6f538892217cbe6f51f258897da63d3a882c/docs/assets/dashboard.png
  - https://raw.githubusercontent.com/ddaikodaiko/hermes-time-machine/78ce6f538892217cbe6f51f258897da63d3a882c/docs/assets/approval.png
  - https://raw.githubusercontent.com/ddaikodaiko/hermes-time-machine/78ce6f538892217cbe6f51f258897da63d3a882c/docs/assets/rollback.png
  - https://raw.githubusercontent.com/ddaikodaiko/hermes-time-machine/78ce6f538892217cbe6f51f258897da63d3a882c/docs/assets/remembers.png
platforms: [linux, macos, windows]
capabilities:
  provides_tools:
    - atm_checkpoint
    - atm_rewind
    - atm_fork
    - atm_list
    - atm_effects
    - atm_diff
  provides_hooks:
    - pre_tool_call
    - post_tool_call
    - transform_tool_result
    - pre_llm_call
    - post_compaction
  provides_middleware: []
  requires_env: []
```

Images live on `raw.githubusercontent.com`, pinned to the same commit as `sha`, as the catalog
README asks. `tier` stays `community`; only Nous maintainers set `verified` / `official`.

## Hermes surfaces used

- `pre_tool_call`: automatic checkpoint, and an `approve` directive for irreversible effects
  of terminal commands and of MCP/plugin tools. Never raises (0.23 fails closed); automatic
  captures are bounded by `auto_budget_seconds`.
- `post_tool_call`: records effects that actually ran in the ledger.
- `transform_tool_result`: tells the model where its restore point is.
- `pre_llm_call`: effect memory, returns `{"context": ...}`.
- `post_compaction`: session-id lineage so effect memory survives compaction.
- `ctx.register_tool` (6 tools), `ctx.register_command` (`/timemachine`), plugin data root.
- Dashboard plugin (`dashboard/manifest.json`, `dist/index.js`, `plugin_api.py`): one tab, the
  `sessions:top` slot, three read-only GET routes behind the dashboard's auth.

## Validation at the proposed pin

- [x] Hermes Agent 0.23.0: `hermes plugins validate` passes (security scan safe, no core
      override, declared tools and hooks match; dashboard backend served through the plugin host).
- [x] `hermes plugins doctor` passes: 6 tools, 5 hooks.
- [x] `python -m unittest discover -s tests`: 164 tests (9 integration tests need Hermes
      installed, 2 dashboard-route tests need FastAPI; both skip otherwise).
- [x] CI on Linux, macOS and Windows, Python 3.11–3.14: 12/12 at `3469f87`
      ([run](https://github.com/ddaikodaiko/hermes-time-machine/actions/runs/42665470085)).
      `78ce6f5` only changes one README line; GitHub created 6 and then 9 of its 12 jobs, all green.
- [x] Dashboard tab exercised live in `hermes dashboard` (Windows) against real session data.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [x] `python demo/run_demo.py` replays the full flow without Hermes.

## Typeform answers (as submitted, still accurate)

- **One-line pitch:** The rollback that knows what it can't undo.
- **Problem:** rewinding files is solved, but `/rollback`, `/undo`, `/retry` and compaction also
  rewind the conversation: the model forgets it pushed, the remote does not. The plugin asks
  at the point of no return, keeps a ledger of what a rewind cannot undo with the compensation
  command, and reminds the model after a rewind.
- **Relation to core `/rollback`:** complementary. `/rollback` restores files and the last turn;
  this adds agent-callable rewind, awareness of pushes, publishes and MCP actions,
  compensation and effect memory.
- **Out of scope:** RAM and process state, and executing compensation (it computes the
  command and asks; it never runs it).
