# Operator Recovery Playbook

**Shipped in:** v4.4 (2026-04-18)
**For the full contract:** see `docs/RECOVERY_PROTOCOL.md`
**For install/surface commands:** see `docs/LOCAL_ADOPTION.md` Recovery section

This is a copy-pasteable walkthrough for: *"I hit a stranded state. Now what?"*

---

## Mental model (30 seconds)

Before ollarma admits fresh bounded work (`route_prompt`, `/workflow`, `/autopilot`, `run_agent`), it scans the repo for:

1. **Stranded sidecar worktrees** (`.claude/worktrees/*`, `.codex/worktrees/*`) with uncommitted files
2. **Sidecar branches** (`claude/*`, `codex/*`, `sidecar/*`) with commits not merged to `main`
3. **Scribe artifacts** (`.ollarma/RESUME.md`, `.ollarma/session-log.jsonl`) from prior sessions

If anything non-clean is found, ollarma **fails closed** with a `RECOVERY_REQUIRED` error carrying exact fix commands. Your job as operator is to resolve the hazard, then re-scan.

---

## Step 1 — Scan

```bash
ollarma recover scan
```

Exit code 0 = clean. Exit code 1 = something to look at. You'll see a table like:

```
┌───────────────────────┬──────────────────────────┐
│ Field                 │ Value                    │
├───────────────────────┼──────────────────────────┤
│ State                 │ recovery_sweep_required  │
│ Blocker               │ RECOVERY_SWEEP_REQUIRED  │
│ Resume artifact       │ no                       │
│ Session log           │ no                       │
│ Worktrees seen        │ 2                        │
│ Sidecar branches ahead│ 1                        │
│ Artifacts at risk     │ 1                        │
└───────────────────────┴──────────────────────────┘

Next fix commands:
  $ # Stranded worktree: /path/to/.claude/worktrees/aurora
  $ git -C /path/to/.claude/worktrees/aurora status
  $ git -C /path/to/.claude/worktrees/aurora diff
  $ git log --oneline main..claude/aurora

Notes:
  - 1 stranded worktree(s) AND 1 ahead-commit(s) -- broad sweep required
```

See `docs/examples/recovery/sample-packet.recovery-sweep-required.json` for the full JSON shape this scan persisted to `.ollarma/incidents/<timestamp>-<state>.json`.

## Step 2 — Decide rescue vs retire (for each hazard)

For each stranded worktree or ahead branch the scan surfaced, answer **two** questions:

### Q1. Is there unique unmerged content?

```bash
# For a stranded worktree at PATH:
git -C PATH status                 # what's dirty?
git -C PATH diff                   # what did you change?

# For a sidecar branch:
git log --oneline main..BRANCH     # commits not on main?
git diff main..BRANCH --stat       # files touched?
```

### Q2. Is that content worth keeping?

- **YES, rescue**: cherry-pick, merge, or cp the unique content into `main`, then retire the sidecar.
- **NO, retire**: the content is superseded/obsolete — record the decision and retire cleanly.

Record every rescue/retire decision in a durable note under `.planning/milestones/` (same pattern as `v4.4-SIDECAR-RECONCILIATION.md`). Future audits need to trace every removal.

## Step 3 — Retire cleanly

```bash
# Worktree first
git worktree remove --force .claude/worktrees/<name>

# Then the branch (ollarma's recovery scanner detects branches, not just worktrees)
git branch -D claude/<name>
```

Do NOT manually `rm -rf` the worktree directory. That leaves a stale worktree entry in `.git/worktrees/` and the recovery scanner will get confused.

## Step 4 — Re-scan

```bash
ollarma recover scan
```

Expected:

- `clean / OK` — all clear, admit resumes
- `resume_context_available / RESUME_AVAILABLE` — also admits; just tells you a prior session left a scribe artifact (read `cat .ollarma/RESUME.md` if you want that context)

If still non-clean, loop to Step 2 with the newly-surfaced hazards.

## Step 5 — Confirm admission resumes

Either inspect `/health` or run the actual workload:

```bash
# HTTP
curl -s http://127.0.0.1:8484/recovery/status
curl -X POST http://127.0.0.1:8484/route ... # should now succeed

# Or check the block-receipt stream — during the block period, each admission
# attempt appended a line. No new lines after re-scan means no new blocks.
tail .ollarma/recovery_block_receipts.jsonl
```

See `docs/examples/recovery/sample-block-receipt.jsonl` for what a block receipt looks like (one JSONL record per blocked admission).

---

## Quick-reference: the 5 recovery states

| State | Admits? | When you see it |
|-------|---------|-----------------|
| `clean` | ✓ | No sidecars with work, no ahead branches, no scribe artifacts |
| `resume_context_available` | ✓ | Scribe artifact exists but nothing stranded |
| `stranded_worktree_detected` | ✗ | Sidecar worktree has uncommitted files |
| `possible_work_loss` | ✗ | Sidecar branch ahead of main, no scribe artifact |
| `recovery_sweep_required` | ✗ | Both stranded AND ahead — broadest sweep |

Severity order: sweep > stranded > possible_loss > resume > clean. Highest-severity match wins.

---

## What NOT to do

- **Don't `rm -rf .ollarma/incidents/`.** Those packets are evidence. They're gitignored so they won't bloat git history. Keep them.
- **Don't set `OLLARMA_RECOVERY_ADMISSION=off` in production.** The opt-out exists for test/dry-run paths only. Shipping with admission off re-introduces the silent-continuation bug v4.3 was built to fix.
- **Don't force-push after retiring a sidecar branch** unless you've first confirmed the branch contains no unique unmerged work. Use `git rev-list --count main..BRANCH` to check.
- **Don't delete `.ollarma/recovery_block_receipts.jsonl` to "clean up".** Governance systems (Watchtower/Overwatch/Antigence) ingest that stream. Archive it if it grows; don't wipe it.

---

## When you're stuck

1. `cat .ollarma/incidents/latest.json` — the most recent packet tells you exactly what was detected and what to run.
2. `ollarma recover report --json` — same as above, prettier.
3. Read `docs/RECOVERY_PROTOCOL.md` for the full contract + ingest shape.
4. Read `.planning/milestones/v4.4-SIDECAR-RECONCILIATION.md` for a worked example of retiring 3 sidecars.

---

*Shipped with v4.4 Recovery Hardening (phase 48) on 2026-04-18.*
