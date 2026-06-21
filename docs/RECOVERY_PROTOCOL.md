# Ollarma Recovery Protocol

Deterministic backup/recovery engine for interrupted local agent/model work.
Shipped in v4.3 (2026-04-17).

## Purpose

Before ollarma admits fresh bounded execution (`route_prompt`, `/workflow`,
`/autopilot`, `run_agent`), it checks the repo for stranded work from prior
sessions — stalled worktrees, sidecar branches ahead of base, and scribe
artifacts left behind when a token limit or agent crash interrupted work.

If the scan finds anything suspicious, the admission layer **fails closed**:
the request is rejected with a structured blocker code and exact fix commands
instead of silently continuing on top of lost context.

This is **not** a portfolio control plane. It is a local recovery engine.
Watchtower/Overwatch/Antigence can ingest ollarma's recovery packets but
ollarma never rules on them.

---

## Recovery States

`ollarma.recovery.scan()` classifies the repo into exactly one of:

| State | Blocker Code | Admission | Meaning |
|-------|-------------|-----------|---------|
| `clean` | `OK` | ✓ admit | No sidecar worktrees with uncommitted work, no sidecar branches ahead of base, no scribe artifacts. Safe to proceed. |
| `resume_context_available` | `RESUME_AVAILABLE` | ✓ admit | Scribe wrote a `.ollarma/RESUME.md` or `session-log.jsonl` but nothing is stranded. Read the resume artifact before proceeding. |
| `stranded_worktree_detected` | `STRANDED_WORKTREE` | ✗ block | A sidecar worktree under `.claude/worktrees/` or `.codex/worktrees/` has modified or untracked files that were never reconciled. |
| `possible_work_loss` | `POSSIBLE_WORK_LOSS` | ✗ block | A sidecar branch (`claude/*`, `codex/*`, `sidecar/*`) has commits not merged to base and no scribe artifact exists — probable reasoning loss. |
| `recovery_sweep_required` | `RECOVERY_SWEEP_REQUIRED` | ✗ block | Stranded worktrees **and** ahead commits together — broad sweep needed before any new work. |

Severity ordering: sweep > stranded > possible_loss > resume > clean. The
highest-severity matching condition wins.

Admission-blocking states are: `stranded_worktree_detected`,
`possible_work_loss`, `recovery_sweep_required`. The first two scribe-related
states (`clean`, `resume_context_available`) always admit.

---

## Recovery Packet Schema (schema_version: 1)

Every scan produces a deterministic packet at
`.ollarma/incidents/<YYYYMMDDTHHMMSSZ>-<state-slug>.json` plus a
`.ollarma/incidents/latest.json` pointer (same bytes, not a symlink — so
ingest is one read, no stat-follow).

```json
{
  "schema_version": 1,
  "project_id": "ollarma",
  "repo_root": "<repo>",
  "timestamp": "2026-04-17T18:00:00+00:00",
  "source": "scan",  // "scan" | "admission:<entrypoint>" | "cli" | "mcp" | "http"
  "state": "stranded_worktree_detected",
  "blocker_code": "STRANDED_WORKTREE",
  "resume_present": false,
  "session_log_present": false,
  "worktrees": [
    {
      "path": "<repo>/.claude/worktrees/foo",
      "branch": "claude/foo",
      "head": "abc123...",
      "modified": ["service.py"],
      "untracked": ["scratch.py"],
      "is_sidecar": true
    }
  ],
  "ahead_commits": [],
  "modified_files": ["<worktree>:<file>", ...],
  "untracked_files": ["<worktree>:<file>", ...],
  "artifacts_at_risk": ["<worktree>:<file>", ...],
  "probable_reasoning_loss": false,
  "unknown_loss_risk": false,
  "next_fix_commands": [
    "# Inspect and reconcile: <repo>/.claude/worktrees/foo",
    "git -C <repo>/.claude/worktrees/foo status",
    "git -C <repo>/.claude/worktrees/foo diff"
  ],
  "notes": ["1 stranded sidecar worktree(s) with uncommitted changes"]
}
```

Determinism invariants (enforced by tests):

- Packet bytes are byte-identical for byte-identical state
  (`orjson.OPT_SORT_KEYS | OPT_INDENT_2`).
- `next_fix_commands[]` is deterministic per state.
- Filename uses UTC timestamp, never local time.
- `latest.json` is rewritten atomically (tempfile + `os.replace`) — crash-safe.

---

## Operator Surfaces

### CLI

```bash
# Run a scan; exit non-zero if state != clean.
ollarma recover scan [--project-root PATH] [--base main] [--json]

# Print the latest packet (no fresh scan).
ollarma recover report [--project-root PATH] [--json]
```

Both commands render a Rich summary table by default. `--json` emits the raw
packet for piping.

### MCP

Two tools registered on the ollarma MCP server:

| Tool | Annotation | Behavior |
|------|-----------|----------|
| `scan_recovery_state` | `WRITES_LOCAL_FILES` | Runs a fresh scan and writes the packet. Returns packet dict. |
| `read_recovery_report` | `READ_ONLY` | Returns `{"has_report": bool, "packet": dict}` without scanning. |

### HTTP

```
GET  /recovery/status         -- latest packet; 404 if no packet yet
POST /recovery/scan           -- fresh scan + packet; optional {"base_branch": ...}
```

Both reuse the same `service.recover_scan()` / `service.recover_latest()`
facade the CLI and MCP tools use.

---

## Admission Control

Four high-value execution entrypoints are gated on recovery state:

- `service.route_prompt` (also `POST /route` and MCP `route_prompt`)
- `service.submit_workflow` (also `POST /workflow` and MCP `submit_workflow`)
- `service.submit_autopilot` (also `POST /autopilot`)
- `service.run_agent` (also `POST /agents/{name}/run` and MCP `run_agent`)

On admission block, the entrypoint raises `RecoveryRequiredError` which
carries:

- `blocker_code` (one of the five above)
- `next_fix_commands[]` (exact shell invocations)
- `packet` (full `RecoveryState` as dict)
- `entrypoint` (which gate blocked)

Transports serialize this via `err.to_error_payload()` into:

```json
{
  "error": "RECOVERY_REQUIRED",
  "blocker_code": "STRANDED_WORKTREE",
  "entrypoint": "route_prompt",
  "next_fix_commands": ["git -C ... status", "git -C ... diff"],
  "state": "stranded_worktree_detected",
  "project_id": "ollarma",
  "timestamp": "..."
}
```

### Block Receipts

Every block appends a `RecoveryBlockReceipt` (schema_version: 1) to
`.ollarma/recovery_block_receipts.jsonl` so governance systems
(Overwatch, Antigence) can observe admission events over time. Fields:
`timestamp`, `entrypoint`, `blocker_code`, `project_id`, `repo_root`,
`state`, `next_fix_commands[]`.

### Performance

The admission check uses a **30-second module-level cache** keyed by repo
root. First request in a window runs the scanner; subsequent requests reuse
the cached `RecoveryState`. Bypass via `force=True` kwarg on
`check_recovery()` or env `OLLARMA_RECOVERY_FORCE_SCAN=1`.

### Opt-out (test/dry-run only)

Set `OLLARMA_RECOVERY_ADMISSION=off` to disable admission entirely. The
test suite's `conftest.py` does this by default so developer sidecar
worktrees don't trip every test case.

---

## Scribe Integration

Backup no longer relies on the agent remembering to call
`scribe_progress()`. Four service entrypoints now emit automatic entries
via `ollarma.scribe_hooks`:

- **pre-dispatch** (`state: started`) — written before admission + work
- **heartbeat** (`state: in_progress`) — daemon thread at ≥30s cadence
  (enabled for long-running paths like `run_benchmark`; used via
  `hooked(..., with_heartbeat=True)`)
- **end-of-run** (`state: completed` on success, `state: blocked` on
  exception with `next_action`) — written in finally/except

The voluntary `scribe_progress` MCP tool remains unchanged (SCRIBE-04 —
agents may still call it explicitly; the new hooks are additive).

Opt-out: `OLLARMA_SCRIBE_HOOKS=off` silences automatic hooks.

---

## Watchtower / Overwatch / Antigence Ingest Contract

The packet is intentionally stable so sibling projects can ingest it
without cross-repo coupling:

- **Watchtower** may poll `.ollarma/incidents/latest.json` across the
  portfolio to build a fleet-wide recovery view. ollarma commits to
  stable filenames and JSON shape at `schema_version: 1`.
- **Overwatch** can consume the `governance_refs` field (empty in v4.3;
  reserved for future expansion) and the block-receipt stream at
  `.ollarma/recovery_block_receipts.jsonl`.
- **Antigence** can classify packet text against its redaction antibodies
  before ingest; the packet shape contains no ollarma-authored
  free-form prose except the `notes[]` field (which is deterministic and
  short).

Schema evolution: any breaking change to the packet shape bumps
`schema_version`. Consumers should reject unexpected versions rather than
paper over them.

---

## Boundaries

- ollarma is a recovery **engine**, not a recovery **authority**. It
  reports state and suggests commands. The operator (or Watchtower)
  executes the fix.
- ollarma **does not** auto-merge, auto-rebase, or auto-delete sidecar
  branches. Those are destructive, state-dependent, and require human
  review.
- ollarma **does not** scan outside its own repo root. Cross-repo sweeps
  are Watchtower's job.
- ollarma's recovery data stays local-first (`.ollarma/incidents/`,
  `.ollarma/recovery_block_receipts.jsonl`). No network emission.

---

## Residual Risk (known, tracked)

- **Non-git repos** — scanner assumes git worktree semantics; a non-git
  project returns `clean` misleadingly. Out of scope for v4.3.
- **Continuous watching** — v4.3 uses polling (scan on request +
  admission-time). Real-time filesystem watch is out of scope.
- **Recovery of remote/cloud agent state** — ollarma is local-first;
  cloud context belongs to the caller.
- **GUI recovery view** — CLI/MCP/HTTP is the v4.3 contract. Dashboard
  integration deferred.

See `.planning/REQUIREMENTS.md` under "Out of Scope (v4.3)" for the full
list.
