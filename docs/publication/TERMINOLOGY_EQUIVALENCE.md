# Ollarma — Terminology Equivalence

**What ollarma actually is:** a **communication, safety, and verification layer for AI-assisted computational research** — it sits between research agents (Claude Code, ChatGPT Codex, local Ollama models, future provider adapters) and routes, checks, and records their work so that local models do the cheap/private/bounded work and only hard or low-confidence cases escalate to a frontier model.

**Why the immune metaphor exists:** the project grew out of an immune-system metaphor (cells, antibodies, antigens, sensors, a swarm). The metaphor is a *naming convenience*, not a biological claim. This document maps every metaphor term to its established term in the machine-learning / computer-science literature, with verified citations, so the work can be written up and reviewed by researchers using the names they already know.

**Audience:** anyone publishing, reviewing, or extending this work who needs the proper academic vocabulary instead of the in-house metaphor.

---

## 1. The one-sentence equivalence

> Ollarma is a **locally-hosted model-cascade router with verifier-based guardrails, multi-agent deliberation for hard cases, constrained (structured) decoding, and a provenance/receipt audit chain** — i.e. a *learning-to-defer / selective-prediction* system whose abstention action is "escalate to a frontier model," wrapped in a verification layer.

Every metaphor term below is a sub-component of that sentence.

---

## 2. Equivalence table (metaphor → research term → citation)

| In-house metaphor | What it is under the hood | Established research term | Key citation(s) |
|---|---|---|---|
| **"cell" / "cell-type"** (a model assigned a role) | a model specialized to one sub-task and selected per query | **expert** in a **mixture-of-experts**; role-specialized model | Jacobs et al. 1991, *Adaptive Mixtures of Local Experts*, Neural Computation 3(1):79–87; Shazeer et al. 2017, arXiv:1701.06538 |
| **"tiny cell fan-out"** (several cheap cells in parallel) | sampling many cheap candidates and aggregating | **ensemble / self-consistency** (sample diverse paths, take the consistent answer) | Wang et al. 2022, *Self-Consistency Improves Chain-of-Thought Reasoning*, arXiv:2203.11171 |
| **"antibody"** (a check that flags bad output) | a learned-or-rule detector that screens generated output | **verifier / guardrail / classifier** | Cobbe et al. 2021, *Training Verifiers to Solve Math Word Problems*, arXiv:2110.14168; Bai et al. 2022, *Constitutional AI*, arXiv:2212.08073 |
| **"antigen"** (the bad pattern an antibody catches) | the specific failure mode / unsafe pattern targeted | **failure mode / error class / unsafe-content target** | (verifier + content-filter literature, above) |
| **"antigen bank"** (set of known-bad exemplars) | a retrieval index of labeled examples for similarity matching | **exemplar/reference set; retrieval index (kNN over embeddings)** | Lewis et al. 2020, *Retrieval-Augmented Generation*, arXiv:2005.11401 |
| **"recall sensor"** (1.5B that alarms but can't emit structure) | a high-recall, low-precision first-stage screen | **first-stage filter in a detection cascade / screening classifier** | Viola & Jones 2001, *Rapid Object Detection using a Boosted Cascade*, CVPR; Geifman & El-Yaniv 2017, arXiv:1705.08500 |
| **"escalation rung / ladder"** (cheap → expensive routing) | route a query to a stronger model based on difficulty/uncertainty | **model cascade / cost-aware routing / hybrid inference** | Chen, Zaharia & Zou 2023, *FrugalGPT*, arXiv:2305.05176; Ding et al. 2024, *Hybrid LLM*, ICLR, arXiv:2404.14618; Ong et al. 2024, *RouteLLM*, arXiv:2406.18665 |
| **"escalate to frontier model"** (the abstain action) | defer hard/low-confidence cases to a larger model/human | **learning-to-defer / selective prediction (classification with a reject option)** | Mozannar & Sontag 2020, *Consistent Estimators for Learning to Defer to an Expert*, ICML, arXiv:2006.01862; Geifman & El-Yaniv 2017, arXiv:1705.08500 |
| **"swarm"** (N personas × M deliberative rounds) | several model instances debating toward consensus | **multi-agent debate / deliberation** | Du et al. 2023, *Improving Factuality and Reasoning through Multiagent Debate*, arXiv:2305.14325 |
| **"stance distribution / dissent clusters / JSD detectability"** | quantifying agreement/disagreement among agents over rounds | **distributional divergence (Jensen–Shannon); inter-agent agreement** | Lin 1991, *Divergence Measures Based on the Shannon Entropy*, IEEE Trans. Inf. Theory 37(1):145–151 |
| **"judge / orchestrator cell"** (phi4-mini scoring others) | a model that scores or selects other models' outputs | **LLM-as-a-judge** | Zheng et al. 2023, *Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena*, arXiv:2306.05685 |
| **"deterministic guardrails"** (JSON grammar; validated asset classes only) | forcing output into a formal structure; refusing un-allow-listed operations | **constrained / grammar-constrained decoding; structured output; capability allow-listing** | Geng et al. 2023, *Grammar-Constrained Decoding for Structured NLP Tasks*, EMNLP, arXiv:2305.13971 |
| **"strict-output / json_mode / json_grammar"** | guaranteeing schema-valid generation | **structured decoding / format enforcement** | Geng et al. 2023, arXiv:2305.13971 |
| **"receipt chain / hash-chained receipts"** | tamper-evident, replayable audit log of every decision | **provenance / data lineage; reproducibility** | W3C PROV-DM (2013), W3C Recommendation |
| **"claim ceiling / claim-ceiling review"** | bounding how strongly a conclusion may be stated vs. its evidence | **evidence grading / overclaiming control; claim verification** | (fact-verification & evidence-hierarchy practice; analogous to NLI entailment checking) |
| **"verification layer"** (the whole system) | independent checking of AI output before it is trusted | **verification / self-verification / grounding / fact-checking** | Cobbe et al. 2021, arXiv:2110.14168; Lewis et al. 2020, arXiv:2005.11401 |
| **"rescue model / pinned floor"** | an always-resident fallback model | **fallback / graceful degradation** (systems reliability) | — (standard reliability engineering) |
| **"bridge / communication layer"** | the substrate routing between agents and providers | **inference gateway / model router / orchestration layer** | Ong et al. 2024, arXiv:2406.18665 (routing); FrugalGPT (cascade) |

---

## 3. The "research and citations" analogy (the scholarly reading)

The user asked to "find the equivalence with research and citations." There are two complementary readings, both true:

**(a) Mechanism equivalence** — §2 above: each metaphor term equals a known ML/CS mechanism.

**(b) Scholarly-practice equivalence** — ollarma's governance vocabulary maps directly onto how *science itself* handles evidence:

| Ollarma governance term | Scholarly-practice equivalent |
|---|---|
| **claim ceiling** (MEASURED / INFERRED / SPECULATIVE / FORBIDDEN) | **evidence grading / levels of evidence** — a conclusion may not be stated more strongly than its data supports |
| **receipt chain** (hash-chained, replayable) | **provenance / chain of custody / citation trail** — every assertion traces to a verifiable source |
| **antibody verdict provenance** (target, input hash, output hash, authority status) | **peer review with reproducible artifacts** — who checked what, against which version, with what standing |
| **escalate-to-frontier** when local evidence is insufficient | **defer to a domain expert / higher-powered method** when the cheap test is inconclusive |
| **pre-registration (PROMPT-### → EXP)** | **study pre-registration** — hypotheses and thresholds fixed *before* the run, to prevent post-hoc fitting |
| **claim-ceiling-review before promotion** | **fact-checking / citation verification before publication** |

This is why the system is honestly described as a **safety/verification layer for AI-assisted computational research**: it imports the discipline of evidence-graded, provenance-tracked, pre-registered science into the loop where an AI agent produces research outputs — and refuses to let an unverified AI claim be promoted as if it were established.

---

## 4. The proper one-paragraph abstract (for the paper)

> We present **Ollarma**, a locally-hosted communication, safety, and verification layer for AI-assisted computational research. Ollarma implements a **model cascade** [FrugalGPT; Hybrid LLM; RouteLLM] whose abstention action is **learning-to-defer** [Mozannar & Sontag 2020; selective prediction, Geifman & El-Yaniv 2017]: cheap local **experts** [mixture-of-experts, Jacobs 1991; Shazeer 2017] handle private, bounded work, and only hard or low-confidence queries **escalate to a frontier model**. Generated outputs pass **verifier-based guardrails** [Cobbe 2021; Constitutional AI, Bai 2022] with **retrieval-based exemplar matching** [RAG, Lewis 2020] and **grammar-constrained decoding** [Geng 2023] for deterministic structured output; an **LLM-as-a-judge** [Zheng 2023] and **multi-agent deliberation** [Du 2023] adjudicate contested cases, with disagreement quantified by **Jensen–Shannon divergence** [Lin 1991]. Every decision emits a **hash-chained provenance receipt** [W3C PROV] under a **claim-ceiling** discipline that grades each conclusion to its evidence. The entire pipeline runs offline on commodity Apple-silicon hardware, giving private, reproducible, evidence-graded AI assistance with explicit, audited escalation.

---

## 5. How the other docs use this

- `PUBLICATION_EVIDENCE_BUNDLE.md` measures the *capacity and resilience* of this cascade on the host of record.
- `ANTIGENCE_MODEL_SIZE_CELL_ROLE_FINDINGS.md` gives the empirical **per-rung model assignment** for the cascade (which model fills which expert/verifier role).
- `UNIFICATION_SINGLE_PACKAGE_PLAN.md` describes packaging the router + verifiers + receipts as one trainable download.

All four should use the §2 column-3 terms in any externally-facing writing; the metaphor stays as an internal nickname only.

---

## Citation list (verified 2026-06-10)

1. Jacobs, Jordan, Nowlan & Hinton (1991). Adaptive Mixtures of Local Experts. *Neural Computation* 3(1):79–87.
2. Viola & Jones (2001). Rapid Object Detection using a Boosted Cascade of Simple Features. *CVPR 2001*.
3. Lin (1991). Divergence Measures Based on the Shannon Entropy. *IEEE Trans. Information Theory* 37(1):145–151.
4. Geifman & El-Yaniv (2017). Selective Classification for Deep Neural Networks. *NeurIPS 2017*. arXiv:1705.08500.
5. Shazeer et al. (2017). Outrageously Large Neural Networks: The Sparsely-Gated Mixture-of-Experts Layer. arXiv:1701.06538.
6. Mozannar & Sontag (2020). Consistent Estimators for Learning to Defer to an Expert. *ICML 2020*. arXiv:2006.01862.
7. Lewis et al. (2020). Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. *NeurIPS 2020*. arXiv:2005.11401.
8. Cobbe et al. (2021). Training Verifiers to Solve Math Word Problems. arXiv:2110.14168.
9. Wang et al. (2022). Self-Consistency Improves Chain-of-Thought Reasoning in Language Models. arXiv:2203.11171.
10. Bai et al. (2022). Constitutional AI: Harmlessness from AI Feedback. arXiv:2212.08073.
11. Chen, Zaharia & Zou (2023). FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance. arXiv:2305.05176.
12. Du et al. (2023). Improving Factuality and Reasoning in Language Models through Multiagent Debate. arXiv:2305.14325.
13. Zheng et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. arXiv:2306.05685.
14. Geng et al. (2023). Grammar-Constrained Decoding for Structured NLP Tasks without Finetuning. *EMNLP 2023*. arXiv:2305.13971.
15. Ding et al. (2024). Hybrid LLM: Cost-Efficient and Quality-Aware Query Routing. *ICLR 2024*. arXiv:2404.14618.
16. Ong et al. (2024). RouteLLM: Learning to Route LLMs with Preference Data. arXiv:2406.18665.
17. W3C (2013). PROV-DM: The PROV Data Model. W3C Recommendation.

*Citations 4–16 verified against arXiv/proceedings on 2026-06-10. Citations 1–3 and 17 are canonical/textbook references cited by title, author, year, and venue.*
