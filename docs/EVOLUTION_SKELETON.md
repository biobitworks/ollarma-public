# Evolution Skeleton — antibodies, trained ML cells, and tiny→small models

How to **evolve** Ollarma's deterministic→probabilistic pre-inference layer when the current
rungs aren't enough. This is a planning/how-to skeleton (not a commitment): it defines the tracks,
the maturity ladder, the data + training + eval gates, the integration points, and the governance.
Shared KB doc for **Ollarma** (the runtime/router) and **Antigence** (the antibody/ML producer).

Context: magicSTUDIObox hosts one big probabilistic worker + the pinned deterministic floor (see
`experiments/maxmodel/ARCHITECTURE.md`). The layer below the big model is what we evolve here.

## 0. The maturity ladder (cheapest → most capable; add a rung only when the one below misses)
| Rung | Head type | What it is | Cost | Owner | When to add |
|---|---|---|---|---|---|
| 0 | `rule_floor` | deterministic regex/AST/structural checks | µs, CPU | Antigence | always-on gate; first |
| 1 | `antigen_bank` | embedding-similarity vs a fitted vector bank (`.npz`) | ms, CPU | Antigence | when "looks like X" needs fuzzy match, not exact |
| 2 | `tiny_cell` | a tiny local model (≤2B) as a classifier/router lane | ~10ms GPU | Ollarma roster | when a decision needs language understanding but not reasoning |
| 3 | `model_judge` | a small–mid model (3–14B) as judge/verifier | 100ms+ | Ollarma roster | when verifying/scoring a generation |
| 4 | `fine_tuned_worker` | a LoRA/distilled small model specialized to a task | load + decode | new artifact | when a general model is wrong often AND you have labeled data |
| 5 | `ceiling_rung` | the big probabilistic worker (30B) | full | Ollarma roster | last resort / hard tasks |

Rule: **escalate only on miss.** Each rung must be cheaper than the one above and must *fail closed*
(abstain → escalate) rather than guess. New rungs are added at the lowest tier that solves the miss.

---

## Track A — Antibodies (rule_floor + antigen_bank), produced by Antigence
**What they are:** deterministic pattern checks (`rule_floor`) and fitted embedding banks
(`antigen_bank`: `.npz` vector banks under `.antigence/bone_marrow/`, `.pkl` fitted antibodies under
`.antigence/antibodies/`). Today: citation/pmid/doi/title antibodies, code/citations/immunology
banks, and the CAFA free-text lane (NLI + claim-evidence overlap).

**Evolve it (how-to):**
1. **Define the antigen** — the thing to detect/route (e.g. "prompt injection", "fabricated PMID",
   "unsafe shell"). Write 20–50 positive + negative examples → `data/antigens/<name>.jsonl`.
2. **Pick the head:** exact/structural → `rule_floor`; fuzzy/"looks-like" → `antigen_bank`
   (embed examples with `nomic-embed-text`, store centroid/threshold).
3. **Build it:** `antigence train-runtime` refreshes B/NK runtime state (+ receipt). For a bank,
   add the vector set to `.antigence/bone_marrow/`; for a fitted antibody, the `.pkl` under
   `.antigence/antibodies/`.
4. **Eval gate:** hold out 20% of examples; require precision ≥ target (antibodies must not
   false-positive on safe inputs). Record P/R + threshold in the receipt. **No labeled eval → no
   promotion.**
5. **Register:** add the antibody system class to Ollarma's `ANTIBODY_REGISTRY` in
   `src/ollarma/guardrail.py` and declare the lane in the project antibody profile
   (`AntibodyProfile`, head_type=`rule_floor`|`antigen_bank`).
6. **Verify integration:** confirm `import antigence` works in Ollarma's venv (today it falls back to
   pass-through — install antigence into `.venv` to make in-process lanes fire), then a project
   request hits the lane.

## Track B — Trained ML cells (classifier / NLI / embedding heads)
**What they are:** small trained models that aren't full LLMs — e.g. a logistic/SVM classifier over
embeddings, an NLI model (DeBERTa-MNLI / SciFact-RoBERTa, already used by CAFA Pillar 2), a reranker.

**Evolve it (how-to):**
1. **Spec the decision** as classification/entailment with a measurable metric (accuracy/F1/AUC).
2. **Data:** labeled set in `data/ml/<name>.jsonl` (`{text, label}` or `{claim, evidence, label}`).
   Follow the Computational Scientific Method rule — no fabricated labels; cite source if derived.
3. **Train:** feature = `nomic-embed` vector (or model logits); fit with scikit-learn; persist
   `.pkl`/`.joblib`. For NLI, use the existing DeBERTa/SciFact endpoint, don't retrain unless needed.
4. **Eval gate:** held-out metric ≥ target + a calibration check (don't ship overconfident heads).
   Emit a reproducible receipt (sha256 of model + data, metric).
5. **Wrap** as an antibody system (Antigence agent class) so it plugs into the same
   `ANTIBODY_REGISTRY` lane mechanism. Strict output = `json_grammar`.
6. **Govern:** results are PROVISIONAL until gsigmad/Overwatch validation (Antigence adapter
   forbids claim promotion). Trust-tier per `gsigmad-audit-output`.

## Track C — Fine-tuned tiny→small local models (LoRA / distillation / Modelfile)
**What they are:** a small Ollama-served model specialized to a recurring task the general models get
wrong — built by LoRA fine-tune or distillation from the big worker, served as a `tiny_cell` or
`fine_tuned_worker` rung.

**Evolve it (how-to):**
1. **Trigger:** the maxmodel benchmark (`experiments/maxmodel/`) shows a task tier where all
   small models score low AND the task is high-volume (worth specializing). Distillation target =
   the big worker's outputs on that task.
2. **Data:** collect (prompt → good output) pairs — from the big worker (distillation) or labeled
   gold. ≥ a few hundred examples. Store `data/finetune/<task>.jsonl`. No fabricated targets.
3. **Train:** LoRA on the base (e.g. `qwen2.5:1.5b`/`llama3.2:3b`) with MLX-LM or unsloth; or full
   distillation. Keep base small so it fits a co-resident slot.
4. **Package for Ollama:** `ollama create <name> -f Modelfile` (FROM base + ADAPTER lora). It now
   appears in `ollama list` and the routing roster.
5. **Eval gate:** **re-run the maxmodel harness** (`maxmodel_probe.py --only <name>`) → must beat the
   incumbent small model on the target suite at acceptable decode_tps, within the swap budget. Add
   to `manifest.json` only if it passes.
6. **Register in the routing roster** (`models.yml` + `antibody_model_policy` role=`structured_cell_type`
   or `escalation_rung`) so Ollarma routes the task to it before escalating to the big model.

---

## Cross-cutting: governance + integration (do these for every new artifact)
- **Pre-register** the evolution as an experiment (`PREREG.md` pattern) with a binding claim ceiling.
- **Eval-gate before promotion** — held-out metric + reproducible receipt (sha256 of artifact +
  data). No eval → stays PROVISIONAL, never auto-promoted.
- **Integration points:** `src/ollarma/guardrail.py` (`ANTIBODY_REGISTRY`), `antibody_profile.py`
  (lane/head_type), `antibody_model_policy.py` (role/fan-out vs sequential rung), `models.yml` +
  `manifest.json` (roster), the routing ladder (`routing_ladder.py`/`stage_router.py`).
- **Boundary:** Antigence *produces* antibodies/ML and *reviews*; Ollarma *routes/executes*. Neither
  promotes claims or writes canonical truth — that's gsigmad/Overwatch (`gsigmad-audit-output`).
- **Resilience:** every rung fails closed (abstain→escalate); if antigence is unavailable the lane is
  pass-through, never a hard error.

## Reuse the benchmark as the selection oracle
The maxmodel JSON DB (`/Volumes/magicBLACKbox/ollarma-experiments/maxmodel/db/`) is the measured
menu: footprint (fits the slot?), decode_tps (serviceable?), per-task score (good enough?). Any new
tiny/fine-tuned model must clear the harness before it earns a routing slot. See
`experiments/maxmodel/HOWTO.md`.

## Next concrete steps (skeleton → action, when we choose to evolve)
- [ ] Install `antigence` into Ollarma's `.venv` so in-process antibody lanes stop passing through.
- [ ] Pick the first antigen to harden (candidate: prompt-injection `rule_floor` + bank).
- [ ] Stand up a labeled-data folder (`data/antigens/`, `data/ml/`, `data/finetune/`) with the
      eval-gate contract.
- [ ] Wire the maxmodel harness as the CI gate for any new tiny/fine-tuned model.
