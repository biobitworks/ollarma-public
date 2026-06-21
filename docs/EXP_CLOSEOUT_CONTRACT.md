# EXP Closeout Contract — DRAFT / NON-AUTHORITATIVE / PI REVIEW REQUIRED

> **STATUS: DRAFT. This document is NOT authoritative and does NOT satisfy the
> `gsigmad-audit-output` Closeout Contract Gate until a PI reviews and adopts it.**
>
> **Provenance:** drafted to resolve the missing contract cited by the
> `gsigmad-audit-output` skill during PROMPT-OLLARMA-SWARM-001 T10 (Phase 70 T9b
> acceptance audit, 2026-05-31). The audit skill names
> `docs/EXP_CLOSEOUT_CONTRACT.md` as its source of truth, but no such file
> existed in this repo; the audit hard-halted. This draft transcribes ONLY the
> four required sections the audit skill enumerates, plus the halt/warning rules
> the skill itself states. It deliberately invents NO broader governance. A PI
> must confirm scope, field semantics, and version before this gate is treated
> as met.

## Version

- **draft-0** (proposed 2026-05-31). No prior `260508-ecc` v1.0 artifact was
  found in this repo; if an authoritative version exists elsewhere in the
  portfolio it supersedes this draft entirely.

## Purpose

Before any output / reproducibility audit or KG writeback, an EXP note must
record the four sections below. A missing required field is a hard halt
(`EXP_CLOSEOUT_CONTRACT_VIOLATION`).

## Required sections

### 1. Artifact linkage

- `experiment_id`
- `experiment_note_path`
- `script_path` — or a documented absence reason
- `result_artifact_paths`
- `lab_notebook_anchor`
- *(optional)* `result_manifest_hash`
- *(optional)* `daily_journal_anchor`

### 2. PROMPT provenance

Exactly one of:

- `prompt_id` + `prompt_path` *(optional `prompt_exp_map_entry`)*, **or**
- `no_prompt_reason` + a non-empty `no_prompt_justification`.

CONFIRMATORY / REPLICATION / material AI-assisted experiments **MUST** record a
`prompt_id`; the `no_prompt_reason` enum is not allowed for these.

### 3. Notebook replay contract

Exactly one of:
`canonical_replay_notebook | inspection_aid_notebook | operator_wrapper_notebook |
no_notebook_required | human_gated_no_execution | ip_sensitive_no_execution |
backfill_required`.

For each listed notebook, record the per-notebook fields:
`independently_runnable`, `calls_scripts_via_subprocess`,
`imports_experiment_scripts`, `loads_frozen_run_artifacts`,
`safe_for_ollarma_jupyter_replay`, `replay_owner`.

### 4. Classification + reason

One of:
`experiment_repo | operator_console_exempt | product_site_exempt |
meta_repo_exempt | human_gated | ip_sensitive_no_execution`,
plus a `classification_reason` string.

## Hard halts (block claim promotion / KG writeback)

- **Missing required field** → `EXP_CLOSEOUT_CONTRACT_VIOLATION: missing fields
  [list]`; refuse to proceed to the Output Audit.
- **`safe_for_ollarma_jupyter_replay: true` with a non-canonical contract value**
  → `EXP_CLOSEOUT_CONTRACT_REPLAY_MISMATCH` (only `canonical_replay_notebook` is
  replay-eligible).
- **CONFIRMATORY / REPLICATION / material AI-assisted EXP without a `prompt_id`**
  → `EXP_CLOSEOUT_CONTRACT_PROMPT_MISSING`.
- **Claim promotion with notebook contract `operator_wrapper_notebook |
  backfill_required | needs_deep_audit | human_gated_no_execution |
  ip_sensitive_no_execution`** → `EXP_CLOSEOUT_CONTRACT_PROMOTION_BLOCKED`. Only
  a runnable/inspection-class notebook (or `no_notebook_required` for
  analysis-only EXPs) is promotion-eligible.

## Soft warnings (do NOT halt; report under "Issues found")

- `experiment_repo` AND `notebook_replay_contract: backfill_required` AND no
  `prompt_id` → warn `notebook_backfill + prompt_missing`.
- `human_gated` AND notebooks present without `safe_for_ollarma_jupyter_replay`
  set → warn `human_gated_replay_decision_pending` (human PI owns next action).

## Out of scope for this draft

This draft does not define: trust-tier policy, KG schema, claim-ceiling rules,
or any governance not directly enumerated by the audit skill's Closeout Contract
Gate. Those remain owned by their respective gsigmad surfaces.
