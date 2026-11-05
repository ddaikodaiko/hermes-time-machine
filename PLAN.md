# 7-day plan — Hermes Agent Plugin Hackathon

**Deadline:** Wednesday 11 November 2026, 17:00 EST.
**Prize target:** first place (the 10k).
**Rule:** the submission is the plugin + catalog PR + demo + Typeform, not the research report.

## Critical path

```
repo público → plugin carga → tests verdes → PR catálogo → demo grabado → Typeform + X
```

The catalog PR is human-reviewed. **Open it on Day 4, not Day 7.** If it is still pending on the deadline, submit the PR link anyway and say so.

## Status

| Day | Date | Work | State |
|---|---|---|---|
| 1 | Thu 5 Nov | Scaffold, policy, ledger, snapshots, 5 smoke tests, demo script | ✅ |
| 2 | Fri 6 Nov | Hermes 0.19.0 installed; plugin loads with 5 tools + 2 hooks; 6 integration tests; config compatibility; mutation checkpoints; safety fixes | ✅ |
| 2b | Fri 6 Nov | Public repo + pinned SHA | ⏳ |
| 3 | Sat 7 Nov | Benchmark, policy tuning, design notes, disclosure | ⏳ |
| 4 | Mon 9 Nov | Record <90 s demo, open catalog PR | ⏳ |
| 5 | Tue 10 Nov | Typeform + X post + Discord | ⏳ |
| 6 | Wed 11 Nov | Re-validate pinned SHA, answer review, confirm before 17:00 EST | ⏳ |

## Day 1 — Thursday 5 Nov

- [x] Scaffold: `plugin.yaml`, `__init__.py`, tools, policy, effect ledger, snapshots.
- [x] 5 smoke tests green.
- [x] Recordable demo script.

**Gate:** repo exists and the plugin loads. (Loading verified; repo pending.)

## Day 2 — Friday 6 Nov

- [x] Install Hermes Agent 0.19.0 in an isolated Python 3.13 environment.
- [x] Confirm the real `PluginManager` loads the plugin: 5 tools, 2 hooks, no error.
- [x] Fix `tools.py` name collision with Hermes' `tools` package (renamed `handlers.py`).
- [x] Read settings from `plugins.entries.<id>.settings` (0.19.x has no `ctx.get_config()`).
- [x] Use the real Hermes home (`%LOCALAPPDATA%\hermes` on Windows).
- [x] Add automatic mutation checkpoint (once per turn) and per-turn tests.
- [x] Fix the safety check that rejected workspaces inside the home directory.
- [x] 13 tests green, including 5 that use the real loader, registry and hooks.
- [x] Create the public GitHub repo and pin a 40-character SHA.

**Gate:** one real end-to-end rewind through Hermes' registry. ✅

## Day 3 — Saturday 7 Nov

- [ ] Benchmark a 50 MB workspace: snapshot, restore, overhead.
- [ ] Tune false positives in the mutation patterns.
- [ ] Review `docs/DESIGN.md` and the disclosure section.
- [ ] Add a `CHANGELOG.md` and screenshots/GIF for the catalog PR.

**Gate:** restore of a 50 MB workspace under 2 s; overhead acceptable.

## Day 4 — Monday 9 Nov

- [ ] Record the <90 s demo (script in `docs/DEMO.md`).
- [ ] Pin the final SHA and open the catalog PR using `docs/SUBMISSION.md`.
- [ ] Add screenshots/GIF to the PR description.

**Gate:** PR open before 23:59 local time.

## Day 5 — Tuesday 10 Nov

- [ ] Submit the Typeform with the PR link and the X post.
- [ ] Post the demo on X tagging `@NousResearch`.
- [ ] Post in the Nous Discord `#plugins-skills-and-skins`.

**Gate:** Typeform submitted, X post live.

## Day 6 — Wednesday 11 Nov (morning)

- [ ] Re-run the tests on the pinned SHA.
- [ ] Check the PR CI is green; answer review comments.
- [ ] Re-read the submission checklist in `README.md`.
- [ ] Submit/confirm before **17:00 EST**.

**Gate:** all four artifacts exist: repo, PR, demo, Typeform.

## Kill switches

| If… | Then… |
|---|---|
| Catalog review is slow | Submit with the PR link; the demo and Typeform are the judged artifacts. |
| Demo recording fails | Use `python demo/run_demo.py` as the recording source; it needs no Hermes install. |
| A newer Hermes removes a hook | Keep the tools working manually (`atm_checkpoint` / `atm_rewind`) and disclose the degradation. |
| Time runs out for RAM | Do not add CRIU. FS-first is the honest, catalog-safe scope. |

## What not to do

- Do not bundle binaries or `.pyc` (catalog rule 17).
- Do not patch Hermes core or wrap `AIAgent` methods (rule 9).
- Do not add a self-updater (rule 3).
- Do not read `.env`, `auth.json` or vendor tokens (rule 11).
- Do not auto-approve or bypass approvals (rule 12).
