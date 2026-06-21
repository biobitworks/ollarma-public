# CAFA 6 → Ollarma/Antigence — Guardrails, Antibodies, and Reusable Components

**Compiled:** 2026-06-10 · **Role:** PI/PM/O/SWE
**One line:** CAFA 6's free-text prediction task is a near-perfect application of the Ollarma verification layer + Antigence antibodies, and the Overwatch publication-**ingestion** pipeline run **in reverse** (generation → verification) supplies most of the machinery.

**Honest scope first.** The CAFA 6 *scored* GO-term task closed (final submission 2026-02-02; final eval 2026-06-01, now "Late Submission"). So this is **not** "let's win CAFA." The value is: (a) **reusable verification/guardrail components** that strengthen the protein-science cluster (deltaprot, xenodisorder, cellico, shadow-seeds) and (b) the **free-text prediction eval is still upcoming** (LLM phase ~9–12 mo after the deadline, i.e. late 2026–2027), and it is *exactly* a claim-with-citation verification problem — our wheelhouse.

---

## 1. Why this fits the verification-layer thesis

CAFA's own rules already encode our governance vocabulary:
- **Open-world annotations** ("absence of evidence ≠ evidence of absence") = the **claim-ceiling** discipline. You may not assert a function is *absent*; predictions are probabilistic in (0, 1].
- **Information accretion (IA) weighting** (Clark & Radivojac 2013): deeper GO terms carry more information, are harder, weigh more — i.e. **stronger claims need stronger evidence.** That is a claim-ceiling weighting function handed to us for free.
- **Free-text with PMID citations** + **up to 5 lines at differential confidence** = a built-in **provenance + per-claim confidence** structure (our receipt chain + claim tiers).
- **LLM-judged then human-judged** text eval = **LLM-as-a-judge → human escalation**, the cascade.

So CAFA isn't analogous to our system; it's an instance of it.

---

## 2. Two guardrail surfaces (the antibodies)

### 2a. GO-term prediction — DETERMINISTIC guardrails (rule_floor antibodies)

These are pure logic/ontology checks — no model needed; they run as the deterministic floor (cheapest cascade rung). A submission antibody must verify:

| Antibody (key) | Check | Tool |
|---|---|---|
| `go_term_validity` | every predicted GO ID exists in the declared GO version; invalid → drop | `goatools` `GODag(go-basic.obo)` / `obonet` |
| `go_score_range` | score ∈ (0, 1.0], ≤3 sig figs; score=0 forbidden | rule |
| `go_term_cap` | ≤1500 terms per protein across MF+BP+CC | rule |
| `go_propagation_consistency` | propagated to root; parent score ≥ max(child scores); the eval auto-propagates, so pre-propagate to avoid surprise | `goatools`/`obonet` ancestor traversal + `networkx` |
| `subontology_partition` | each term tagged to the correct MF/BP/CC namespace | `goatools` |
| `leaderboard_leakage` | exclude the UniProt-provided leaderboard proteins from claims meant for the test superset | set diff |
| `format_contract` | tab-separated, no header, target IDs match FASTA headers | rule |

**This is the literal "deterministic guardrails" pillar** — and it's reusable for *any* ontology-labeled prediction (DisProt/CAID terms in xenodisorder, etc.). Wrap **[CAFA-evaluator](https://github.com/BioComputingUP/CAFA-evaluator)** as the scoring oracle behind these.

### 2b. Free-text prediction — VERIFIER/JUDGE antibodies (the real payoff)

The free-text task = "describe the protein's function, cite PMIDs, don't over-claim." Each is an antibody:

| Antibody (key) | What it catches | Head type | Cascade rung |
|---|---|---|---|
| `pmid_citation_present` | a factual assertion with no traceable citation | rule_floor | screen sensor |
| `pmid_exists_and_resolves` | a cited PMID that doesn't exist / doesn't match | rule + PubMed E-utilities | structured verifier |
| `claim_entailment` | the cited paper does **not** support the claim (NLI / claim verification) | model_judge | escalation rung |
| `open_world_overclaim` | absence-as-evidence, over-specific, causal overreach beyond evidence (claim-ceiling) | model_judge | reasoning rung |
| `sequence_grounding` | de-novo claim (no literature) not derivable from sequence/structure features | antigen_bank + model | reasoning rung |
| `entity_normalization` | named genes/proteins/processes that don't normalize to real IDs (hallucinated entities) | rule+model | structured verifier |
| `text_format_contract` | ASCII 33–126, ≤5 lines, ≤3000 chars, no tabs, per-line confidence | rule_floor | screen sensor |

**`claim_entailment` is a direct reuse of the immunOS preprint's SciFact baseline** — scientific claim verification (claim + abstract → SUPPORT/REFUTE/NOINFO) is exactly the antibody needed here. The preprint's machinery transfers wholesale.

---

## 3. "Overwatch ingestion, in reverse" (your instinct, made concrete)

The gsigmad/Overwatch publication pipeline runs **forward**: publication → parse → structured claims + provenance → SeedGraph → claim-ceiling review → queue. For CAFA free-text we run the **same stages backward**:

```
 INGEST  (Overwatch, forward):  paper text ─▶ sentences ─▶ entities/relations ─▶ claims+PMIDs ─▶ claim-ceiling ─▶ KG
 VERIFY  (CAFA, reverse):       generated function text ─▶ sentences ─▶ entities/relations ─▶ claims+PMIDs ─▶ claim-ceiling ─▶ accept/flag
```

Every component you already use to *read* publications is what you need to *check* generated text:
- **Sentence segmentation / "diagramming"** — split text into atomic claims and parse subject–predicate–object (protein → does → function). The ingest path's sentence splitter is the verify path's claim extractor.
- **Entity normalization / dictionaries** — the same gene/protein/GO dictionaries used to recognize entities in papers are used to confirm generated entities are real (not hallucinated).
- **Provenance / PMID linkage** — ingest *records* the PMID a claim came from; verify *requires* the generated claim to name a PMID and checks it. Same provenance schema, opposite direction. This is the receipt chain.
- **Claim-ceiling review** — ingest grades incoming claims; verify grades outgoing claims before they're trusted. Identical gate (`gsigmad-audit-claims`, `gsigmad-claim-ceiling-review`).

So the reverse-ingestion antibody pipeline is: **segment → parse → normalize entities → require+resolve PMIDs → entailment-check → claim-ceiling grade → emit receipt.**

---

## 4. Reusable Python packages (grounded)

| Need | Packages | Notes |
|---|---|---|
| **GO ontology / DAG logic** | `goatools` (`GODag`, ancestor propagation), `obonet` (OBO→`networkx`), `pronto` (OBO/OWL parser), `networkx` | parse `go-basic.obo`, propagate to root, namespace partition — the `rule_floor` antibodies |
| **Official scoring oracle** | [`CAFA-evaluator`](https://github.com/BioComputingUP/CAFA-evaluator) | IA-weighted Fmax, propagation; wrap as the deterministic gate |
| **Logic / entailment / reasoning** | `owlready2` (OWL reasoner over GO axioms), `pronto`, `problog`/`pyke` (probabilistic logic), NLI models | GO consistency + `claim_entailment`. (DeepGO-SE frames function prediction itself as *semantic entailment* — same idea applied to guardrails) |
| **Sentence parsing / "diagrams"** | `spaCy` + `scispaCy` (biomedical models), `stanza`, `benepar` (constituency trees = sentence diagrams), `amrlib` (AMR) | claim extraction + structure check |
| **Biomedical NER + normalization (dictionaries)** | [`BERN2`](https://github.com/dmis-lab/BERN2), PubTator Central, scispaCy UMLS linker, MeSH/UMLS | `entity_normalization`; hallucinated-entity detection |
| **Citation verification** | NCBI E-utilities / Entrez (`biopython.Bio.Entrez`), PubMed/PMC, the PubMed MCP already wired in this session | `pmid_exists_and_resolves`; resolve PMID → abstract for entailment |
| **Claim verification (text→support)** | SciFact dataset + SciBERT/PubMedBERT/BioBERT, the immunOS-preprint SciFact baseline | `claim_entailment`, `open_world_overclaim` |
| **Sequence → function (the actual ML, if predicting)** | ESM-2, ProtT5 embeddings; `DeepGOPlus`, `DeepGO-SE`, `ProteInfer` | only if generating predictions; ESM-2 small variants run on the M1 host, large ones escalate to frontier/bigger HW |
| **Datasets / dictionaries** | GO (`go-basic.obo`), UniProtKB, PubMed/PMC, CAFA training annotations + IA weights, MeSH/UMLS | offline-cacheable; fits the local-first constraint |

Lighter deps (goatools, obonet, spaCy small, Entrez) fit the offline/bounded M1 ethos; heavier ones (BERN2, ESM-2 large) are escalation-only or one-time.

---

## 5. Cells-in-roles mapping (the antibody swarm for protein-function text)

Using the measured size→role ladder (`ANTIGENCE_MODEL_SIZE_CELL_ROLE_FINDINGS.md`):
- **screen sensor (1.5–2B)** — `text_format_contract`, `pmid_citation_present` (cheap, high-recall: "is there a citation at all?")
- **structured verifier (2–4B, json)** — `go_score_range`, `entity_normalization` triage, format JSON verdicts
- **judge (phi4-mini)** — score free-text quality (LLM-as-a-judge, mirrors CAFA's own phase-1 LLM eval)
- **router (granite 8B, json_grammar)** — dispatch which antibody lane a claim needs
- **escalation rung (9B) → reasoning (14B, on-demand)** — `claim_entailment`, `open_world_overclaim` (the hard NLI/overreach calls)
- **frontier (cloud, receipted)** — only when local entailment confidence is below threshold on a high-stakes claim
- **deterministic rule_floor** — all of §2a runs with no model at all (goatools + rules)

This is "the right type of cells in different roles with orchestration," instantiated on a real biomedical task.

---

## 6. Portfolio reuse (why this is worth building even though CAFA scoring closed)

The same antibodies + reverse-ingestion pipeline serve the protein cluster directly:
- **deltaprot** — proteomic claim review (the antibodies are the review lane).
- **xenodisorder** — DisProt/CAID is another ontology-labeled prediction; the `*_validity`/`*_propagation` rule_floor antibodies transfer verbatim.
- **cellico / shadow-seeds** — protein-aggregation / ncAA claims need the same `claim_entailment` + `open_world_overclaim` guardrails before any IP/publication use.
- **Antigence** — adds a concrete, domain-grounded antibody pack ("protein-function") with real calibration data, advancing the "no real accuracy number yet" gap (`[[antigence-accuracy-reality-and-tracks]]`): CAFA-evaluator + SciFact give *labeled* data to finally calibrate these antibodies.

---

## 7. Concrete next step (if pursued)

A bounded first build, fits one Ollarma phase:
1. `go_term_validity` + `go_propagation_consistency` + `go_score_range` + `go_term_cap` as a deterministic submission antibody wrapping `goatools` + `CAFA-evaluator` (no model; pure rule_floor). Reusable immediately for xenodisorder/deltaprot.
2. `pmid_citation_present` + `pmid_exists_and_resolves` as a free-text antibody using the existing PubMed E-utilities/MCP.
3. `claim_entailment` as a model_judge lane reusing the immunOS SciFact baseline, calibrated on SciFact (labeled → a real MCC/accuracy number for Antigence).
Each ships under its own gsigmad pre-registration; each emits the verdict-provenance receipt from `ANTIGENCE_RTB_02`. No "win CAFA" claim — the deliverable is calibrated, reusable verification antibodies.

---

## Sources (verified 2026-06-10)
- CAFA-evaluator (BioComputingUP) — official IA-weighted Fmax scorer: https://github.com/BioComputingUP/CAFA-evaluator
- goatools (tanghaibao) — GO DAG handling in Python: https://github.com/tanghaibao/goatools
- BERN2 (dmis-lab) — biomedical NER + normalization: https://github.com/dmis-lab/BERN2
- Clark & Radivojac (2013) — information accretion weighting (CAFA metric basis).
- DeepGOPlus (Kulmanov & Hoehndorf) / DeepGO-SE — sequence→GO; DeepGO-SE = function prediction as approximate semantic entailment.
- Jiang Y, et al. (2016) Genome Biol 17:184 — weighted Fmax full-evaluation formulas (CAFA metric).
