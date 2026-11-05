# 90-second demo script

Record at 1080p, terminal font large enough to read on a phone. No cuts that hide the result.

## Before recording

```bash
cd hermes-time-machine
python demo/run_demo.py   # dry run once, then clear the terminal
```

Have the X post text ready. Tag `@NousResearch`.

## Shot list

| Time | Screen | Narration (English) |
|---|---|---|
| 0:00–0:08 | Title card: `hermes-time-machine` + "the undo Hermes doesn't have" | "Coding agents can break your workspace in one command. Git can't save what was never committed." |
| 0:08–0:18 | `demo/run_demo.py` baseline: `app.py`, `schema.sql` | "Here is a healthy workspace. Two files, no commits." |
| 0:18–0:30 | Step 2: `auto:write_file`, `trigger.type: mutation` | "The agent starts editing. The first file-changing call of the turn takes a checkpoint automatically — milliseconds, outside the repo." |
| 0:30–0:42 | Agent writes bad code, drops the table, adds `debug.log` | "Now it migrates the schema, breaks the handler and leaves a mess." |
| 0:42–0:55 | `git push origin main --force` → `atm_effects` shows `git_push`, `reversible: false` | "It also pushed. That is the part no undo button can fix. The effect ledger records it and marks it irreversible." |
| 0:55–1:05 | `atm_rewind({"confirm": false})` shows preview + warning | "Rewind previews the restore and refuses to pretend the push never happened." |
| 1:05–1:18 | `atm_rewind({"confirm": true, "allow_irreversible": true})` → files restored, `debug.log` gone | "Acknowledge it, restore, and the workspace is back. The previous state is kept in a trash folder, not deleted." |
| 1:18–1:28 | `atm_fork({"name": "alternative"})` → second directory | "Or fork the checkpoint and try a different approach without touching the original." |
| 1:28–1:30 | Closing card: repo URL + `@NousResearch` | "Policy-driven checkpoints, honest about what can't be undone." |

## Commands (copy-paste)

```bash
python demo/run_demo.py
```

The script prints every JSON response, so the audience sees the actual contract, not a mock.

## X post draft

> Agents can `rm -rf` your repo, drop a table and `git push` before you notice.
>
> We built hermes-time-machine for @NousResearch: a checkpoint before the first edit of every turn, another before risky commands, one-command rewind, and an effect ledger that tells you what can no longer be undone.
>
> Demo (90s) ↓
> [link]
>
> #HermesAgent #AIAgents

## Failure recovery while recording

If the demo fails mid-take, do not restart from zero: run `python demo/run_demo.py` again. It uses a fresh temp workspace every time, so the take is repeatable.
