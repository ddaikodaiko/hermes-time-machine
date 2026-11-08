# Changelog

## 0.2.0 - 2026-11-07

Repositioned as "the rollback that knows what it can't undo", targeting Hermes Agent 0.23.0
(`requires_hermes: ">=0.23.0"`). Works alongside core `/rollback`.

### Added

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
