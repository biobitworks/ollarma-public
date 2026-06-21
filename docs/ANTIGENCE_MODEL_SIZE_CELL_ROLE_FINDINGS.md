# Antigence Update — Model Size → Role → Antibody-Lane Findings

**From:** Ollarma (PI/PM/Operator/SWE consolidation pass), 2026-06-10
**To:** Antigence (immune-inspired review/escalation lane)
**Status:** MEASURED, host-specific advisory. Policy seed for Antigence's antibody model policy, not a runtime change to Antigence.
**Terminology:** uses the proper research terms from [`publication/TERMINOLOGY_EQUIVALENCE.md`](publication/TERMINOLOGY_EQUIVALENCE.md). The immune metaphor is kept in parentheses for continuity only.
**Host of record:** Mac Studio M1 Max (`Mac13,1`), 32 GB unified, Ollama 0.24.0 (MLX).

---

## TL;DR for Antigence

1. **Treat the model roster as a cascade, not a pool.** Cheap experts do first-pass screening; only hard/low-confidence cases escalate one rung at a time, ending at a frontier model. This is **learning-to-defer / model-cascade** behavior [Mozannar & Sontag 2020; FrugalGPT 2023], not parallel "one big model per agent."
2. **The model-count cap (3), not memory, is the binding constraint on the reference host.** With the rescue + embed models pinned, exactly **one working model** is resident at a time under default config. Antibody fan-out must therefore be *cheap-model fan-out* or *sequential escalation*, never concurrent big-model fan-out.
3. **Routers and verdict-producing antibodies require grammar-constrained decoding.** The best tier-calibrating model on this host (granite4.1:8b) leaks prose/markdown without a JSON grammar; the small structured experts (qwen3.5:2b/4b) do not. Enforce `json_grammar` before any antibody is allowed to `block`.
4. **The smallest model is a sensor, not a verdict producer.** qwen2.5:1.5b detects overreach (high recall) but breaks schema — use it as a screening alarm that *escalates*, never as an authoritative blocker.
5. **qwen3:1.7b stays reserved for Antigence/Sentinel.** Ollarma's bridge returns `RESERVED_MODEL_ANTIGENCE_SENTINEL` for it by governance, confirmed under live stress. This reservation is working as designed; Antigence owns that slot.

---

## 1. The measured role ladder (which model fills which role)

Each row is a **cascade rung**. "Role" uses the proper term; "(metaphor)" keeps the immune nickname. Evidence: live antibody-role probes (`docs/EVIDENCE_RTB02_ESCALATION_LADDER_CELLICO_BIO_2026-05-31.md`) + co-residency footprints (`experiments/coresidency/MATRIX.md`).

| Model | Size | Resident VRAM (ctx4096) | Role (proper term) | (Metaphor) | Strict output | Residency posture |
|---|---|---|---|---|---|---|
| qwen2.5:1.5b | 1.5B | 1.3–2.2 GB | high-recall screening classifier / fallback | recall sensor + rescue cell | none (schema-unreliable) | **pinned** |
| nomic-embed-text | 137M | 0.54 GB | retrieval embedder (exemplar matching) | antigen-bank index | n/a | **pinned** |
| qwen3.5:2b | 2.7B | 3.83 GB | structured-output expert (verifier floor) | cell-type floor | `json_mode` | warm |
| qwen3.5:4b | 3.4B | 5.38 GB | stronger structured-output expert | cell-type | `json_mode` | warm-if-room |
| phi4-mini | 3.8B | 2.69 GB | LLM-as-a-judge / scorer | judge cell | `json_mode` | warm-if-room |
| granite4.1:8b | 8B | 5.28 GB | router (cascade dispatcher) | router cell | **`json_grammar` required** (leaks prose otherwise) | warm-if-room |
| qwen3.5:9b | 9B | 7.95 GB | primary escalation rung (science triage) | escalation rung 1 | `json_mode` | warm-if-room |
| deepseek-r1:14b / phi4-reasoning:14b | 14B | 8.7–10.7 GB | reasoning rung | reasoning cell | `json_mode` | **load-on-demand, evict after** |
| 24–30B (mistral-small3.2, gemma4:26b, qwen3.6:27b, qwen3-coder:30b) | 24–30B | 15–20.6 GB | local ceiling rung (benchmark-only) | ceiling cell | `json_mode` | **load-on-demand, one at a time, evict after** |
| **frontier (cloud)** | — | n/a | **deferral target** (escalate-to-expert) | apex escalation | provider-native | escalation only, receipted |
| qwen3:1.7b | 1.7B | 1.55 GB | **reserved for Antigence/Sentinel** | sentinel | — | peer-owned, not Ollarma |

---

## 2. Hard rules for Antigence's antibody model policy

These translate the co-residency limits into enforceable policy (feeds the planned `src/ollarma/antibody_model_policy.py` and Antigence's own packs):

1. **Max one model ≥14B resident at a time.** The 24–30B tier *loads* but only by evicting the resident cheap-expert set and growing swap. Never keep a ≥14B model warm; never load two large models concurrently. → *cascade with strict load→resolve→evict ordering.*
2. **Cheap-expert fan-out is the only allowed parallelism.** With the 3-model count cap and pinned floor, concurrent big-model fan-out is physically impossible on this host. Antibody parallelism = several ≤4B structured experts; everything above the floor is **sequential** with explicit receipts. → *ensemble of cheap experts + sequential cascade above.*
3. **Grammar-constrained decoding gates blocking authority** [Geng et al. 2023]. An antibody whose lane includes a tool-caller (granite family) MUST set `json_grammar`/`format=json`. Empirically granite4.1:8b leaks prose without it; qwen3.5:2b/4b do not. A model that leaks prose cannot hold `block` authority.
4. **One rung per escalation step.** The cheap structured expert (2b) over-escalates straight to "heavy." Cap escalation to one rung per step so the cascade isn't short-circuited to the most expensive model. → *staged deferral, not jump-to-frontier.*
5. **Escalate on (a) invalid schema OR (b) confidence below the lane threshold.** The sensor's job is to *trip*, not to decide. → *selective prediction: abstain-and-defer* [Geifman & El-Yaniv 2017].
6. **Open/unknown calibration cannot produce an authoritative block/reject.** An antigen-bank lane (retrieval head) with `calibration_status ∈ {open, unknown}` is downgraded to advisory/escalation-only. LOUD degraded posture, never a silent pass. → *uncalibrated detector = advisory only.*
7. **Embedder unavailability is LOUD.** If `nomic-embed-text` is unavailable or the bridge thrashes, antigen-bank (retrieval) heads emit degraded/advisory, not a silent pass.

---

## 3. Orchestration shape (cells in roles → cascade + deliberation)

The "right type of cells acting in different roles with orchestration" maps to a concrete pipeline:

```
                    ┌─────────────────────────────────────────────────┐
   AI output  ──▶   │  SCREEN (recall sensor, 1.5B)                    │  high recall, may abstain
                    │  + retrieval match vs antigen bank (nomic-embed) │
                    └───────────────┬─────────────────────────────────┘
                                    │ trip / low-confidence / schema-break
                                    ▼
                    ┌─────────────────────────────────────────────────┐
   structured  ──▶  │  VERIFY (cheap structured experts, 2–4B)         │  json_mode; fan-out allowed
   verdict          │  + LLM-as-a-judge scorer (phi4-mini)            │
                    └───────────────┬─────────────────────────────────┘
                                    │ disagreement / below threshold
                                    ▼
                    ┌─────────────────────────────────────────────────┐
   route       ──▶  │  ROUTE (router, granite 8B, json_grammar)        │  one rung per step
                    └───────────────┬─────────────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────────────────┐
   escalate    ──▶  │  ESCALATION RUNG (9B) → REASONING (14B, on-dmd)  │  sequential, evict-after
                    │  → CEILING (24–30B local, one-at-a-time)         │
                    └───────────────┬─────────────────────────────────┘
                                    │ still insufficient / high stakes
                                    ▼
                    ┌─────────────────────────────────────────────────┐
   defer       ──▶  │  FRONTIER (cloud) — receipted escalation only    │  learning-to-defer apex
                    └─────────────────────────────────────────────────┘
```

For contested verdicts at the VERIFY/ROUTE stage, **multi-agent deliberation** [Du et al. 2023] is the adjudication primitive (this is Ollarma's Phase 70 predictive-swarm engine — N personas × M rounds, disagreement measured by Jensen–Shannon divergence). Antigence's adjudicative swarm is the sister of that engine in a separate repo; the two share the deliberation shape but not the codebase.

Every stage emits a hash-chained provenance receipt (target, antibody key, lane class, owner/source project, input/output hash, verdict, head type, embedding model, calibration status, authority status, model role, residency, strict-output mode, escalation rung). This is the verdict-provenance contract from `docs/ANTIGENCE_RTB_02_ANTIBODY_SWARM_PLAN.md`.

---

## 4. What Antigence should change

| Change | Rationale | Source |
|---|---|---|
| Adopt the §1 role ladder as the antibody model-policy seed | grounded in live probes + footprints on the shared host | EVIDENCE_RTB02 + coresidency MATRIX |
| Enforce `json_grammar` for any tool-caller antibody before granting block authority | granite leaks prose without it | EVIDENCE_RTB02 §strict-output |
| Encode "max one ≥14B resident; sequential above the floor; evict-after" | count cap + two-large-model wall | coresidency MATRIX limits 1–3 |
| Keep qwen3:1.7b reserved; do not let Ollarma serve it via /chat | governance reservation confirmed under stress | STRESS_FINDINGS §model serve ceiling |
| Treat antigen-bank (retrieval) heads with open calibration as advisory-only | uncalibrated detector ≠ authoritative blocker | RTB-02 resolution rule 11 |
| Cap escalation at one rung/step | 2b over-escalates to heavy | EVIDENCE_RTB02 §escalation trigger |

These are advisory inputs to Antigence's own policy; Ollarma does not write Antigence runtime. The cross-repo boundary (Ollarma owns local runtime receipts; Antigence owns risky review/escalation) is preserved.
