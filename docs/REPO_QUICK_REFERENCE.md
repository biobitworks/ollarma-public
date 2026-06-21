# ollarma Quick Reference

Status: operator_approved
Last reviewed: 2026-06-07
cellARCH name: cellARCHollarma
cellARCH version: cellARCHollarma-v0.1.0
Cellico-Bio spine version: CBIO-METHOD-SPINE-v0.1.0
Central KB start node: <repo>/.planning/quick/260606-asap-cross-seeding-repo-rrr-redteam-research-remediate/architecture-closeout/CELLICO_BIO_KB_START_NODE.md
Decision state: OPERATOR_APPROVED

## One-Line Role

operator-approved cellARCH placement

## Role And Authority

Shared shorthand: PI = Principal Investigator; PM = Project Manager; SWE = Software Engineer; O = Operator; U = User; A = Admin; R = Review.

| Field | Value |
|---|---|
| PI owner | repo-local / operator-assigned |
| PM owner | repo-local / operator-assigned |
| SWE owner | repo-local / operator-assigned |
| O owner | Byron only |
| U surface | cellARCH / Cellico-Bio operators and agents |
| A/sudo owner | Byron only |
| R owner/surface | Antigence, peer review, or repo-local review when risk-triggered |

No agent may claim Admin authority or run sudo/admin actions. Local repo roles may narrow this authority floor; they may not expand it.

## Spine Fit

| Field | Value |
|---|---|
| Spine stage(s) | governance |
| Role | support, validator, governance, reference_as_needed |
| Upstream input | bounded prompts, model-route requests, bridge messages, workflow manifests, process/status checks, agent requests, and receipt handoffs |
| Deterministic analysis | deterministic orchestration, admission checks, bridge handoff, workflow/status monitoring, resource reporting, and receipt generation around probabilistic agent/model routing and local-model responses |
| Downstream output | local route responses, bridge/session logs, model/runtime status, workflow admission receipts, pause/resume handoffs, deterministic monitor summaries, and probabilistic agent/model outputs with provenance and ceilings |
| Claim ceiling | planning / support / validation only unless owner-repo receipts explicitly say otherwise |

## Environment

| Field | Value |
|---|---|
| Environment type | repo-local; inspect env files |
| Env file(s) | pyproject.toml |
| Lock file(s) | included above when present |
| Optional env lanes | unknown unless repo-local docs say otherwise |
| Runtime notes | Substrata remains compute priority under pressure; local repo rules may add stricter limits |

## Data And Databases

| Field | Value |
|---|---|
| Local data roots | repo-local only; do not copy private payloads into this card |
| File-backed outputs | repo-local results/artifacts only when governed |
| Database surfaces | none recorded here unless repo-local docs specify |
| KG references | watchKG |
| Writeback posture | blocked or explicitly gated; never inferred from this card |

## Tasks And State

| Field | Value |
|---|---|
| Current active task | milestone=v5.1; milestone_name=Phases; status=v5.1 ship status — criteria #1-4 + #6 MET; #5 sole carry-forward. Phases 66/67/68 BUILD-COMPLETE (#1-3, 116 lane tests); Phase 69 COMPLETE (#4); Phase 71 COMPLETE (#6 — notebook_workflow.py + 20 tests merged 7e6e762; BOTH operator gates crossed: gsd adapter allowed_receipts append af24ac8 [branch ollarma/phase71-notebook-receipt, pending merge to gsd main] + real deltaprot EXP-004 run 5f86e34, receipt_hash d72d562a, notebook NOT executed, deltaprot tree unchanged); Phase 70 T9b PARTIAL (#5 NOT met — Codex-peer EXP-002 calibration → T9c rerun → PI sign-off, NOT solo-achievable). Tests: full suite 1618 passed / 1 live-deselected, 0 failures. Tagging v5.1-rc carrying #5.; last_updated=2026-05-31T22:45:00.000Z; last_activity=2026-05-31 — Phase 70 T9b live acceptance run committed (420d2ae): verdict PARTIAL. 5 named MESI gates PASS; inert-control strict-cleanliness sub-criterion FAIL (max round-over-round JSD 0.0412 >= 0.02, from the cold-start round0->round1 transition; falsification threshold 0.05 NOT crossed). Per operator: remediation/calibration, NOT engine rejection; criterion #5 NOT marked PASS; do NOT close T11/T12 as clean. Calibration follow-up: .planning/quick/260531-phase70-inert-calibration/. T10 audit re-run is PROVISIONAL/BLOCKED (docs/EXP_CLOSEOUT_CONTRACT.md drafted but DRAFT/unapproved; no EXP note authored). Phase 69 closed earlier today (proof bundle runs/swarm_proof/20260531T172553Z, criterion #4). |
| Current blocker | see repo-local STATE/ROADMAP/receipts |
| Latest receipt | repo-local latest receipt, if present |
| Next safe action | advance repo to next milestone only after local card + current STATE/ROADMAP review |
| Do not do | no writeback, release, public copy, claim promotion, commit/push/rsync/delete, or secret exposure without explicit approval |

## Git And GitHub

| Field | Value |
|---|---|
| Git repo? | true |
| Current branch | main |
| Git head | ce45a69 |
| Dirty count at review | 0 |
| GitHub remote | present |
| Push policy | operator-only |
| Backup/mirror policy | operator-only unless repo-local policy says otherwise |

## Secrets, Passwords, APIs

Do not write secrets or tokens here.

| Field | Value |
|---|---|
| Secret refs | by name only; not audited in this card |
| API refs | by name only; not audited in this card |
| Password refs | keychain/vault reference only |
| Secret status | unknown unless repo-local docs say otherwise |

## Security, IP, And IT Controls

| Field | Value |
|---|---|
| Data classification | internal/private by default unless repo-local docs say public |
| IP/public-copy posture | gated |
| Prompt/model-risk posture | Antigence required for weak, unsafe, suspicious, generated-to-ranking, or claim-promoting outputs |
| Dependency/supply-chain posture | inspect lock files and repo-local docs |
| Resource priority | low_on_pressure unless explicitly prioritized; Substrata priority wins under pressure |
| Backup/rsync posture | operator_only |
| Incident pause condition | pause on secret exposure, private payload emission, writeback attempt, claim promotion, unsafe/model-risk output, or resource pressure conflict |

## Publications And External Sources

| Field | Value |
|---|---|
| Publications used | repo-local publication/citation registry if present |
| Dataset/source refs | repo-local source-freeze/registry refs if present |
| License/DUA status | gated/unknown unless receipt-bound |
| Public-copy status | blocked/gated unless operator/counsel approved |

## Bridge And Recovery

| Field | Value |
|---|---|
| Ollarma bridge files | `.ollarma/RESUME.md`, `.ollarma/session-log.jsonl`, or repo-local bridge when present |
| Antigence triggers | weak output, model risk, unsafe prompt, prompt injection, secrets-adjacent material, generated-to-ranking, claim promotion |
| Overwatch triggers | canonical truth, writeback, release, promotion, source-of-truth changes |
| Recovery instruction | Start from this card, then central KB start node, then repo-local STATE/ROADMAP/latest receipts. |

## Boundaries

- Ollarma can route, monitor, and carry receipts; it is not approval authority for writeback, release, claim promotion, public copy, secrets, or safety decisions
- This card is a quick reference, not canonical Overwatch truth.
- This card does not approve DB/KG writeback, public copy, release, commit/push, rsync, deletion, claim promotion, or admin actions.
- Admin/sudo is Byron-only.
