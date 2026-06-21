# Ollarma Next Experiments Roadmap

**Status:** planning artifact, not execution evidence.
**Date:** 2026-05-31.
**Purpose:** define the next discrete experiments needed to turn Ollarma from a
local model/runtime substrate into a real-time bridge for multi-agent sidecars
that can offload routine work from frontier models while preserving receipts,
governance, and project-specific safety review.

## Current Evidence Base

- Phase 69 is complete: a real proof run against the BioViz Atlas mirror was
  committed under `runs/swarm_proof/20260531T172553Z/`.
- Phase 70 T9b ran and produced a PARTIAL verdict. The real scenarios behaved
  well, but the inert-control strict-cleanliness gate failed at round-1 JSD
  `0.0412`; falsification did not fire.
- EXP-OLLARMA-SWARM-002 is now pre-registered via
  `prompts/PROMPT_002_INERT_CONTROL_CALIBRATION.md`.
- RTB planning exists for:
  - bridge event stream (`docs/ANTIGENCE_RTB_01_EVENT_STREAM_PLAN.md`);
  - project antibody lanes (`docs/ANTIGENCE_RTB_02_ANTIBODY_SWARM_PLAN.md`);
  - reviewed chat, KG navigation, distillation, and proof bundles.
- The strategic goal is not only Phase 70 cleanup. It is a local-first agent
  substrate where Codex, Claude Code, and ChatGPT can delegate bounded sidecar
  work to Ollarma and only spend frontier tokens on decisions, review, and hard
  synthesis.

## Operating Rules

- Each confirmatory or calibration experiment needs a Decision Packet and
  PROMPT before execution.
- Sidecars may draft, summarize, search, classify, and check receipts; they must
  not silently mutate canonical science/governance state.
- Antigence project antibodies may flag/block/escalate only within their
  calibration and claim ceiling.
- Distillation outputs are evidence bundles, not promoted claims.
- Any experiment that requires long GPU runtime must be operator-scheduled and
  single-writer.

## Experiment Sequence

### EXP-002: Inert-Control Calibration

**Status:** registered, ready for SWE.
**Prompt:** `prompts/PROMPT_002_INERT_CONTROL_CALIBRATION.md`.
**Question:** does the T9b inert wobble reproduce across inert prompt forms, or
was it a single cold-start artifact?

Tasks:

- Run five inert prompt classes: lorem, shuffled neutral text, repeated neutral
  sentence, random tokens, bland factual paragraph.
- Keep T9b settings unchanged: `qwen2.5-coder:7b`, `N=50`, `M=5`, seed `42`,
  temperature `0.0`.
- Emit `runs/swarm_calibration_<UTC>/calibration_verdict.json`.

Pass gate:

- All inert prompt forms have max JSD `<0.02`.

If partial/fail:

- Route to EXP-003 to localize generation versus aggregation sensitivity.

### EXP-003: Aggregator Null-Model Audit

**Status:** planned; needs Decision Packet and PROMPT after EXP-002 output.
**Question:** is the inert wobble produced by persona generation or by the
aggregation/JSD estimator at `N=50`?

Tasks:

- Replay frozen inert outputs from T9b and EXP-002 where available.
- Bootstrap/permutation-resample persona stances.
- Recompute round-over-round JSD null distributions.
- Compare observed `0.0412` against the null band.

Pass gate:

- The source of wobble is classified as one of:
  `generation_drift`, `aggregation_sensitivity`, `sampling_noise`, or
  `mixed`.

Follow-on:

- If generation drift: remediate prompt/persona behavior.
- If aggregation sensitivity or sampling noise: remediate metric/baseline
  protocol, not model behavior.

### EXP-004: Inert Prompt / Metric Remediation

**Status:** gated on EXP-002 and EXP-003.
**Question:** can we make inert controls pass without damaging real-scenario
detectability?

Candidate interventions:

- neutral warm-up/baseline round excluded from the strict metric;
- stronger "no meaningful stance / abstain" instruction;
- inert-specific abstention handling;
- clustering/JSD threshold adjustment backed by EXP-003.

Pass gate:

- inert max JSD is `<0.02` under the selected metric;
- the original four real scenarios still show acceptable detectability;
- remediation is documented as an amendment before T9c.

### T9c: Phase 70 Acceptance Rerun

**Status:** gated on EXP-004.
**Question:** does the remediated predictive swarm achieve a clean 5/5 MESI
PASS?

Tasks:

- Rerun the original five scenarios.
- Add expanded inert controls only if EXP-002 justifies amending the suite.
- Rerun T10 audit after verdict generation.

Pass gate:

- clean 5/5 MESI PASS and authoritative T10 audit.

## Real-Time Bridge And Sidecar Experiments

### EXP-005: RTB-01 Bridge Event Spine Proof

**Question:** can Ollarma emit replayable bridge events for ChatGPT Codex,
Claude Code, and local operators without changing `/chat` or `/route`
semantics?

Tasks:

- Implement `BridgeEvent` and append-only `.ollarma/bridge/events.jsonl`.
- Emit coarse events for chat, route, KB, blocked, and escalated outcomes.
- Add recent-event and SSE replay endpoints.
- Run a localhost proof with one Codex sidecar and one Claude Code sidecar
  reading the same stream.

Pass gate:

- both agents can reconstruct the same event sequence by `run_id`;
- reconnect-by-last-event-id produces no duplicates;
- no secrets appear in bridge payloads;
- existing `/chat`, `/route`, and SSE tests remain green.

### EXP-006: Multi-Agent Token-Offload Sidecar Trial

**Question:** how much routine SWE/context work can Ollarma sidecars absorb
before frontier agents need to spend tokens?

Tasks:

- Define sidecar roles:
  `repo_scout`, `test_triage`, `receipt_summarizer`, `doc_locator`,
  `antibody_precheck`, `handoff_compactor`.
- For a fixed task set, run frontier-only baseline vs frontier+Ollarma sidecar.
- Measure frontier tokens, elapsed time, correction count, and missed-risk
  findings.

Pass gate:

- at least 30% frontier-token reduction on routine context gathering;
- no increase in missed blocking findings;
- every sidecar output has source refs and a bridge event receipt.

Hard stop:

- sidecars may not make final merge/claim/closeout decisions.

### EXP-007: Claude Code / Codex Handoff Coherence Trial

**Question:** can the bridge prevent duplicated work between Claude Code agents
and Codex agents?

Tasks:

- Reproduce a controlled version of the RTB-01 duplicate-build failure mode.
- Add bridge-level ownership claims for file surfaces and task ids.
- Require agents to read claims before starting overlapping work.

Pass gate:

- duplicate implementation attempts are reduced to zero in the fixture task;
- conflicting ownership claims produce a visible `blocked` or `needs_operator`
  event instead of parallel writes.

## Antigence Antibody Experiments

### EXP-008: Project Antibody Profile Coverage

**Question:** which repos have missing safety/domain antibodies, and can Ollarma
surface those gaps before execution?

Tasks:

- Scan active project adapter manifests for `antibodies` and
  `antibody_profile`.
- Classify missing domains:
  science claims, citation/provenance, security, method validity, data
  contracts, UX/public-communication, notebook independence, privacy/leakage.
- Produce a per-repo coverage matrix.

Pass gate:

- every active repo has a coverage classification;
- high-risk repos have at least a planned project-owned antibody pack;
- missing domains are registered as future EXP/PROMPT candidates, not silently
  treated as safe.

### EXP-009: Antigence Project-Pack Pilot

**Question:** can one project-owned antibody pack run as a bounded sidecar and
preserve provenance?

Candidate first repo:

- `deltaprot` if Phase 71 notebook workflow remains first target;
- `bioviz-atlas` if model-comparison provenance is the immediate priority;
- `antigence` if security/sentinel lanes are the priority.

Tasks:

- Define one project-owned antibody profile.
- Include at least one core lane, one project-owned lane, and one imported
  shared lane if available.
- Emit lane verdict provenance: owner, source digest, allowed use, claim
  ceiling, calibration status.

Pass gate:

- advisory/block/escalate authority is explicit;
- open-calibration heads cannot emit authoritative block verdicts;
- disagreement is preserved rather than averaged away.

### EXP-010: Antibody Drift And Reuse Audit

**Question:** can exported antibodies be reused across repos without becoming
silent global authority?

Tasks:

- Export one antibody from a source project with digest and allowed uses.
- Import it into another project by digest.
- Change the source digest and verify the importer fails closed.

Pass gate:

- reuse is opt-in on both sides;
- digest drift blocks or degrades review;
- bridge events show why the lane was blocked/degraded.

## Distillation And Training Experiments

### EXP-011: Distillation Receipt Pilot

**Question:** can Ollarma distill multi-agent output while preserving safety
flags, dissent, and source hashes?

Tasks:

- Use Phase 69 proof output, Phase 70 verdict output, and RTB/antibody review
  outputs as source refs.
- Produce a `DistillationReceipt` with uncertainty, omissions, safety flags,
  and `no_claim_promotion=true`.

Pass gate:

- changing any source hash changes the receipt id;
- safety findings survive distillation;
- no claim ceiling is raised.

### EXP-012: Small-to-Large Distillation Dataset

**Question:** can local sidecar outputs become a training/evaluation corpus for
smaller models without laundering frontier judgments?

Tasks:

- Build a dataset from accepted sidecar tasks:
  input, local output, frontier correction, final accepted answer, receipts.
- Label examples by role: scout, triage, summarizer, antibody precheck,
  bridge-router, distiller.
- Separate training candidates from evaluation holdout.

Pass gate:

- dataset examples include provenance and correction labels;
- no private secrets or unredacted payloads;
- frontier answer is labeled as teacher/correction, not ground truth unless
  reviewed.

### EXP-013: Small-Model Role Improvement Trial

**Question:** does distillation or prompt-tuning improve a local model on one
sidecar role enough to reduce frontier usage further?

Candidate roles:

- receipt summarizer;
- repo scout;
- test-failure classifier;
- antibody precheck.

Pass gate:

- improved local model beats the pre-distillation baseline on held-out examples;
- no increase in unsafe omissions;
- latency stays within sidecar budget.

## Cross-Repo Workflow Experiments

### EXP-014: Phase 71 Notebook Workflow Fixture

**Question:** can Ollarma execute validated notebook workflows with receipts
without touching real science repos?

Tasks:

- Implement fixture-only validated notebook DAG.
- Reject wrapper notebooks using `%run`, `!python`, `subprocess`, or
  `scripts/exp*.py` delegation.
- Emit `notebook_workflow_receipt`.

Pass gate:

- fixture happy path passes;
- wrapper notebooks fail closed;
- no execution against real science repos.

### EXP-015: First Real Notebook Workflow Proof

**Question:** can the Phase 71 runtime handle the first operator-approved real
repo without writeback?

Candidate:

- `deltaprot`, per current Watchtower-confirmed first-target note.

Pass gate:

- operator approval exists;
- target remains read-only/no-writeback unless explicitly approved;
- gsigmad EXP/PROMPT linkage and claim ceiling are enforced before and after.

## Missing-Domain Discovery

### EXP-016: Missing Domain Inventory

**Question:** what domain capabilities are absent from the current local
sidecar/antibody stack?

Tasks:

- Inventory portfolio tasks and classify domains:
  wet-lab biology, proteomics, clinical safety, software security, data
  engineering, notebooks, UX/public explanation, policy/legal-adjacent,
  finance/cost, infrastructure reliability.
- Map each domain to current local model capability, antibody coverage, and
  escalation path.

Pass gate:

- every domain has one of: covered, planned, intentionally out-of-scope, or
  requires frontier/operator review;
- missing high-risk domains become future antibody or distillation candidates.

### EXP-017: Domain Router Gap Trial

**Question:** can Ollarma detect "missing domain" cases before local sidecars
hallucinate expertise?

Tasks:

- Build a small evaluation set of prompts from uncovered domains.
- Test route outcomes: local answer, local abstain, antibody flag, frontier
  escalation, operator handoff.

Pass gate:

- high-risk missing-domain prompts do not receive unqualified local answers;
- route receipts state the gap and escalation reason.

## Recommended Execution Order

1. EXP-002: inert-control calibration.
2. EXP-003: aggregator null-model audit, if EXP-002 is not a clean PASS.
3. EXP-004 + T9c: remediation and clean Phase 70 acceptance.
4. EXP-005: RTB event spine proof.
5. EXP-006/007: multi-agent sidecar offload and handoff-coherence trials.
6. EXP-008/009/010: antibody coverage and project-pack pilots.
7. EXP-011/012/013: distillation receipts and small-model role improvement.
8. EXP-014/015: notebook workflow fixture then real proof.
9. EXP-016/017: missing-domain inventory and router gap trial.

## Near-Term SWE Backlog

- Run EXP-002 from `PROMPT-002` when GPU/runtime is clear.
- Draft EXP-003 decision packet only after EXP-002 verdict exists.
- Implement RTB-01 if Phase 70 remediation is not currently using the GPU.
- Add bridge ownership-claim events before running more parallel Claude/Codex
  build agents on the same surface.
- Start antibody coverage inventory as a read-only scan; do not activate
  blocking antibody lanes until calibration/authority status is known.

## What Not To Do Yet

- Do not pre-register EXP-004 before EXP-002/003 identify root cause.
- Do not mark Phase 70 clean until T9c and authoritative T10 pass.
- Do not train or fine-tune on sidecar outputs before distillation receipts and
  redaction checks exist.
- Do not make shared antibodies global by default.
- Do not let Ollarma write canonical KG/EXP state outside gsigmad governance.
