# Plan — Hermes Agent Plugin Hackathon

**Deadline:** Wednesday 11 November 2026, 17:00 EST.
**Judged on:** usefulness, reliability, polish, creativity. Judges are the Nous Research Hermes team.
**Rule:** the submission is the plugin + catalog PR + demo + Typeform.

## Critical path

```
plugin loads on 0.23 -> tests green -> docs -> catalog PR (new SHA) -> demo recorded -> X post + Typeform
```

The catalog PR is human-reviewed. If it is still pending at the deadline, submit the PR link anyway and say so.

## Status

| Day | Date | Work | State |
|---|---|---|---|
| 1 | Thu 5 Nov | Scaffold, policy, effect ledger, tar checkpoints, smoke tests, demo script (v0.1.0) | done |
| 2 | Fri 6 Nov | Plugin loaded in Hermes 0.19 through the real `PluginManager`; mutation checkpoints; safety fixes; public repo | done |
| 3 | Sat 7 Nov | v0.2 pivot (below) | done |
| 4 | Sun 8 Nov | v0.2.1 from a live session and an adversarial review; demo recorded; catalog PR reopened | done |
| 5 | Mon 9 Nov | #149553 merged (v0.2.1); X demo; Typeform submitted; v0.2.2 (MCP tools, publishers); v0.3.0 (dashboard tab, `atm_diff`, undoable restores); YouTube demo; SHA-bump PR #152080 | done |
| 6 | Tue 10 Nov | Wait for #152080; answer review if any; visibility (X, Discord) | in progress |
| 7 | Wed 11 Nov | Confirm the pin before 17:00 EST; no new features | pending |

### Day 3 — what was actually done

Reading Hermes 0.23 changed the plan. Two findings:

1. Core `/rollback` (opt-in, `checkpoints.enabled`) already restores files and the last chat
   turn via a shadow git store. A better file snapshotter is not the point. What it lacks is
   knowledge of pushes and publishes, and the fact that after `/rollback` the model forgets
   the turn in which it pushed.
2. `pre_tool_call` fails **closed** in 0.23: a hook that raises or times out blocks the tool.
   The v0.1 tar capture could stall a tool call on a big workspace.

Done in response (v0.2.0):

- [x] New positioning: "the rollback that knows what it can't undo".
- [x] Checkpoint engine rewritten: incremental, content-addressed, deadline-bounded, resumable.
- [x] Point-of-no-return gate through Hermes' approval directive (`approve` / `warn` / `off`).
- [x] Effect ledger with pre-state and compensation commands read from files, no subprocess.
- [x] Effect memory (`pre_llm_call`) and compaction lineage (`post_compaction`).
- [x] `transform_tool_result` annotation, `/timemachine` command.
- [x] Retargeted to Hermes 0.23.0 (`requires_hermes: ">=0.23.0"`); `hermes plugins validate`
      and `hermes plugins doctor --ci` pass.
- [x] 122 tests (unit, flow and 9 integration tests through the real 0.23 `PluginManager`).
- [x] Benchmark on a Windows laptop with real-time antivirus (worst case; no Linux or macOS numbers).
- [x] Docs rewritten: README, DESIGN, DEMO, SUBMISSION, CHANGELOG; `demo/run_demo.py` rebuilt.

### Days 4–5 — after the v0.2 pivot

- [x] v0.2.1: model-driven restores always ask the human; `commit && push` chains; fewer false positives.
- [x] Catalog entry merged ([#149553](https://github.com/NousResearch/hermes-agent/pull/149553), pin `1ac9c78`).
- [x] Demo on X ([tweet](https://x.com/daik0zz/status/2119454575119737147)) and YouTube
      ([video](https://youtu.be/i84_S98vUs8)); Typeform submitted.
- [x] v0.2.2: MCP and plugin tools through the gate, ledger and memory; cargo/PyPI/gem publishers;
      persistent reminders; README GIF, screenshots, coverage table; catalog banner.
- [x] v0.3.0: read-only Time machine dashboard tab; `atm_diff`; a safety checkpoint before every restore.
- [x] 164 tests, CI 12/12 on 3 OS; `validate` and `doctor` pass (6 tools, 5 hooks).

## Remaining

- [ ] [#152080](https://github.com/NousResearch/hermes-agent/pull/152080) (bump to 0.3.0, pin `78ce6f5`) merged.
      Do not push to its branch again unless review asks for it.
- [ ] Optional: run `bench/bench_snapshot.py` on Linux or macOS and add the numbers.

## Kill switches

| If… | Then… |
|---|---|
| Catalog review is slow | Submit with the PR link; the demo and Typeform are the judged artifacts. |
| Demo recording against a real model fails | Record `python demo/run_demo.py --pause`; it needs no Hermes install. |
| A newer Hermes changes a hook | Keep the tools working (`atm_checkpoint` / `atm_rewind`) and disclose the degradation. |
| Time runs out | Do not add features. Reliability and a clean demo beat scope. |
| #152080 not merged by the deadline | The merged v0.2.1 entry stays valid; the repo, README and videos show v0.3.0. |

## What not to do

- Do not bundle binaries or `.pyc` (catalog rule 17).
- Do not patch Hermes core or wrap `AIAgent` methods (rule 9).
- Do not add a self-updater (rule 3).
- Do not read `.env`, `auth.json` or vendor tokens (rule 11).
- Do not auto-approve or bypass approvals (rule 12).
