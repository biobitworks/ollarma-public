# Local vs Online Boundary — protein-function verification (and the pipeline generally)

**Operator directive (2026-06-10):** antibodies, antigens, and cells run on **local models**; **no frontier model and no online database until absolutely necessary**; **MCP and RAG are a test oracle to grade the local models, not a data source.**

This document draws the boundary precisely and explains how it is *measured*, not just asserted.

## The headline

For this task the boundary sits **far toward local**. After a **one-time dataset download**, the entire verification pipeline — deterministic guardrails, all ML antibodies/antigens/cells, calibration, and cellico-bio validation — runs **fully offline on the M1 host**. Online and frontier are reserved for three narrow, explicit cases.

## The four zones

| Zone | What's in it | When it runs |
|---|---|---|
| **① Fully local (default, always)** | GO DAG logic + all deterministic rule_floor antibodies; the cells (qwen/granite/phi local models 1.5–14B); ESM-2 *small* (8M–150M) sequence cell; local NLI/SciFact `claim_entailment`; `open_world_overclaim` judge; nomic-embed antigen-bank; cascade orchestration; receipts; calibration on the local labeled corpus | every run, offline, zero network |
| **② Online ONCE (bootstrap, then cached local)** | download `go-basic.obo` (pinned version), CAFA training annotations, SciFact (claims+abstracts+labels), IA weights | first setup only; afterwards the corpus is local + hashed |
| **③ Online as TEST ORACLE (by choice, not in the runtime path)** | PubMed/HuggingFace **MCP** + **RAG** over the local corpus, used to *grade* the local cascade and locate the residual where local falls short | only in the D8 evaluation harness, never in production verdicts |
| **④ Online/frontier — "absolutely necessary" (last resort)** | (a) fetch a **novel PMID's** abstract absent from the local corpus; (b) **frontier model** escalation when a *high-stakes* claim is below the local-confidence threshold AND every local rung is exhausted | gated by `absolutely_necessary=true`, logged with an `escalation_receipt`; a clean run hits this **zero** times |

## Component-by-component

| Component | Local? | Note |
|---|---|---|
| GO ontology + DAG propagation | ✅ ① | `goatools`/`obonet` on the downloaded `.obo`; pure logic |
| Deterministic GO antibodies (validity/score/cap/propagation/format) | ✅ ① | no model at all |
| `pmid_citation_present` | ✅ ① | rule |
| `pmid_resolves` against local corpus | ✅ ① | online only for a novel PMID (zone ④a) |
| `claim_entailment` (NLI) | ✅ ① | local PubMedBERT/SciFact model or local LLM + local abstract |
| `open_world_overclaim` | ✅ ① | local judge cell, json_grammar |
| `sequence_grounding` | ✅ ① | ESM-2 small on M1 (MPS); ProtT5/ESM-large = zone ④ escalation only |
| antigen-bank (exemplar retrieval) | ✅ ① | locally-pinned nomic-embed |
| cascade orchestration + receipts | ✅ ① | Ollarma substrate |
| Calibration (MCC/precision/recall) | ✅ ① | runs on the **local** labeled split; graded by ③, not frontier |
| MCP/RAG reference | ③ | grades local; not a dependency of any antibody |
| Frontier judge | ④b | last-resort residual only |

## Why this is the right boundary

The local roster already covers every cascade role (verified: `ollarma calibrate` profiled 21 local models, 0 role gaps). The labeled science (GO/CAFA/SciFact) is downloadable and small enough to cache. The only thing a local model *cannot* manufacture is a fact it has never seen — which is exactly the novel-PMID (④a) and the high-stakes-uncertain (④b) cases, and nothing else. So "local until absolutely necessary" is not a compromise here; it is achievable for ~100% of the calibration and the large majority of production verification.

## The boundary is MEASURED, not assumed

The validation deliverable (Ollarma D8 + Antigence A5) **empirically locates** zone ④. For each antibody/cell task we report local-only accuracy vs the MCP/RAG oracle on labeled data:
- where local matches the oracle within tolerance → that task is **proven local** (stays in ①);
- where local lags → that task's residual is the **measured** online/frontier need (the only justified zone-④ traffic).

So we don't guess where local stops working — we measure it on CAFA/SciFact and let the numbers draw the line. That measurement is itself the comp-bio validation + pipeline stress test cellico-bio needs.

## Stop rules
- No antibody/cell may call frontier or an online DB in its default path; any such call is a flagged `absolutely_necessary` exception with a receipt.
- MCP/RAG outputs are grading signals only — never promoted into a production verdict or the training data (zone ③ stays out of ①).
- The one-time dataset download is the only sanctioned bootstrap network use; everything else is local.
