# Unification Plan — One Trainable Local Verification Package

**Compiled:** 2026-06-10 · **Role:** PI/PM/Operator/SWE
**Goal (verbatim from the operator):** "unify into a single package that a user can download and train themselves based on their unique needs … save tokens, run local, private, secure and safe data analysis with deterministic guardrails … orchestration comes in when you need to escalate to a frontier model that requires much larger hardware than what we have."
**Terminology:** proper research terms per [`publication/TERMINOLOGY_EQUIVALENCE.md`](publication/TERMINOLOGY_EQUIVALENCE.md).

---

## 1. What the unified package is

**One downloadable tool: a local, offline communication / safety / verification layer for AI-assisted computational research.** It combines two existing projects:

- **Ollarma** — the runtime: model cascade, routing/escalation, deterministic execution boundary, receipt chain, recovery, bench harness, swarm engine.
- **Antigence** — the verification packs: antibodies (verifiers/guardrails), antigen banks (retrieval exemplar sets), the immune-inspired review/escalation lane.

Unified, they become: *a user installs one package, points it at the models they already run locally, and the package profiles those models and assigns each to a role in a verification cascade — then runs their data analysis privately, checks every AI output against deterministic + learned guardrails, and escalates to a frontier model only when local evidence is insufficient.*

The token-saving thesis: **most steps never reach a frontier model.** Cheap local experts screen and verify; the expensive call happens only on the residual hard cases (learning-to-defer). This is the FrugalGPT/Hybrid-LLM/RouteLLM cost argument made concrete and private.

---

## 2. "Train themselves based on their unique needs" — what training means here

The user does not have to fine-tune a model. "Train" = **the package calibrates itself to the user's hardware, models, and domain.** Three calibration loops, all already prototyped in this repo:

| Loop | What it learns | Existing machinery |
|---|---|---|
| **A. Roster profiling** | Which of the user's local models can fill which cascade role (screen / verify / judge / route / escalate / reason / ceiling), given their hardware. | the bench harness (`registry.py`, `scorers.py`, `reporter.py`) + co-residency probe (`experiments/coresidency/`) + the role-ladder evidence (`ANTIGENCE_MODEL_SIZE_CELL_ROLE_FINDINGS.md`) |
| **B. Antibody calibration** | The decision thresholds for each domain verifier (what counts as overreach / unsafe / off-policy for *their* field). | antibody policy (`antibody_profile.py`, `antibody_model_policy.py`) + antigen-bank calibration_status + claim-ceiling review |
| **C. Escalation tuning** | When local confidence is too low and a frontier call is worth the cost/privacy trade. | routing ladder (`routing_ladder.py`, `escalation.py`) + gateway receipts |

Output of training = a **per-user policy bundle** (which model → which role, which thresholds, which escalation triggers), produced by running the user's own data/models through the harness. Reproducible, receipted, and re-runnable as their models evolve — exactly the "rerun as models evolve" value in the project charter.

---

## 3. Architecture of the unified package

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │  ollarma-verify (single installable package)                         │
  │                                                                        │
  │   ┌─ CALIBRATE (one-time + on model change) ─────────────────────┐   │
  │   │  roster profiling → role assignment  (bench + coresidency)   │   │
  │   │  antibody calibration → thresholds   (antigence packs)       │   │
  │   │  writes: per-user policy bundle + receipts                   │   │
  │   └──────────────────────────────────────────────────────────────┘   │
  │                                                                        │
  │   ┌─ RUN (per task, offline by default) ─────────────────────────┐   │
  │   │  screen → verify → judge → route → escalation rungs          │   │
  │   │  deterministic execution boundary (allow-listed asset classes)│   │
  │   │  every step → hash-chained provenance receipt                │   │
  │   │  escalate to FRONTIER only on insufficient local evidence    │   │
  │   └──────────────────────────────────────────────────────────────┘   │
  │                                                                        │
  │   ┌─ VERIFY / AUDIT ─────────────────────────────────────────────┐   │
  │   │  claim-ceiling review · receipt-chain replay · manifest sha256│   │
  │   └──────────────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────────┘
        local Ollama models (user's own)        frontier provider (opt-in, receipted)
```

The three blocks map to the three things the user asked for:
- **CALIBRATE** = "train themselves based on their unique needs."
- **RUN** = "local, private, secure, safe data analysis with deterministic guardrails" + "save tokens."
- **escalate to FRONTIER** = "orchestration … escalate to a frontier model that requires much larger hardware."

---

## 4. What already exists vs what unification needs

**Already built (in this repo, tested):**
- the full RUN pipeline (cascade, guardrails, execution boundary, receipts) — 1672 passing tests
- the bench harness for roster profiling
- the co-residency probe for hardware limits
- the swarm engine for deliberative adjudication
- the antibody policy schema + resolver
- the gateway + escalation receipt contract

**Needs building for unification (the roadmap):**

| Item | Why | Effort |
|---|---|---|
| **U1. `ollarma calibrate` command** | one entrypoint that runs roster profiling + antibody calibration and emits the per-user policy bundle | ✅ **FIRST CUT BUILT** — `scripts/ollarma_calibrate.py` profiles the live local roster (Ollama `/api/tags`), classifies each model into a cascade role via the measured size→role ladder, warns on missing roles, and writes a portable policy bundle JSON. Verified live: profiled 21 local models → all cascade roles covered, 0 gaps. Deeper version (bench-driven quality profiling) is the next iteration. |
| **U2. Antigence-as-dependency** | pull Antigence antibody packs into the same install (today they're a separate repo) | M — package boundary + import contract; respects the cross-repo governance split |
| **U3. Bring-your-own-models onboarding** | detect the user's `ollama list`, map to roles, warn on gaps (no screen model, no router, etc.) | S — `reserved_models.py` + role-ladder already encode the logic |
| **U4. Frontier provider plug** | opt-in adapters (the PROVIDER_ROADMAP: Anthropic live, OpenAI/Gemini/Grok staged) behind the escalation receipt | M — gateway exists; provider adapters are the work |
| **U5. Policy-bundle portability** | export/import the per-user policy so a lab can share a calibrated config (without sharing data) | S — it's a manifest + sidecars |
| **U6. Single installer + public tree** | `pip install ollarma-verify`; built from the public extraction (`public-release/PUBLIC_RELEASE_PLAN.md`) | M — depends on the public tree builder |

Sequencing: **U3 → U1 → U2 → U4 → U5 → U6.** U3/U1 deliver self-calibration on local models (the headline). U2 folds in Antigence. U4 lights up real escalation. U6 ships it.

---

## 5. The three audiences get the same engine, different bundles

Ties to [`public-release/LICENSING_TIERS.md`](public-release/LICENSING_TIERS.md):
- **Public:** the engine + `calibrate` + synthetic example packs. A user can self-host, profile their own models, and run verified local analysis.
- **Academic:** adds the pre-registration / claim-ceiling / provenance research pack — for reproducible, citable AI-assisted research.
- **Corporate:** adds turnkey integration adapters, governance backends, support, and indemnification.

Safety/verification features are free in **all** tiers (LICENSING_TIERS §2 principle).

---

## 6. Honest boundaries

- This is a **plan with a working anchor**, not yet a shipped unified package. **U1 (`ollarma calibrate`) has a built, live-verified first cut**; U2–U6 are designed but unbuilt; the engine they unify is built and tested (1672 tests).
- Antigence lives in a separate repo with its own governance; unification must preserve the boundary (Ollarma owns local runtime receipts; Antigence owns risky review/escalation) even inside one installable — they merge as *packages*, not as a collapsed codebase.
- "Train" here is **calibration**, not model fine-tuning. If true fine-tuning (e.g., a custom domain verifier) is later wanted, that's a separate, larger track and should be scoped on its own.
- Frontier escalation requires the user's own provider key and network; the offline-by-default guarantee holds until they opt in, and every escalation is receipted (invariant I-02: no silent fallback).
