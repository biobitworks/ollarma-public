# Ollarma — Licensing Tiers

**Status:** DECIDED (PM) and instantiated as concrete license files; the open-core boundary below is the maintainer's chosen structure on top of **Apache-2.0** (`LICENSE`). Matches the goal of "a public version and a private version for academic use and corporate licensing."

**Concrete artifacts (now exist in the repo root):**
- Public core → [`LICENSE`](../../LICENSE) (Apache-2.0)
- Academic → [`LICENSE-ACADEMIC-ADDENDUM.md`](../../LICENSE-ACADEMIC-ADDENDUM.md) (v1.0, free non-commercial research)
- Corporate → [`LICENSE-COMMERCIAL.md`](../../LICENSE-COMMERCIAL.md) (v1.0, terms summary)

**Not legal advice.** The structure is decided; the binding wording (especially the commercial order form and trademark registration) should still pass counsel review before publication. That review is a standard pre-publish gate, not an open design question.

---

## 1. The three tracks

| Track | Audience | License | What they get | What they owe |
|---|---|---|---|---|
| **Public (OSS core)** | anyone; community; evaluation | **Apache-2.0** | the substrate (cascade router, verifier guardrails, receipt chain, swarm engine, bench harness), synthetic example adapters, all surfaces, full tests | attribution; patent grant per Apache-2.0; no warranty |
| **Academic** | universities, non-profit research labs | **Apache-2.0 core + Academic Use Addendum** (free, non-commercial research) | OSS core **plus** the research extensions: reproducibility/provenance tooling, pre-registration (PROMPT→EXP) harness, evaluation rubrics, citation pack | cite the project in publications; share derived eval artifacts back where possible; non-commercial scope |
| **Corporate** | companies deploying internally or in products | **Commercial License** (paid; per-seat or per-deployment) | OSS core **plus** a supported distribution: private-portfolio integration adapters, governance backends (Overwatch/Watchtower-style), priority support, indemnification, SLA | license fee; standard commercial terms |

---

## 2. Open-core boundary (what's free vs paid)

The cleanest, most defensible split keeps the **engine fully open** and charges for **integration + support + assurance**, never for the core mechanism. This is the standard open-core pattern and avoids the "crippled OSS" anti-pattern.

**Always free / Apache-2.0 (the §2a substrate in `PUBLIC_RELEASE_PLAN.md`):**
- model cascade / routing ladder / escalation
- verifier guardrails + antibody policy framework
- deterministic execution boundary
- provenance / receipt chain
- recovery engine, local KB/retrieval, bench harness, swarm engine
- HTTP / CLI / MCP surfaces
- synthetic example adapters; full test suite

**Academic-addendum extras (free for non-commercial research):**
- pre-registration harness (PROMPT-### → EXP linkage, claim-ceiling review)
- evaluation rubrics + reference datasets glue
- the provenance/citation pack (PROV sidecars, manifest verification)
- reproducibility runbooks

**Corporate-only (paid):**
- turnkey private-portfolio integration adapters (bring-your-own-repos at scale)
- governance backends (Overwatch/Watchtower-style truth + operator console)
- priority support, security response SLA, indemnification
- a hosted/managed control plane (if offered later)

> **Principle:** never paywall a verification or safety mechanism. Safety features are free in every tier. The paid value is *integration, support, and assurance*, not *protection*.

---

## 3. Trademark / naming

- Apache-2.0 grants copyright + patent rights but **not** trademark rights. The names **Ollarma** and **Antigence** (and any logos) should be held as marks so forks can use the code but not imply endorsement.
- Public README must include a short trademark policy: "the code is Apache-2.0; the name is a trademark — name your fork something else."

---

## 4. Antigence relationship

Antigence (the immune-inspired review/escalation lane) is a **separate repo** and likely its own license decision. Recommended alignment:
- The **antibody-policy framework** and verifier interfaces in *Ollarma* stay Apache-2.0 (they're substrate).
- Antigence's **specific antibody packs / antigen banks** can be tiered independently (some open exemplar packs; domain-specific packs commercial). This matches RTB-02's "project-specific antibody packs are first-class; some packs explicitly export reusable antibodies."
- The unification plan (`../UNIFICATION_SINGLE_PACKAGE_PLAN.md`) assumes both projects converge on a compatible open-core boundary so the single downloadable package has one coherent license story.

---

## 5. Decision checklist (before any public publish)

- [ ] Counsel review of the open-core boundary and Academic Use Addendum wording.
- [ ] Trademark registration decision for Ollarma / Antigence.
- [ ] CONTRIBUTING + CLA/DCO choice (DCO is lighter-weight; CLA enables relicensing).
- [ ] SPDX headers added to source in the public tree.
- [ ] Patent-grant review (Apache-2.0 §3 already grants; confirm no conflicting obligations).
- [ ] Export-control / dual-use review (the verification layer is defensive; document intended use).
