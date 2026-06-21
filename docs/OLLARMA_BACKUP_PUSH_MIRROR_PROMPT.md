# Ollarma Backup / Push / Mirror Prompt

Use this prompt when a sibling project wants `ollarma` to help with the external-drive backup workflow that Overwatch already governs.

This is not a service-mode `/chat` or `/route` workflow. It belongs on the interactive project-chat lane because it may inspect git state, commit, push to backup remotes, and run the mirror sync.

## Use This Surface

Start from:

```bash
ollarma chat --project Overwatch --model qwen3:1.7b
```

Do not use `POST /chat`, `POST /route`, or MCP `route_prompt` for this workflow. Those surfaces are intentionally narrower and should remain read-only by default.

## Copy-Paste Prompt

```text
You are helping with the external-drive backup / push / mirror workflow on this machine.

Use the Overwatch-governed runbook and scripts, not an improvised sequence.

Primary references:
- <repo>/docs/governance/BACKUP_PUSH_MIRROR_RUNBOOK_2026-03-07.md
- <repo>/docs/governance/KB_HOWTO_BACKUP_PUSH_MIRROR_2026-03-07.md

Primary script entry point:
- <local-path>

Rules:
- Confirm `/Volumes/magicDATAbox` is mounted before doing anything destructive.
- Inspect the runbook and script flags before executing.
- Use the one-command runner unless the operator explicitly asks for step-by-step mode.
- Treat external-drive backup push as the default target.
- Do not push to origin/GitHub unless the operator explicitly approves `--allow-origin-push`.
- Report the exact command you are going to run before you run it.
- Capture exact output, exact failures, and final validation signals.
- If the drive is not mounted or the script path is missing, stop and report the blocker.

Preferred flow:
1. Check the drive mount.
2. Review the runbook quickly to confirm the current command path.
3. Show the exact command.
4. Run the backup/push/mirror command.
5. Summarize the result, including whether backup push, agent-skill backup, and final mirror sync completed.

If the operator wants mirror-only:
- Use `python3 <local-path>`

If the operator wants the full run:
- Use `cd <local-path> && ./run_backup_push_mirror_full.sh`

Optional flags only when requested:
- `--with-dry-run`
- `--allow-origin-push`
- `--skip=repo1,repo2`
- `--no-presync`
- `--no-final-mirror`
```

## Concrete Commands

Check the external drive first:

```bash
[ -d /Volumes/magicDATAbox ] && echo "mounted" || echo "not mounted"
```

Recommended full run:

```bash
cd <local-path>
./run_backup_push_mirror_full.sh
```

Mirror-only fallback:

```bash
python3 <local-path>
```

## What This Workflow Normally Covers

- local commit if needed
- push to external-drive backup targets
- live agent-skill backup
- final rsync mirror to the external drive
- validation snapshot

By default, this is backup-first, not origin-first. Origin push requires explicit opt-in.

## Recovery Artifacts — commit vs ignore vs mirror (v4.4)

The v4.3 recovery engine and v4.2 service layer write several categories of runtime state under `.ollarma/`. The entire `.ollarma/` directory is gitignored as of v4.4 (see `.gitignore`). That's intentional — but it has consequences for external-drive backups.

| Path | Purpose | Commit to git? | Push to origin? | External-drive mirror? |
|------|---------|---------------|-----------------|------------------------|
| `.ollarma/incidents/*.json` | Recovery packets (one per scan) | ❌ no (gitignored) | ❌ no | ✅ yes — evidence for Watchtower/Overwatch/Antigence ingest |
| `.ollarma/incidents/latest.json` | Pointer to newest packet | ❌ no | ❌ no | ✅ yes |
| `.ollarma/recovery_block_receipts.jsonl` | Append-only admission-block audit | ❌ no | ❌ no | ✅ yes — governance stream |
| `.ollarma/RESUME.md` | Auto-regenerated scribe summary | ❌ no | ❌ no | ✅ yes (optional — regenerable) |
| `.ollarma/session-log.jsonl` | Append-only scribe entries | ❌ no | ❌ no | ✅ yes — session provenance |
| `.ollarma/bridge/events.jsonl` | Append-only bridge event spine for chat/route/workflow/review replay | ❌ no | ❌ no | ✅ yes — redacted runtime event replay for Ollarma/Antigence review |
| `.ollarma/governance/<digest>/payload.json` | Content-addressed governance payloads | ❌ no | ❌ no | ✅ yes — governance chain |
| `.ollarma/agent_receipts.json` | Per-agent-run RunReceipts | ❌ no | ❌ no | ✅ yes — run provenance |
| `.ollarma/manifests/*.json` | Workflow manifest materialization | ❌ no | ❌ no | ⚠ depends on manifest content — mirror unless it contains secrets |
| `.ollarma/kb/*` | Deterministic KB artifacts (SQLite + JSON) | ❌ no | ❌ no | ✅ yes if project's KB is authoritative on this machine |
| `.ollarma/benchmark_active.flag` | PIPE-10 advisory flag | ❌ no | ❌ no | ❌ transient — stale flag on a different machine is misleading |
| `.ollarma/startup/readiness.json` | StartupReadinessPayload snapshot (Phase 51/v4.5) | ❌ no (gitignored) | ❌ no | ⚠ optional — regenerated on next service start; useful for forensics if service failed to reach ready |
| `.ollarma/gateway/admissions.jsonl` | Append-only gateway admission audit (accept/reject/disabled/dry_run, hash-chained via `canonical_hash`) — Phase 57/v5.0 | ❌ no (gitignored) | ❌ no | ✅ yes — governance stream; Watchtower/Overwatch ingest; every `/gateway/submit` call leaves one entry here even if rejected |
| `.ollarma/gateway/receipts.jsonl` | Append-only `FrontierReceipt` stream (hash-chained, 1:1 with admissions where admission was accept/disabled/dry_run or produced a failed FrontierReceipt on reject) — Phase 57/v5.0 | ❌ no (gitignored) | ❌ no | ✅ yes — cost + provider audit; never contains raw API keys (I-06 enforced by admission boundary) |
| `.ollarma/gateway/rate_state.json` | Per-project rate-cap counters snapshot (Phase 58-02, pending) | ❌ no (gitignored) | ❌ no | ❌ transient — stale on a different machine is misleading (same reasoning as `benchmark_active.flag`) |

**Reference copies** of a sample packet + sample block receipt live at `docs/examples/recovery/` (redacted, committed). Those ARE in git for protocol-reference purposes and will NOT collide with live runtime state. A redacted sample FrontierReceipt + GatewayAdmissionEntry pair will land under `docs/examples/gateway/` alongside Phase 59 (first real provider); until then, the schema reference is `src/ollarma/gateway.py`.

### Mirror guidance

The external-drive mirror workflow already treats `.ollarma/` as operator state — no change needed to the runbook for v4.4 IF your mirror script already excludes transient files (`benchmark_active.flag`). If you're building a new mirror target, include `.ollarma/{incidents,recovery_block_receipts.jsonl,RESUME.md,session-log.jsonl,bridge/events.jsonl,governance,agent_receipts.json,kb,manifests,gateway/admissions.jsonl,gateway/receipts.jsonl}/*` and exclude `.ollarma/benchmark_active.flag` and `.ollarma/gateway/rate_state.json` (v5.0 transient state).

### What NOT to do

- Do NOT `git add -f .ollarma/...` to force-commit runtime state. That bypasses the v4.4 gitignore hardening and bloats the repo with per-scan churn.
- Do NOT mirror `.ollarma/` to a shared/multi-machine target. Block receipts and incident packets are machine-local evidence. Sharing them cross-machine breaks the governance invariant that one receipt = one admission event on one host.
- Do NOT push recovery state to origin. Nothing under `.ollarma/` is meaningful to a remote reviewer; it's local forensic evidence.
