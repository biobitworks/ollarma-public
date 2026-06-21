# Burst Compute Tier — Kaggle GPU (~30h/week) for bio models + CAFA6 batch inference

Adds a **4th compute tier** to the magicSTUDIObox inference design: cloud **burst** capacity for
workloads that cannot run on the local box. Operator context (2026-06-15): ~**30 GPU-hours/week**
on Kaggle, runnable in **bursts as needed**, plus CAFA6 project inputs.

## Why this tier exists
The maxmodel experiment marked these **BLOCKED locally** — they are PyTorch/HF (not Ollama-served),
so they can't run on magicSTUDIObox: **ESM3 / ESM2, RFdiffusion3, Evo 2, BioMedLM, OpenBioMed**.
They ARE standard GPU batch jobs → they belong on the burst tier. Same for any LLM too large for the
32 GB box when an occasional hard task justifies it. And CAFA6 (protein-function prediction over many
sequences) is inherently batch — a natural burst workload.

## The escalation ladder, extended
```
rule_floor → antigen_bank → tiny_cell → model_judge → local_big_worker(30B) → BURST_BATCH(Kaggle GPU)
   └────────── magicSTUDIObox, always-on, interactive ──────────┘   └─ cloud, batch, budgeted ─┘
```
Ollarma escalates to the burst tier only when: (a) the task needs a model that **cannot** run locally
(protein structure/embedding, genomic, structure generation), or (b) batch volume justifies a GPU
burst — **and** weekly budget remains. Otherwise it stays local. Fail-closed: budget exhausted →
queue for the next window, never silently overspend.

## Honest constraint (shapes the design)
Kaggle GPU = **session-based notebooks** (~9–12 h/session, ~30 h/week total), **not** a persistent
low-latency API. So the burst tier is **queued batch jobs**, not interactive serving:
- ✅ great for: ESM/Evo2 embeddings, RFdiffusion structure generation, BioMedLM batch QA, CAFA6
  prediction sweeps — all batch, latency-tolerant.
- ❌ not for: interactive chat (that stays on the local box).
- If a persistent bio **endpoint** is ever needed, the alternatives are HF Inference Endpoints /
  Modal / Replicate (real APIs, metered) — out of scope for the 30 h/week Kaggle plan.

## Job + budget contracts (sidecars, per portfolio rule)
- **Burst job** `docs/burst_jobs.jsonl` (candidate queue): `{job_id, model, task, inputs_ref,
  params, est_gpu_min, status: queued|running|done|blocked, requested_by, created}`.
- **Budget ledger** `docs/burst_budget.jsonl`: `{week, gpu_min_budget: 1800, gpu_min_spent,
  remaining}` — 30 h = 1800 min/week. Decrement per completed job from its receipt.
- **Job receipt** `.planning/receipts.jsonl`: `{job_id, model, gpu_min_used, inputs_sha256,
  outputs_sha256, kaggle_kernel, finished}`. Results stay **PROVISIONAL** until gsigmad/Overwatch
  validation (`gsigmad-audit-output`). No secrets / restricted data in committed sidecars.

## Execution pattern (burst, not endpoint)
1. **Enqueue** a job row (`burst_jobs.jsonl`) — model + inputs (e.g. a FASTA ref) + params + budget.
2. **Kaggle notebook** (scheduled or manually kicked) reads the queue, checks the budget ledger,
   pulls inputs, runs the model on GPU, writes outputs + a receipt (sha256 + GPU-min) to a shared
   results location, marks the job `done`.
3. **Writeback** is local-first: results land as candidates; Ollarma/Watchtower surface them; only
   operator-approved promotion to KG via the gsigmad writeback skills.
4. **Watchtower** renders the queue + budget read-only (same pattern as the maxmodel card).

## CAFA6 tie-in
CAFA6 = protein function prediction. The burst tier produces the GPU artifacts the existing CAFA lane
consumes: **ESM/Evo2 embeddings + structure features** feed Antigence's `verify-function-text` /
`claim_entailment` antibodies (NLI + claim-evidence overlap). So: burst tier *generates* predictions
& embeddings (batch) → local antibodies *verify/score* them (cheap, deterministic) → governed
writeback. See `CAFA_HANDOFF.md`, `prompts/PROMPT_005_CAFA_A1_CLAIM_ENTAILMENT_CALIBRATION.md`.

## Governance / boundary
Ollarma routes + tracks budget; it does not own truth. Burst results are runtime evidence
(PROVISIONAL) until gsigmad/Overwatch validate. The 30 h/week is a hard ceiling enforced by the
ledger; bursts are explicit operator-budgeted actions, not silent escalations. Keep Kaggle
credentials out of the repo (local-only / env), and no restricted sequence data in committed sidecars.

## Next concrete steps (skeleton → action)
- [ ] Freeze the burst-job + budget JSON schemas (above) and seed `burst_budget.jsonl` at 1800 min/wk.
- [ ] Write the Kaggle notebook template: read queue → check budget → run model (start with **ESM2
      embeddings**, the simplest high-value bio job) → write outputs + receipt.
- [ ] Decide the trigger: manual kick / Kaggle schedule / Watchtower button (read-only enqueue).
- [ ] Wire the burst rung into Ollarma routing as a budget-gated escalation (cannot-run-locally → burst).
- [ ] Point the CAFA lane antibodies at burst-produced embeddings.
```
Open question for the operator: which bio job first — ESM2 embeddings for CAFA6, or a different model?
```
