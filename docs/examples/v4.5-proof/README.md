# v4.5 Live Proof Artifacts

Durable reference copies of the Phase 54 live-proof runs, captured 2026-04-19.
All machine-specific absolute paths have been redacted to `<REPO_ROOT>/...`;
timestamps are frozen to 2026-04-19; no bearer tokens or secrets are present.

## Host state during capture

- `admission_posture`: ready
- `pipeline_posture`: ready
- `swap_posture`: degraded (2361 MB swap, threshold 512 MB)
- `model_availability`: degraded (SELECTION_STALE → fresh artifact generated)
- Effective model: `qwen3:1.7b` (local GPU, MLX backend)

This is the "under load" condition Phase 54 was designed to prove. The routing
ladder (Phase 53) correctly chose the local lane and surfaced `SWAP_DEGRADED`
for all assets — no frontier escalation occurred.

## Files

| File | Captures |
|------|----------|
| `autopilot-report.json` | PROOF-01: AutopilotReport from `ollarma autopilot Conductor --run`. 8 assets (Conductor project), all routed to `qwen3:1.7b` via `ollarma-default` lane. All rejected at scheduler due to SWAP_DEGRADED. No frontier escalation. Receipts + checkpoint written to `conductor/.ollarma/autopilot/`. |
| `checkpoint-resume-evidence.json` | PROOF-02: Workflow checkpoint/resume cycle. First `ollarma workflow` submission writes checkpoint + receipt to `conductor/.ollarma/runs/phase54-proof/`. Process exits (SWAP_DEGRADED). `resume_workflow_run` resolves `next_stage=preflight`, `resume_from_step=preflight-check` from the saved checkpoint — no data loss. |

## Route receipt check (PROOF-01 + PROOF-02)

Per Phase 54 contract: no receipt may show `lane=frontier_or_human` with a
non-blocked status.

- PROOF-01 receipts: `lane=ollarma-default`, `reason_code=SWAP_DEGRADED` × 8
- PROOF-02 receipt: `lane=workflow_execution_queue`, `reason_code=SWAP_DEGRADED` × 1
- **Frontier escalation: NONE** — proof passes.

## These are reference examples, not runtime data

The live runtime equivalents are gitignored:

- `conductor/.ollarma/autopilot/*/` — autopilot run receipts + checkpoints
- `conductor/.ollarma/runs/*/` — workflow run receipts + checkpoints
- `conductor/.ollarma/manifests/` — workflow manifests (project-side)

See:
- `docs/OLLARMA_PERSISTENCE_PLAYBOOK.md` — operator playbook
- `.planning/phases/54-live-proof-under-load/` — phase plans and summaries
- `.planning/milestones/v4.5-CLOSEOUT.md` — milestone closeout record
