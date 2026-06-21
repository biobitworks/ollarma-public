# Criterion #5 — PI Decision (Phase 70 inert-control)

**Decision owner:** PI (this consolidation pass) · **Date:** 2026-06-10
**Status:** DECISION FRAMEWORK — final selection filled from the live EXP-002 verdict (run `runs/swarm_calibration_20260610T140157Z/`). Until the verdict lands, claim C10 in `CLAIMS_LEDGER.md` stays **OPEN**.

---

## 1. What criterion #5 requires

v5.1 ship gate #5: *Phase 70 acceptance has 5/5 MESI gates PASS on the pre-registered benchmark suite.*

**Current evidence (T9b, `420d2ae`, 2026-05-31):**
- ✅ 5/5 named MESI gates PASS: `api_contract`, `wall_time`, `jsd_detectability` (5/5), `resilience_quarantine` (≤0.4%), `thermal_hygiene`.
- ⚠️ Inert-control **strict-cleanliness sub-criterion** FAIL: max round-over-round JSD `0.0412` ≥ the `0.02` cleanliness gate, from the cold-start round0→round1 transition out of unanimous-neutral.
- ✅ The `0.05` **falsification threshold** was NOT crossed.

So the *named gates* pass; the *sub-criterion* on inert cleanliness is the lone failure. The question EXP-002 answers: **does that round-1 wobble reproduce across inert prompt forms, or was it a single-prompt cold-start artifact?**

## 2. The science-integrity constraint

Per the portfolio rule and gsigmad claim-ceiling discipline: a parameter/threshold cannot be moved post-hoc to manufacture a PASS, and a conclusion cannot be stated above its evidence. The `0.02`/`0.05` thresholds are inherited verbatim from PROMPT-OLLARMA-SWARM-001/002 and are **read-only**. The PI may *accept PARTIAL with documented limitations* (an explicitly sanctioned resolution path in `.planning/STATE.md`), but may **not** relabel PARTIAL as PASS.

## 3. Decision tree (selected branch marked after the verdict lands)

The EXP-002 verdict taxonomy is fixed by PROMPT-002:

### Branch A — verdict `PASS` (all 5 inert prompts max JSD < 0.02)
- **Interpretation:** the T9b 0.0412 wobble was a **single-prompt cold-start artifact**; the engine is clean across inert forms.
- **PI decision:** the inert-control sub-criterion is met *on the calibration suite*. Recommend criterion #5 advance to **candidate-MET pending a T9c acceptance rerun** (the calibration is a diagnostic, not the acceptance run itself). Do **not** unilaterally close #5 without T9c; this is honest scoping, not a block.
- **Carry-forward:** schedule T9c (operator GPU window) to confirm on the *acceptance* driver.

### Branch B — verdict `CALIBRATION_BASELINE` (one+ prompt ∈ [0.02, 0.05]; none > 0.05)
- **Interpretation:** the wobble **reproduces** as a low-level baseline of the cold-start step, but never approaches falsification. It is a **measured property of the inert control**, not an engine defect.
- **PI decision (recommended):** **ACCEPT PARTIAL WITH DOCUMENTED LIMITATIONS.** Record that 5/5 named MESI gates pass and the inert control sits in the calibration-baseline band [0.02, 0.05] driven by the unanimous-neutral cold-start. Route to **EXP-003 null-model audit** as the follow-on if/when a stricter cleanliness claim is later needed. Criterion #5 ships as **MET-with-limitations**; v5.1 no longer blocked on a perfectionist sub-threshold that falsification does not support.
- **Why this is defensible:** the falsification threshold (the real "is the engine broken?" test) is not crossed; the 0.02 gate is a *cleanliness ideal*, and a documented baseline below falsification is a normal, honest scientific outcome.

### Branch C — verdict `FAIL` (one+ prompt > 0.05)
- **Interpretation:** the inert control crosses falsification on at least one form — an **engine-level** signal that the swarm produces non-trivial stance drift on meaningless input.
- **PI decision:** criterion #5 **stays BLOCKED**; reopen Phase 70. Do not ship v5.1 on #5. Route to engine remediation (aggregation/seed/cold-start handling) before any further acceptance attempt.

## 4. Decision record (filled from the live verdict)

**Live run:** `runs/swarm_calibration_20260610T140157Z/` · qwen2.5-coder:7b · N=50 × M=5 · seed=42 · temp=0 · settings_match_t9b=true · total wall 5044 s (84 min) · quarantine 0.0 throughout.

> **VERDICT:** `FAIL` — **engine falsification.** 2 of 5 inert prompts exceeded the 0.05 falsification threshold.
> **SELECTED BRANCH:** **C** (engine-level falsification).
> **PI DECISION:** Criterion #5 is **NOT MET and stays BLOCKED.** Phase 70 clean closeout is **stopped.** v5.1 does **not** ship on criterion #5. Register as a negative result; route to engine remediation under a *new* pre-registration — do **not** proceed to EXP-003/004 "as if the engine is sound," and do **not** silently fix-and-rerun.
> **CLAIM C10 RESOLUTION:** C10 ("criterion #5 PASS") is **resolved FALSE.** The inert-control strict-cleanliness sub-criterion is not merely unmet — the falsification gate *fired*.

### Per-prompt evidence

| inert prompt | max JSD | source step | strict-clean (<0.02) | falsified (>0.05) |
|---|---|---|---|---|
| inert_1_lorem | 0.0412 | round0→1 | ✗ | ✗ |
| inert_2_shuffled_neutral | 0.0203 | round0→1 | ✗ | ✗ |
| inert_3_repeated_sentence | 0.0146 | round1→2 | ✓ | ✗ |
| **inert_4_random_tokens** | **0.0810** | round0→1 | ✗ | **✓** |
| **inert_5_bland_factual** | **0.0858** | round0→1 | ✗ | **✓** |

### What the data says (PI reading)

1. **The wobble reproduces and worsens.** T9b's 0.0412 was *not* a single-prompt artifact — it reproduced exactly on `inert_1_lorem` and grew to ~0.085 on random-token and bland-factual inputs. The earlier PARTIAL was an under-estimate of the problem, not an over-estimate.
2. **The failure is localized to the cold-start.** Every falsifying value is the **round0→round1** transition out of unanimous-neutral; all later rounds sit ≤0.015. The engine is stable once seeded but manufactures spurious stance differentiation in its *first* deliberative round on certain noise inputs.
3. **This is diagnosable, not mysterious** — but diagnosability does not downgrade the verdict. Falsification fired; the honest label is FAIL.

### Disposition (PI/PM/O/SWE, owning all four roles)

- **PI:** accept the FAIL. Do not relabel, do not amend thresholds (`supports_threshold_amendment=false`; falsification is not a threshold question). Register the negative result.
- **PM:** criterion #5 remains the open v5.1 blocker; milestone does **not** advance to ship on this gate. Update STATE accordingly.
- **O:** the live run owned the GPU single-writer; evidence committed; no production swarm logic or threshold changed.
- **SWE:** the remediation hypothesis is **persona-generation confabulation** (round_0 already non-neutral on noise, pre-aggregation). Pursued under EXP-004 (below) with a *new* pre-registration, not a silent patch.

---

## 6. EXP-004 remediation result (2026-06-10) → NOT REMEDIATED; v5.2 re-scope

I attempted the genuine remediation under pre-registration (`PROMPT-004`, written before the code change). The fix: an explicit abstention affordance in the persona prompt. **Dual gate, both required: inert clean AND real-scenario detectability preserved (anti-lobotomy).**

| gate | result |
|---|---|
| Inert (5 prompts, N=50×M=5) | **PASS (strong)** — all 5 max JSD = **0.0000** (was 2/5 falsified). Confabulation eliminated. `runs/swarm_calibration_20260610T160004Z/` |
| Detectability (2 polarizing scenarios, anti-lobotomy) | **FAIL** — both real scenarios (AI-receipt policy, biotech trial) also collapsed to 100% neutral round_0. `runs/swarm_calibration_20260610T160004Z/detectability_check.txt` |

**Verdict: NOT REMEDIATED.** The blanket abstention passes inert by silencing *all* stance-taking — a lobotomy the pre-registered anti-lobotomy gate caught. With a 7B code model, a blunt "abstain on non-substantive input" instruction cannot separate noise from a contestable claim. Engine change **reverted** (baseline restored; 173 swarm tests pass). Full note: `experiments/EXP_OLLARMA_SWARM_004_inert_abstention_remediation.md`.

### PI/PM decision — re-scope (the honest closure)

Two distinct facts are now established with evidence: (a) the predictive swarm **confabulates** stance on noise (EXP-002 falsification), and (b) the obvious fix **lobotomizes** real debate (EXP-004). Neither the original nor the patched engine passes Phase 70 acceptance. A correct fix (discriminating abstention) is genuine future work needing its own pre-registration.

Therefore, as PI/PM owning the milestone:

- **v5.1 ships its five met criteria** — #1–#4 (execution-lane swarm: lane runtime, checkpoint/resume, routing ladder, proof run) and #6 (notebook workflow runtime). These are the working, tested substrate (1672 tests).
- **Criterion #5 (the predictive deliberative swarm, Phase 70) is split out into a new v5.2 milestone**, carrying both negative results (EXP-002 confabulation, EXP-004 lobotomy) and the v5.2 remediation hypothesis (discriminating abstention: pre-classifier, non-code judge, or few-shot calibration; same dual gate).
- This is **closure, not abandonment**: the working product ships; the falsified research feature is deferred with a documented, evidence-backed path. Criterion #5 is **never relabeled PASS**.

The two swarm tracks were always distinct (ROADMAP: "Two distinct swarm tracks, both in v5.1"). The execution-lane track works and ships; the predictive track needs more science and moves to v5.2.

## 5. What this decision does NOT do

- Does not move the `0.02`/`0.05` thresholds.
- Does not relabel a PARTIAL/BASELINE as a clean PASS.
- Does not close T9c — even under Branch A, the acceptance rerun is a separate operator-gated step.
- Does not, by itself, tag or promote v5.1; tagging is operator-driven and out of scope for this pass.
