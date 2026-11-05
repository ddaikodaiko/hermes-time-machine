# hermes-time-machine

Policy-driven workspace checkpoints for [Hermes Agent](https://hermes-agent.nousresearch.com), with an **effect ledger** for the actions a rewind cannot undo.

> Most agents can undo their own file edits. None of them can tell you that the `git push` they just ran is already out of reach.

## What it does

- Takes a checkpoint of the workspace **before the first file-changing call of each turn** (`write_file`, `patch`, `rm`, `mv`, `sed -i`, migrations, installs).
- Takes a checkpoint **before risky calls** even if they are not edits (`git push --force`, `rm -rf`, `DROP TABLE`, writes outside the workspace, your own regexes).
- Records **external effects** (`git push`, `npm publish`, HTTP writes, cloud changes) and blocks a rewind that would silently pretend they never happened.
- Restores the workspace in one call, keeping the previous contents in a bounded trash directory.
- Forks a checkpoint into an independent directory to explore an alternative without touching the original.
- Uses only the Python standard library. No native binaries, no shell, no network, no credentials.

## What it does **not** do

- **No process or RAM snapshots.** That needs CRIU/microVMs and is out of scope for a catalog-safe plugin.
- **No conversation rewind.** It restores the environment, not the chat history.
- **No external undo.** It cannot unsend an email, revert a payment or delete a published package. It only makes that visible.
- **No GPU or Windows process state.** Filesystem only.

Being explicit about this is part of the design: a rollback tool that overpromises is worse than none.

## Install

```bash
hermes plugins install ddaikodaiko/hermes-time-machine
hermes plugins enable hermes-time-machine --no-allow-tool-override
```

The second flag matters: this plugin never replaces built-in tools, so it should not receive override permission.

Manual install (development):

```bash
git clone https://github.com/ddaikodaiko/hermes-time-machine
cp -r hermes-time-machine <hermes-home>/plugins/
hermes plugins enable hermes-time-machine --no-allow-tool-override
hermes plugins list --user --json
```

`<hermes-home>` is `~/.hermes` on Linux/macOS and `%LOCALAPPDATA%\hermes` on Windows.

Verified against **Hermes Agent 0.19.0 (2026.7.20)**, the current PyPI release: the plugin loads with 5 tools and 2 hooks and the integration tests pass. Hermes 0.19.0 has no `plugins doctor` / `plugins validate` subcommands; if your build provides them, run them as well.

## Configuration

Settings live under `plugins.entries.hermes-time-machine.settings` in `config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-time-machine
  entries:
    hermes-time-machine:
      settings:
        workspace: "/absolute/path/to/project"   # empty = current working directory
        auto_checkpoint: true
        auto_checkpoint_mutations: true
        mutation_cooldown_seconds: 120
        risk_threshold: 0.6
        max_snapshot_mb: 200
        retention: 20
        exclude_git: false
        extra_risky_patterns: []
```

| Key | Default | Meaning |
|---|---|---|
| `workspace` | `""` | Protected directory. Empty means the process working directory. Set it explicitly if Hermes may start from your home directory. |
| `auto_checkpoint` | `true` | Master switch for all automatic checkpoints. |
| `auto_checkpoint_mutations` | `true` | One checkpoint before the first file-changing call of each turn. |
| `mutation_cooldown_seconds` | `120` | Minimum gap between mutation checkpoints when the host provides no turn id. |
| `risk_threshold` | `0.6` | Minimum risk score (0–1) for a risk-triggered checkpoint. |
| `max_snapshot_mb` | `200` | Automatic snapshots larger than this are skipped and journaled. Manual checkpoints are always allowed. |
| `retention` | `20` | Automatic checkpoints to keep. Manual checkpoints are never pruned. |
| `exclude_git` | `false` | Exclude `.git` for faster snapshots. A rewind will then not restore git metadata. |
| `extra_risky_patterns` | `[]` | Additional regexes that mark a terminal command as risky. |

`exclude_git: true` makes snapshots faster, but a rewind will not restore `.git` metadata.

## Tools

| Tool | What it does |
|---|---|
| `atm_checkpoint(label)` | Manual checkpoint. |
| `atm_rewind(checkpoint_id?, confirm?, allow_irreversible?)` | Preview by default; restore with `confirm=true`. |
| `atm_fork(checkpoint_id?, name?)` | Extract a checkpoint into a sibling directory. |
| `atm_list(limit?)` | List checkpoints, newest first, with trigger and size. |
| `atm_effects(since?)` | List recorded effects and whether a rewind is safe. |

Typical flow:

```
user: refactor the auth module
agent: write_file(...)                  -> automatic checkpoint (mutation, turn 1)
agent: patch(...)                       -> same checkpoint, no extra cost
agent: terminal("git push origin main") -> effect ledger records an irreversible git_push
agent: atm_effects({})                  -> safe_to_rewind: false
agent: atm_rewind({"confirm": false})   -> preview + warning
agent: atm_rewind({"confirm": true, "allow_irreversible": true})
```

## How the policy works

`pre_tool_call` scores each call from 0 to 1:

| Signal | Score |
|---|---|
| Terminal command matches a destructive or remote-write pattern | 0.90 |
| Write tool targets a path outside the workspace | 0.80 |
| Command has a known irreversible effect | 0.75 |
| File-changing call that matched nothing else | mutation checkpoint, once per turn |
| Anything else | 0.00 |

Above `risk_threshold` (default `0.6`), a checkpoint is taken **before** the tool runs. The hook never blocks a tool and never bypasses Hermes approvals. If anything fails, it fails open and the agent continues.

## Effect ledger

`post_tool_call` classifies completed commands and appends to `effects.jsonl`:

- `git push`, `gh pr create`, `npm publish`, `docker push`
- `kubectl apply/delete`, `terraform apply`, cloud create/delete/update
- `curl`/`wget` writes, SQL `DROP/TRUNCATE/DELETE/UPDATE/INSERT`
- `sendmail`/`mail`, remote shell writes
- writes outside the workspace
- local destructive commands (`rm -rf`, `git reset --hard`, `git clean -f`)

`atm_rewind` refuses to run while there are irreversible effects after the checkpoint, unless `allow_irreversible=true`. The effects are never "undone"; they are acknowledged.

## Storage

```
<hermes-home>/plugin-data/hermes-time-machine/
├── checkpoints/<id>/manifest.json
├── checkpoints/<id>/workspace.tar.gz
├── trash/<id>_<suffix>/          # previous contents of a rewind (last 10 kept)
├── journal.jsonl                 # checkpoints, rewinds, forks, skips
└── effects.jsonl                 # external effects
```

Set `ATM_DATA_DIR` to override the location (used by tests and the demo).

## Safety

A rewind moves the current workspace contents into `trash/` and extracts the checkpoint over the workspace. Before doing that, `snapshot.assert_safe` refuses:

- filesystem roots and drive roots,
- the home directory,
- any directory that contains the home directory,
- known system directories (`C:\Windows`, `C:\Program Files`, `/usr`, `/etc`, `/var`, ...),
- workspaces that contain, or are contained by, the plugin data directory.

If extraction fails, the previous contents are moved back.

## Demo

```bash
python demo/run_demo.py
python demo/run_demo.py --pause   # press Enter between steps, for recording
```

It creates a throwaway workspace, triggers an automatic checkpoint with a simulated `write_file`, breaks the workspace, records a `git push`, previews a rewind, restores, verifies and forks. Designed to be screen-recorded in under 90 seconds.

Tests:

```bash
python -m unittest discover -s tests -v
```

- `tests/test_smoke.py` — policy, snapshots, rewind, fork, hooks (no Hermes required).
- `tests/test_hermes_integration.py` — real `PluginManager`, real `tools.registry`, real hook dispatch (skipped when Hermes is not installed).

13 tests, all passing on Hermes 0.19.0 / Python 3.13.

## Disclosure (catalog rule 13)

- **Shell commands:** none. The plugin never spawns a process.
- **Network calls:** none.
- **Files read/written:** the configured workspace, plus the plugin data directory.
- **Background processes:** none.
- **Credentials:** none collected or stored.
- **Risky behaviour:** a rewind moves the current workspace contents to a trash directory and extracts a checkpoint over the workspace. See [Safety](#safety) for the refusal rules.

## Catalog submission checklist

- [x] Public repository owned by the submitter.
- [x] `plugin.yaml` and `__init__.py` at the pinned commit.
- [x] Plugin loads in Hermes 0.19.0 with the declared 5 tools and 2 hooks.
- [x] `provides_tools` / `provides_hooks` match what `register()` registers.
- [x] `requires_hermes: ">=0.19.0"`.
- [x] No binaries, `.pyc`, self-updater or core overrides.
- [x] `plugin-catalog/hermes-time-machine.yaml` PR with a 40-character SHA pin — [#149553](https://github.com/NousResearch/hermes-agent/pull/149553).
- [ ] Demo recorded (<90 s) and posted on X tagging `@NousResearch`.
- [ ] Typeform submitted.

## Roadmap

- **v0.2:** content-addressed chunks and incremental snapshots.
- **v0.3:** optional memory layer through Hermes' existing Modal/Daytona backends (hot snapshot/pause), never through native binaries.
- **v0.4:** policy packs per project (`strict.yaml`) and a team audit view.
- **v1.0:** conversation-aware rewind via Hermes session hooks.

## License

MIT.
