# 90-second demo

One real Hermes session, no voice-over. On-screen captions carry the story; music only.
Everything is safe to run: `origin` is a **local bare repository**, so the push never
leaves the machine.

Story in one line: *every rollback restores files; none of them can un-push.*

## Setup (once)

```bash
# 1. A demo project whose "origin" is a folder on this machine
mkdir -p ~/atm-demo && cd ~/atm-demo
git init --bare remote.git
git init -b main app && cd app
git config user.email demo@example.com && git config user.name demo
printf 'def total(items):\n    return sum(i["price"] for i in items)\n' > shop.py
git add . && git commit -m "initial" -q
git remote add origin ../remote.git
git push -u origin main -q          # so origin/main exists before the demo
```

```bash
# 2. Install and enable the plugin (no tool-override permission)
hermes plugins install ddaikodaiko/hermes-time-machine
hermes plugins enable hermes-time-machine --no-allow-tool-override
```

```yaml
# 3. ~/.hermes/config.yaml  (key names for core checkpoints and plugin settings;
#    confirm against your Hermes build)
checkpoints:
  enabled: true            # core /rollback
plugins:
  entries:
    hermes-time-machine:
      settings:
        workspace: ""      # empty = the folder Hermes runs commands in
        irreversible_gate: approve
        effect_memory: true
```

```bash
# 4. Run Hermes from the project, Anthropic Sonnet as the model
cd ~/atm-demo/app
hermes --provider anthropic        # pick a Sonnet model; check `hermes --help` for the exact flag
```

Dry run first. Reset between takes:

```bash
cd ~/atm-demo/app && git reset --hard origin/main -q && git push -f origin main -q
```

No Hermes handy? `python demo/run_demo.py --pause` replays the same story from the
plugin's own hooks (fake git repo and bare remote written as files). Use it as a
fallback source for the recording; the captions below fit its steps 2 to 9.

## Prompts

1. `Refactor shop.py: add tax handling and a discount parameter, commit it, and push to origin main.`
2. `/rollback` (core command)
3. `Did you push anything?`
4. `/timemachine effects`
5. `Rewind to your first checkpoint.` (the agent calls `atm_rewind`)

## Shot list (target 88 s)

| t (s) | Screen | Caption |
|---|---|---|
| 0-6 | Title card: `hermes-time-machine`. | Every rollback restores files. None of them can un-push. |
| 6-20 | Prompt 1. The first edit's tool result shows `time_machine: {"checkpoint": "ckpt_...", ...}`. | First edit of the turn: a restore point, in milliseconds. |
| 20-35 | Hermes approval prompt: "Point of no return (hermes-time-machine) - git push -> origin/main ... If needed later: git push --force-with-lease=...". Approve. | Before anything permanent, Hermes asks. With the blast radius and the undo command. |
| 35-50 | Prompt 2: `/rollback`. Files and the last turn disappear. | Now roll back. Files and the last chat turn are gone. |
| 50-68 | Prompt 3. The model answers yes, with time and sha range, and offers the revert command. | The model remembers because the plugin told it. The remote never forgot. |
| 68-82 | Prompt 4 then 5: `/timemachine effects`, then the `atm_rewind` preview with `irreversible` / "still true outside". | Every rewind tells you what it can't undo. |
| 82-90 | Closing card: repo URL, `@NousResearch`. | hermes-time-machine. The rollback that knows what it can't undo. |

Optional A/B cut (strong): repeat prompts 1 to 3 with the plugin disabled
(`hermes plugins disable hermes-time-machine`). Expected, not guaranteed: the model says
it did not push anything. Only use the shot if it actually happens on your take.

## Recording notes

- 1080p, large terminal font readable on a phone. No cuts inside a shot that hide a result.
- Captions are text overlays, not narration. Keep each under 12 words.
- After the take, verify the claim on screen: `git -C ~/atm-demo/remote.git log --oneline -1`
  shows the pushed commit, even after `/rollback`.
- The 50-68 and 68-82 shots depend on what the model says; retake until the screen shows
  what the plugin actually injected and the real ledger output.
- X post draft:

> Every rollback restores files. None of them can un-push.
>
> hermes-time-machine for @NousResearch Hermes: approval at the point of no return, a ledger of what a rewind can't undo (with the exact compensation command), and effect memory so the model doesn't forget a push after /rollback.
>
> 90 s demo below.
