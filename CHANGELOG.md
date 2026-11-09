# Changelog

## 0.2.2 - 2026-11-09

### Added

- MCP and plugin tools go through the gate, the ledger and effect memory. Hermes 0.23 names MCP tools
  `mcp__<server>__<tool>`; GitHub tools (pull requests, issues, merges, releases, direct commits,
  branch deletes) get a target and, once the call returns, a compensation (`gh pr close 42 --repo o/r`,
  `gh release delete`, revert recipes with the merge or commit sha). Messaging tools (Slack, Discord,
  Telegram, email, ...) and any tool named for an irreversible verb (deploy, publish, delete, send,
  merge, refund, ...) are recorded with honest notes. Only the tool name decides; reads never trigger;
  dry runs are skipped; failure is read from the structured result.
- Settings `gate_tools` (`approve`/`warn`/`off`) and `gate_tool_allow_patterns` (regex on tool names).
- Package publishers: `cargo publish` (`cargo yank --version` from `Cargo.toml`), `twine upload`,
  `uv publish`, `poetry publish`, `hatch publish` (name and version from `pyproject.toml` or the dist
  file, with the PyPI yank page), `gem push` (`gem yank -v` from the gem file name).

### Changed

- Effect-memory reminders already shown are persisted (`reminded.json`), so a gateway restart does not
  repeat them.
- README: demo GIF and a coverage table.

## 0.2.1 - 2026-11-08

Found in a live Hermes 0.23 session and an adversarial review.

### Changed

- A restore requested by the model (`atm_rewind(confirm=true)`) always goes through Hermes'
  approval prompt, with the files it changes and the effects that stay true.
- `git commit && git push` in one call: the push is described as publishing the commits the
  command creates, and the exact compensation is filled in from the refs after the push.
- The gate matches commands only at the head of each sub-command; mentions (`echo`, `grep`,
  comments, heredocs) and `--dry-run` no longer ask for approval.

### Fixed

- Effect memory no longer reminds delegated subagents of their parent's pushes.
- Automatic checkpoints share one time budget (measure + capture).
- gc never runs when a manifest cannot be read; restore handles read-only files on Windows;
  invalid UTF-8 in the ledger is tolerated; `/timemachine` shows local times; path filters
  are case-insensitive on Windows; workspaces under `/private/var` work on macOS.

## 0.2.0 - 2026-11-07

Repositioned as "the rollback that knows what it can't undo", targeting Hermes Agent 0.23.0
(`requires_hermes: ">=0.23.0"`). Works alongside core `/rollback`.

### Added

- Model-driven restores need the human: `atm_rewind(confirm=true)` returns an `approve` directive (no `rule_key`) with the checkpoint, file counts and effects that stay true. The `/timemachine` command is the human path.

- Point-of-no-return gate: `pre_tool_call` returns an `approve` directive for irreversible
  effects (`git push`, `npm publish`, `terraform apply`, and similar) with the blast radius
  and the compensation command. Modes `approve`, `warn`, `off`; `gate_allow_patterns`;
  a per-kind-and-target `rule_key`.
- Effect ledger with pre-state, read from `.git` files and `package.json` without spawning
  processes. Compensation recipes for git push, npm publish, GitHub PRs and releases, kubectl,
  with guidance for Docker, Terraform, cloud CLIs, HTTP writes, SQL, mail and remote shells.
  Failed attempts are recorded as failed.
- Effect memory (`pre_llm_call`): after `/rollback`, `/undo`, `/retry` or compaction,
  irreversible effects missing from the visible history are re-injected once. Visibility is
  decided by `tool_call_id`, with an evidence fallback.
- `post_compaction` hook: records session-id lineage so effect memory survives compaction.
- `transform_tool_result` hook: adds a `time_machine` note with the checkpoint id, plus a hint
  to use `atm_rewind` after repeated failures (`struggle_threshold`).
- `/timemachine` slash command: `status`, `list`, `effects`, `diff`, `rewind`, `help`.
- Incremental, content-addressed checkpoint engine with file-level diff, per-path restore and
  fork. Restore verifies all objects first and moves replaced files to a trash directory.
- Resumable, time-bounded automatic capture (`auto_budget_seconds`), needed because Hermes
  0.23 fails closed when a `pre_tool_call` hook raises or times out.
- New settings: `irreversible_gate`, `gate_allow_patterns`, `auto_budget_seconds`,
  `max_file_mb`, `effect_memory`, `struggle_threshold`, `annotate_results`.
- Integration tests through the real Hermes 0.23 `PluginManager` and hook dispatch.
- `python demo/run_demo.py` replays the whole story without Hermes or a model.

### Changed

- The checkpoint engine was replaced. **v0.1 tar checkpoints are not restorable by 0.2**
  (there were no released users); they are ignored.
- `atm_rewind` preview lists `irreversible` effects ("still true outside") and refuses to
  restore over them without `allow_irreversible=true`.
- Docs rewritten around the new positioning.

## 0.1.0 - 2026-11-05

- First version: policy-driven workspace checkpoints (tar-based), automatic checkpoints before
  the first file-changing call of each turn and before risky commands, one-call rewind,
  fork, and an effect ledger; tools `atm_checkpoint`, `atm_rewind`, `atm_fork`, `atm_list`,
  `atm_effects`; hooks `pre_tool_call` and `post_tool_call`.
